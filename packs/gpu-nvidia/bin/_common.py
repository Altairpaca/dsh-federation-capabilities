#!/usr/bin/env python3
"""Shared helpers for the gpu-nvidia capability pack.

Zero third-party dependencies (Python standard library only).

Mesh shell capability contract, shared by every executor in this pack:

  stdin  : exactly one JSON object (the invocation args)
  stdout : exactly one JSON object, {"ok": true, ...} or {"ok": false, "error": "..."}
  stderr : diagnostics only

Supported host: Linux with the NVIDIA driver and ``nvidia-smi``. Advisory file
locks are POSIX-only; on other platforms locking degrades (with a warning) so
that the read-only capabilities still work for local development.

Security posture: this pack is *not* a sandbox. ``gpu.exec`` and
``gpu.job.submit`` only run scripts that the operator lists explicitly in
``FEDCAP_GPU_SCRIPT_ALLOWLIST``, never an arbitrary shell string, but an
allowlisted script runs with the pack user's permissions.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time

try:  # POSIX advisory locks; the pack also works on non-POSIX hosts without them
    import fcntl
except ImportError:  # pragma: no cover - Windows/macOS development
    fcntl = None

VERSION = "0.1.0"
PACK_ID = "gpu-nvidia"

# A single capability response must stay small. The per-stream caps below are
# chosen so that stdout + stderr + metadata stay under MAX_RESPONSE_BYTES.
MAX_RESPONSE_BYTES = 64 * 1024
MAX_STDOUT_EMBED = 48 * 1024
MAX_STDERR_EMBED = 6 * 1024
MAX_JOB_STREAM_EMBED = 28 * 1024
MAX_ARGS_BYTES = 256 * 1024

DEFAULT_DEVICE_ALLOWLIST = "0,2"
DEFAULT_JOB_DIR = "~/.fedcap/jobs"

GPU_FIELDS = [
    "index",
    "name",
    "uuid",
    "memory.total",
    "memory.used",
    "memory.free",
    "utilization.gpu",
    "temperature.gpu",
    "driver_version",
]

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
OPERATION_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class CapabilityError(Exception):
    """An expected failure with a stable machine-readable ``code``."""

    def __init__(self, code, message):
        super(CapabilityError, self).__init__(message)
        self.code = code


def emit(payload):
    """Write exactly one JSON object to stdout."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(code, detail, **extra):
    payload = {"ok": False, "error": code, "detail": detail}
    payload.update(extra)
    return payload


def log(message):
    sys.stderr.write("[%s] %s\n" % (PACK_ID, message))
    sys.stderr.flush()


