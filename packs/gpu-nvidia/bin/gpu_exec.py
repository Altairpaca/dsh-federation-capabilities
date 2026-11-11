#!/usr/bin/env python3
"""gpu.exec - run one whitelisted action on this GPU host.

Actions (``args.action``):
  * ``nvidia_smi_query`` - a fixed nvidia-smi query (``args.query``:
    ``summary`` | ``list`` | ``processes``); read-only, no device lock.
  * ``python_script``    - run an absolute script path that the operator listed
    in ``FEDCAP_GPU_SCRIPT_ALLOWLIST``; the script gets the JSON ``args.params``
    object on stdin. The GPU is pinned with ``CUDA_VISIBLE_DEVICES``, a
    per-device advisory lock is held for the run, and free VRAM is checked
    before starting.

There is deliberately no action that evaluates a shell string, ``-c`` code or
a URL. Captured stdout is capped so one response stays below 64 KiB.
"""

import json
import os
import sys
import time

import _common as common

ACTIONS = ("nvidia_smi_query", "python_script")
QUERIES = {
    "summary": [
        "--query-gpu=index,name,uuid,driver_version,memory.total,memory.used,"
        "memory.free,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
    ],
    "list": ["-L"],
    "processes": [
        "--query-compute-apps=pid,process_name,gpu_uuid,used_memory",
        "--format=csv,noheader,nounits",
    ],
}


def _nvidia_query(query, work_dir, timeout_seconds):
    stdout_path = os.path.join(work_dir, "nvidia-smi.stdout")
    stderr_path = os.path.join(work_dir, "nvidia-smi.stderr")
    run = common.run_subprocess(
        [common.nvidia_smi_path()] + QUERIES[query],
        stdout_path,
        stderr_path,
        work_dir,
        timeout_seconds,
        common.child_env(),
    )
    payload = {
        "query": query,
        "exitCode": run["exitCode"],
        "timedOut": run["timedOut"],
        "elapsedMs": run["elapsedMs"],
        "stdout": common.read_limited(stdout_path, common.MAX_STDOUT_EMBED),
        "stderr": common.read_limited(stderr_path, common.MAX_STDERR_EMBED),
    }
    if run.get("error"):
        payload["detail"] = run["error"]
    payload["ok"] = run["exitCode"] == 0 and not run["timedOut"]
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
        action = args.get("action")
        if action not in ACTIONS:
            raise common.CapabilityError("invalid_input", "action must be one of %s" % (list(ACTIONS),))
        timeout_seconds = common.bounded_int(args, "timeoutSec", 120, 1, 600)

        work_root = os.path.join(common.job_dir(), "work", common.new_job_id())
        common.ensure_dir(work_root)

        if action == "nvidia_smi_query":
            query = args.get("query", "summary")
            if query not in QUERIES:
                raise common.CapabilityError("invalid_input", "query must be one of %s" % (sorted(QUERIES),))
            result["action"] = action
            result["workDir"] = work_root
            result.update(_nvidia_query(query, work_root, min(timeout_seconds, 30)))
        else:
            requested = common.optional_device(args)
            min_free_mib = common.bounded_int(args, "minFreeMiB", 1024, 0, 65536)
            script = common.allowed_script(args.get("script"))
            params = common.script_params(args)
            params_blob = json.dumps(params, ensure_ascii=False).encode("utf-8")
            if len(params_blob) > common.MAX_ARGS_BYTES:
                raise common.CapabilityError(
                    "invalid_input", "params JSON is larger than %d bytes" % common.MAX_ARGS_BYTES
                )

            device, gpu = common.select_device(requested, min_free_mib)
            result["action"] = action
            result["device"] = device
            result["requestedDevice"] = requested
            result["deviceFreeMiB"] = gpu["memoryFreeMiB"]
            result["workDir"] = work_root

            lock = common.DeviceLock(device)
            lock.acquire()
            result["locked"] = True

            stdout_path = os.path.join(work_root, "script.stdout")
            stderr_path = os.path.join(work_root, "script.stderr")
            run = common.run_subprocess(
                [sys.executable, script],
                stdout_path,
                stderr_path,
                work_root,
                timeout_seconds,
                common.child_env(device),
                params_blob,
            )
            result["script"] = script
            result["exitCode"] = run["exitCode"]
            result["timedOut"] = run["timedOut"]
            result["runMs"] = run["elapsedMs"]
            result["stdout"] = common.read_limited(stdout_path, common.MAX_STDOUT_EMBED)
            result["stderr"] = common.read_limited(stderr_path, common.MAX_STDERR_EMBED)
            if run.get("error"):
                result["detail"] = run["error"]
            result["ok"] = run["exitCode"] == 0 and not run["timedOut"]
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
