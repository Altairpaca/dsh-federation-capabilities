#!/usr/bin/env python3
"""gpu.job.submit - submit one allowlisted python script as a background job.

Args (stdin JSON):
  action       : "python_script" (only supported job action)
  script       : absolute path listed in FEDCAP_GPU_SCRIPT_ALLOWLIST
  params       : JSON object passed to the script on stdin (default {})
  device       : optional GPU index from the device allowlist
  timeoutSec   : 1..3600 hard job timeout (default 600)
  minFreeMiB   : free-VRAM floor checked before submission (default 1024)
  operationKey : optional idempotency key; resubmitting the same key returns the
                 original job instead of starting a second one

The job directory lives under ``FEDCAP_GPU_JOB_DIR/jobs/<jobId>`` and contains
meta.json, params.json, status.json, runner.pid, script.stdout, script.stderr,
the detached runner log and a private ``work/`` directory. The submit call
returns state ``queued``; the runner flips it to ``running`` and then to a final
state. Use gpu.job.status to poll and gpu.job.result to read the captured output.
"""

import json
import os
import sys
import time

import _common as common

ACTION = "python_script"
DEFAULT_TIMEOUT_SECONDS = 600
MAX_JOB_TIMEOUT_SECONDS = 3600
DEFAULT_MIN_FREE_MIB = 1024


def _duplicate_payload(job_id):
    try:
        _path, meta, status = common.job_snapshot(job_id)
    except common.CapabilityError:
        raise common.CapabilityError(
            "operation_key_stale",
            "operationKey is already bound to job %r but that job directory is gone; "
            "use a new operationKey" % job_id,
        )
    payload = {
        "ok": True,
        "duplicate": True,
        "jobId": job_id,
        "jobDir": common.job_path(job_id),
    }
    payload.update(common.snapshot_payload(job_id, meta, status))
    payload["ok"] = True
    payload["duplicate"] = True
    return payload


def main():
    started = time.monotonic()
    result = {
        "ok": False,
        "pack": common.PACK_ID,
        "version": common.VERSION,
        "ts": int(time.time() * 1000),
    }
    lock = None
    try:
        args = common.read_stdin_args()
        action = args.get("action", ACTION)
        if action != ACTION:
            raise common.CapabilityError("invalid_input", "action must be %r" % ACTION)
        script = common.allowed_script(args.get("script"))
        params = common.script_params(args)
        params_blob = json.dumps(params, ensure_ascii=False).encode("utf-8")
        if len(params_blob) > common.MAX_ARGS_BYTES:
            raise common.CapabilityError(
                "invalid_input", "params JSON is larger than %d bytes" % common.MAX_ARGS_BYTES
            )
        requested = common.optional_device(args)
        timeout_seconds = common.bounded_int(
            args, "timeoutSec", DEFAULT_TIMEOUT_SECONDS, 1, MAX_JOB_TIMEOUT_SECONDS
        )
        min_free_mib = common.bounded_int(args, "minFreeMiB", DEFAULT_MIN_FREE_MIB, 0, 65536)
        key = common.operation_key(args)

        job_root = common.job_dir()
        if key:
            existing = common.operation_job(job_root, key)
            if existing is not None:
                return _duplicate_payload(existing)

        device, gpu = common.select_device(requested, min_free_mib)

        # Fail fast when another pack job already owns the device; the detached
        # runner re-acquires the lock for the actual job lifetime.
        lock = common.DeviceLock(device)
        lock.acquire()
        lock.release()
        lock = None

        job_id = common.new_job_id()
        job_path = common.ensure_dir(os.path.join(job_root, "jobs", job_id), 0o700)
        work_dir = common.ensure_dir(os.path.join(job_path, "work"), 0o700)
        created_at = int(time.time() * 1000)
        meta = {
            "jobId": job_id,
            "action": action,
            "script": script,
            "device": device,
            "timeoutSec": timeout_seconds,
            "minFreeMiB": min_free_mib,
            "python": sys.executable,
            "operationKey": key,
            "createdAt": created_at,
            "workDir": work_dir,
        }
        common.write_json_atomic(os.path.join(job_path, "meta.json"), meta, 0o600)
        common.write_json_atomic(os.path.join(job_path, "params.json"), params, 0o600)

        if key:
            winner = common.reserve_operation(job_root, key, job_id)
            if winner is not None:
                common.remove_tree(job_path)
                return _duplicate_payload(winner)

        common.write_json_atomic(
            os.path.join(job_path, "status.json"),
            {"state": "queued", "createdAt": created_at},
            0o600,
        )
        runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_job_runner.py")
        launched = common.launch_background(
            [sys.executable, runner, job_root, job_id, str(timeout_seconds)],
            os.path.join(job_path, "runner.log"),
            os.path.join(job_path, "runner.err"),
            work_dir,
        )
        # The runner owns status.json from here on; write only its pid so a very
        # fast job's final state can never be overwritten by the submit call.
        common.write_text_atomic(os.path.join(job_path, "runner.pid"), "%d\n" % launched["pid"])

        result.update(
            {
                "ok": True,
                "duplicate": False,
                "jobId": job_id,
                "jobDir": job_path,
                "state": "queued",
                "action": action,
                "device": device,
                "requestedDevice": requested,
                "deviceFreeMiB": gpu["memoryFreeMiB"],
                "script": script,
                "timeoutSec": timeout_seconds,
                "operationKey": key,
            }
        )
    except common.CapabilityError as exc:
        result["error"] = exc.code
        result["detail"] = str(exc)
    finally:
        if lock is not None:
            lock.release()
    result["elapsedMs"] = int((time.monotonic() - started) * 1000)
    return result


if __name__ == "__main__":
    sys.exit(common.entry(main))