def entry(main_fn):
    """Run a capability main function and keep the one-JSON stdout contract."""
    try:
        payload = main_fn()
    except CapabilityError as exc:
        payload = fail(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001 - the shell contract must survive bugs
        log("unexpected failure: %s: %s" % (type(exc).__name__, exc))
        payload = fail("internal_error", "%s: %s" % (type(exc).__name__, exc))
    emit(payload)
    return 0


def read_stdin_args():
    """Read the invocation args. An empty or interactive stdin means ``{}``."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    if len(raw.encode("utf-8", "replace")) > MAX_ARGS_BYTES:
        raise CapabilityError("invalid_input", "stdin JSON is larger than %d bytes" % MAX_ARGS_BYTES)
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise CapabilityError("invalid_input", "cannot parse stdin JSON: %s" % exc)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise CapabilityError("invalid_input", "arguments must be a JSON object")
    return parsed


# ---------------------------------------------------------------------------
# Configuration (resolved by fedcap into FEDCAP_GPU_* environment variables)
# ---------------------------------------------------------------------------


def env_text(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def job_dir():
    raw = env_text("FEDCAP_GPU_JOB_DIR", DEFAULT_JOB_DIR) or DEFAULT_JOB_DIR
    return os.path.abspath(os.path.expanduser(raw))


def device_allowlist():
    raw = env_text("FEDCAP_GPU_DEVICE_ALLOWLIST", DEFAULT_DEVICE_ALLOWLIST) or DEFAULT_DEVICE_ALLOWLIST
    devices = []
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            index = int(token, 10)
        except ValueError:
            raise CapabilityError("invalid_config", "FEDCAP_GPU_DEVICE_ALLOWLIST contains a non-integer: %r" % token)
        if index < 0:
            raise CapabilityError("invalid_config", "FEDCAP_GPU_DEVICE_ALLOWLIST must not contain negative indices")
        if index not in devices:
            devices.append(index)
    if not devices:
        raise CapabilityError("invalid_config", "FEDCAP_GPU_DEVICE_ALLOWLIST is empty")
    return devices


def script_allowlist():
    """Absolute script paths approved for gpu.exec / gpu.job.submit.

    fedcap renders the manifest's array value through JavaScript ``String()``,
    so a configured list arrives either as ``/a,/b`` or as JSON array text; both
    forms are accepted here.
    """
    raw = os.environ.get("FEDCAP_GPU_SCRIPT_ALLOWLIST")
    if raw is None or not raw.strip():
        return []
    text = raw.strip()
    items = None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            items = [str(item) for item in parsed]
    if items is None:
        items = re.split(r"[,\n]", text)
    paths = []
    for item in items:
        item = item.strip().strip('"').strip("'").strip()
        if not item:
            continue
        item = os.path.expanduser(item)
        if not os.path.isabs(item):
            raise CapabilityError(
                "invalid_config",
                "FEDCAP_GPU_SCRIPT_ALLOWLIST entries must be absolute paths: %r" % item,
            )
        real = os.path.realpath(item)
        if real not in paths:
            paths.append(real)
    return paths


def ensure_dir(path, mode=0o700):
    os.makedirs(path, mode=mode, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def bounded_int(args, key, default, minimum, maximum):
    raw = args.get(key, default)
    value = None
    if isinstance(raw, bool):
        raise CapabilityError("invalid_input", "%s must be an integer" % key)
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            raise CapabilityError("invalid_input", "%s must be an integer" % key)
        value = int(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            value = int(text, 10)
        except ValueError:
            raise CapabilityError("invalid_input", "%s must be an integer" % key)
    else:
        raise CapabilityError("invalid_input", "%s must be an integer" % key)
    if value < minimum or value > maximum:
        raise CapabilityError("invalid_input", "%s must be between %d and %d" % (key, minimum, maximum))
    return value


def optional_device(args):
    if "device" not in args or args.get("device") is None:
        return None
    return bounded_int(args, "device", 0, 0, 63)


def json_object(args, key):
    value = args.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CapabilityError("invalid_input", "%s must be a JSON object" % key)
    return value


def script_params(args):
    """The JSON object handed to an allowlisted script on stdin.

    ``params`` is the documented field; ``args`` is accepted as an alias so that
    callers can follow the shorter ``action/script/args`` form.
    """
    value = args.get("params")
    if value is None:
        value = args.get("args")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CapabilityError("invalid_input", "params must be a JSON object")
    return value


def operation_key(args):
    raw = args.get("operationKey")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise CapabilityError("invalid_input", "operationKey must be a non-empty string")
    key = raw.strip()
    if not OPERATION_KEY_RE.match(key):
        raise CapabilityError(
            "invalid_input",
            "operationKey must match %s (no spaces)" % OPERATION_KEY_RE.pattern,
        )
    return key


def validated_job_id(raw):
    if not isinstance(raw, str) or not JOB_ID_RE.match(raw):
        raise CapabilityError("invalid_input", "jobId must match %s" % JOB_ID_RE.pattern)
    return raw


# ---------------------------------------------------------------------------
# nvidia-smi
# ---------------------------------------------------------------------------


def _run(argv, timeout_seconds=20):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_seconds)
    except FileNotFoundError:
        raise CapabilityError("nvidia_smi_unavailable", "nvidia-smi was not found on PATH")
    except subprocess.TimeoutExpired:
        raise CapabilityError("nvidia_smi_timeout", "nvidia-smi did not answer within %ss" % timeout_seconds)
    except OSError as exc:
        raise CapabilityError("nvidia_smi_unavailable", "cannot run nvidia-smi: %s" % exc)


def nvidia_smi_path():
    path = os.environ.get("NVIDIA_SMI_PATH") or shutil.which("nvidia-smi")
    if not path:
        raise CapabilityError("nvidia_smi_unavailable", "nvidia-smi was not found on PATH")
    return path


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def read_gpus():
    proc = _run(
        [
            nvidia_smi_path(),
            "--query-gpu=" + ",".join(GPU_FIELDS),
            "--format=csv,noheader,nounits",
        ]
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:300]
        raise CapabilityError("nvidia_smi_failed", "nvidia-smi exited %s: %s" % (proc.returncode, detail))
    gpus = []
    for line in proc.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != len(GPU_FIELDS):
            continue
        row = dict(zip(GPU_FIELDS, parts))
        gpus.append(
            {
                "index": _to_int(row["index"]),
                "name": row["name"],
                "uuid": row["uuid"],
                "memoryTotalMiB": _to_int(row["memory.total"]),
                "memoryUsedMiB": _to_int(row["memory.used"]),
                "memoryFreeMiB": _to_int(row["memory.free"]),
                "utilizationPct": _to_int(row["utilization.gpu"]),
                "temperatureC": _to_int(row["temperature.gpu"]),
                "driver": row["driver_version"],
            }
        )
    if not gpus:
        raise CapabilityError("nvidia_smi_failed", "nvidia-smi returned no GPU rows")
    for gpu in gpus:
        if gpu["index"] is None:
            raise CapabilityError("nvidia_smi_failed", "cannot parse the GPU index from nvidia-smi output")
        if gpu["memoryFreeMiB"] is None:
            gpu["memoryFreeMiB"] = 0
    return gpus


def read_compute_apps():
    proc = _run(
        [
            nvidia_smi_path(),
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ]
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:300]
        raise CapabilityError("nvidia_smi_failed", "nvidia-smi exited %s: %s" % (proc.returncode, detail))
    apps = []
    for line in proc.stdout.splitlines():
        parts = line.split(",", 2)
        if len(parts) < 3:
            continue
        apps.append(
            {
                "pid": _to_int(parts[0].strip()) if _to_int(parts[0].strip()) is not None else parts[0].strip(),
                "process": parts[1].strip(),
                "usedMiB": _to_int(parts[2].strip()),
            }
        )
    return apps


def select_device(requested, min_free_mib):
    """Pick an allowlisted GPU and enforce the free-VRAM floor.

    Returns ``(device_index, gpu_row)``. Without ``requested`` the allowlisted
    GPU with the most free VRAM wins, so a busy co-tenant card is avoided.
    """
    allowlist = device_allowlist()
    gpus = read_gpus()
    by_index = {gpu["index"]: gpu for gpu in gpus}
    missing = [index for index in allowlist if index not in by_index]
    if missing:
        raise CapabilityError(
            "invalid_config",
            "FEDCAP_GPU_DEVICE_ALLOWLIST references missing GPU index/indices: %s" % missing,
        )
    if requested is not None:
        if requested not in allowlist:
            raise CapabilityError(
                "invalid_input",
                "device %d is not in FEDCAP_GPU_DEVICE_ALLOWLIST %s" % (requested, allowlist),
            )
        candidates = [requested]
    else:
        candidates = list(allowlist)
    candidates.sort(key=lambda index: by_index[index]["memoryFreeMiB"], reverse=True)
    device = candidates[0]
    free = by_index[device]["memoryFreeMiB"]
    if free < min_free_mib:
        raise CapabilityError(
            "insufficient_vram",
            "GPU %d has %d MiB free; at least %d MiB required" % (device, free, min_free_mib),
        )
    return device, by_index[device]


# ---------------------------------------------------------------------------
# Per-device advisory lock
# ---------------------------------------------------------------------------


class DeviceLock(object):
    """Exclusive advisory lock for one GPU (serializes pack jobs per device).

    Other processes on the host are not coordinated by this lock; it only keeps
    this pack's own ``gpu.exec``/``gpu.job`` invocations from overlapping.
    """

    def __init__(self, device, job_root=None):
        self.device = device
        self.job_root = job_root or job_dir()
        self.path = os.path.join(self.job_root, "locks", "gpu%d.lock" % device)
        self.handle = None
        self.disabled = False

    def acquire(self):
        if fcntl is None:
            self.disabled = True
            log("warning: POSIX advisory locks are unavailable; per-device serialization is disabled")
            return
        ensure_dir(os.path.dirname(self.path))
        self.handle = open(self.path, "a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise CapabilityError(
                "device_busy",
                "GPU %d is busy: another gpu-nvidia job holds the lock (%s)" % (self.device, self.path),
            )
        try:
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(
                "%d %s\n" % (os.getpid(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            )
            self.handle.flush()
        except OSError:
            pass

    def release(self):
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except (OSError, AttributeError):
            pass
        try:
            self.handle.close()
        except OSError:
            pass
        self.handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False


# ---------------------------------------------------------------------------
# Bounded files and output
# ---------------------------------------------------------------------------


def read_limited(path, max_bytes, tail=False):
    """Read a text file, capped at ``max_bytes``. Never raises."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"text": "", "bytes": 0, "truncated": False}
    truncated = size > max_bytes
    try:
        with open(path, "rb") as handle:
            if tail and truncated:
                handle.seek(-max_bytes, os.SEEK_END)
            data = handle.read(max_bytes)
    except OSError:
        return {"text": "", "bytes": size, "truncated": truncated}
    text = data.decode("utf-8", "replace")
    if tail and truncated:
        text = text.lstrip("\ufffd")
    return {"text": text, "bytes": size, "truncated": truncated}


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def remove_tree(path):
    """Best-effort removal of a job directory this pack just created."""
    try:
        shutil.rmtree(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# JSON state helpers
# ---------------------------------------------------------------------------


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, payload, mode=0o600):
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), int(time.time() * 1000))
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def write_text_atomic(path, text, mode=0o600):
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), int(time.time() * 1000))
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def read_runner_pid(job_path):
    """Read the detached runner pid written by gpu.job.submit (may be absent)."""
    try:
        with open(os.path.join(job_path, "runner.pid"), "r", encoding="utf-8") as handle:
            return int(handle.read().strip(), 10)
    except (OSError, ValueError):
        return None


def new_job_id():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)


def operation_job(job_root, key):
    path = os.path.join(job_root, "operations", hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")
    record = read_json(path, None)
    if not isinstance(record, dict):
        return None
    job_id = record.get("jobId")
    return job_id if isinstance(job_id, str) else None


def reserve_operation(job_root, key, job_id):
    """Atomically claim an operationKey.

    Returns ``None`` when this call won the claim, or the jobId already bound to
    the key when another submission won. ``os.link`` is used so the record
    content is never observed half-written.
    """
    directory = ensure_dir(os.path.join(job_root, "operations"))
    path = os.path.join(directory, hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")
    record = {"operationKey": key, "jobId": job_id, "createdAt": int(time.time() * 1000)}
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), int(time.time() * 1000))
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        try:
            os.link(tmp, path)
            return None
        except OSError:
            existing = read_json(path, None)
            if isinstance(existing, dict) and isinstance(existing.get("jobId"), str):
                return existing["jobId"]
            raise CapabilityError(
                "operation_key_error",
                "cannot reserve operationKey %r: a conflicting record already exists" % key,
            )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def job_path(job_id):
    return os.path.join(job_dir(), "jobs", validated_job_id(job_id))


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def job_snapshot(job_id):
    """Load one job's meta/status, marking a dead runner as ``lost``."""
    path = job_path(job_id)
    if not os.path.isdir(path):
        raise CapabilityError("job_not_found", "no job with jobId %r" % job_id)
    meta = read_json(os.path.join(path, "meta.json"), {})
    if not isinstance(meta, dict):
        meta = {}
    status = read_json(os.path.join(path, "status.json"), {})
    if not isinstance(status, dict):
        status = {}
    state = status.get("state")
    if state in ("queued", "running"):
        runner = status.get("runnerPid") or meta.get("runnerPid")
        if runner is None:
            runner = read_runner_pid(path)
        if isinstance(runner, int) and not pid_alive(runner):
            status = dict(status)
            status["state"] = "lost"
            status["note"] = "runner process is gone before writing a final state"
    return path, meta, status


def snapshot_payload(job_id, meta, status):
    payload = {
        "jobId": job_id,
        "state": status.get("state"),
        "exitCode": status.get("exitCode"),
        "timedOut": bool(status.get("timedOut", False)),
        "createdAt": meta.get("createdAt"),
        "startedAt": status.get("startedAt"),
        "finishedAt": status.get("finishedAt"),
        "durationMs": status.get("durationMs"),
        "device": meta.get("device"),
        "script": meta.get("script"),
        "timeoutSec": meta.get("timeoutSec"),
        "operationKey": meta.get("operationKey"),
    }
    for key in ("error", "detail", "note"):
        if status.get(key) is not None:
            payload[key] = status[key]
    return payload


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def child_env(device=None, extra=None):
    """A scrubbed environment for allowlisted scripts and jobs.

    Secrets from the pack user's interactive shell are deliberately not
    inherited; set values an allowlisted script needs through ``extra`` only.
    """
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
    }
    home = os.environ.get("HOME")
    if home:
        env["HOME"] = home
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        env["TMPDIR"] = tmpdir
    if device is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(device)
    if extra:
        env.update(extra)
    return env


def _kill_process_group(proc, grace_seconds=5.0):
    """Terminate only the child's own process group, never other processes."""
    if os.name != "posix":
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
    else:
        try:
            proc.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    else:
        try:
            proc.kill()
        except OSError:
            pass


def run_subprocess(argv, stdout_path, stderr_path, cwd, timeout_seconds, env, stdin_bytes=None):
    """Run a child with a hard timeout, capturing output to files.

    The child gets its own session/process group on POSIX so a timeout kills the
    whole job subtree and nothing else.
    """
    started = time.monotonic()
    timed_out = False
    error = None
    exit_code = None
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        kwargs = {
            "stdin": subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            "stdout": out,
            "stderr": err,
            "cwd": cwd,
            "env": env,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            return {
                "exitCode": None,
                "timedOut": False,
                "elapsedMs": int((time.monotonic() - started) * 1000),
                "error": "cannot start %r: %s" % (argv[0], exc),
            }
        try:
            if stdin_bytes is not None:
                proc.communicate(input=stdin_bytes, timeout=timeout_seconds)
            else:
                proc.communicate(timeout=timeout_seconds)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001 - best-effort reap
                pass
            exit_code = proc.returncode
            error = "timed out after %ss" % timeout_seconds
    return {
        "exitCode": exit_code,
        "timedOut": timed_out,
        "elapsedMs": int((time.monotonic() - started) * 1000),
        "error": error,
    }


def launch_background(argv, stdout_path, stderr_path, cwd, env=None):
    """Start a detached child (used once by gpu.job.submit)."""
    with open(stdout_path, "ab") as out, open(stderr_path, "ab") as err:
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": out,
            "stderr": err,
            "cwd": cwd,
            "env": env if env is not None else child_env(),
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
    return {"pid": proc.pid}


def allowed_script(raw_script):
    """Validate one absolute script path against the configured allowlist."""
    if not isinstance(raw_script, str) or not raw_script.strip():
        raise CapabilityError("invalid_input", "script must be a non-empty absolute path")
    path = os.path.expanduser(raw_script.strip())
    if not os.path.isabs(path):
        raise CapabilityError("invalid_input", "script must be an absolute path")
    allowlist = script_allowlist()
    if not allowlist:
        raise CapabilityError(
            "script_not_allowed",
            "script execution is disabled: FEDCAP_GPU_SCRIPT_ALLOWLIST is empty",
        )
    real = os.path.realpath(path)
    if real not in allowlist:
        raise CapabilityError(
            "script_not_allowed",
            "script is not in FEDCAP_GPU_SCRIPT_ALLOWLIST: %s" % real,
        )
    if not os.path.isfile(real):
        raise CapabilityError("script_not_allowed", "allowlisted script does not exist: %s" % real)
    try:
        stat = os.stat(real)
    except OSError as exc:
        raise CapabilityError("script_not_allowed", "cannot stat allowlisted script: %s" % exc)
    if stat.st_mode & 0o002:
        raise CapabilityError(
            "script_not_allowed",
            "allowlisted script is world-writable; chmod it to 0755: %s" % real,
        )
    if hasattr(os, "getuid") and stat.st_uid != os.getuid():
        log("warning: allowlisted script is not owned by the pack user: %s" % real)
    return real
