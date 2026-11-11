#!/usr/bin/env python3
"""Internal background runner used by gpu.job.submit.

Usage: python _job_runner.py <jobRoot> <jobId> <timeoutSeconds>

The runner is started detached by ``gpu_job_submit.py``. It holds the per-device
advisory lock for the whole job, runs the allowlisted script with a scrubbed
environment and ``CUDA_VISIBLE_DEVICES`` pinned, captures the job's stdout and
stderr to files and writes the final ``status.json``. It is not part of the
public capability surface and is never invoked by a mesh node directly.
"""

import json
import os
import sys
import time

import _common as common


def _write_status(path, payload):
    common.write_json_atomic(path, payload, 0o600)


def main():
    if len(sys.argv) != 4:
        common.log("usage: _job_runner.py <jobRoot> <jobId> <timeoutSeconds>")
        return 2
    job_root = os.path.abspath(sys.argv[1])
    job_id = sys.argv[2]
    try:
        timeout_seconds = int(sys.argv[3], 10)
    except ValueError:
        common.log("timeoutSeconds must be an integer: %r" % sys.argv[3])
        return 2

    job_path = os.path.join(job_root, "jobs", job_id)
    meta = common.read_json(os.path.join(job_path, "meta.json"), None)
    if not isinstance(meta, dict):
        common.log("meta.json missing or invalid for job %s" % job_id)
        return 1
    status_path = os.path.join(job_path, "status.json")
    device = meta.get("device")
    started_at = int(time.time() * 1000)
    started = time.monotonic()
    lock = None

    try:
        if isinstance(device, int):
            lock = common.DeviceLock(device, job_root)
            try:
                lock.acquire()
            except common.CapabilityError as exc:
                _write_status(
                    status_path,
                    {
                        "state": "failed",
                        "error": "device_busy",
                        "detail": str(exc),
                        "startedAt": started_at,
                        "finishedAt": int(time.time() * 1000),
                    },
                )
                return 1

        _write_status(status_path, {"state": "running", "runnerPid": os.getpid(), "startedAt": started_at})

        params = common.read_json(os.path.join(job_path, "params.json"), {})
        if not isinstance(params, dict):
            params = {}
        params_blob = json.dumps(params, ensure_ascii=False).encode("utf-8")
        python = meta.get("python") or sys.executable
        script = meta.get("script")
        if not isinstance(script, str) or not os.path.isfile(script):
            _write_status(
                status_path,
                {
                    "state": "failed",
                    "error": "script_not_found",
                    "detail": "allowlisted script no longer exists: %r" % script,
                    "startedAt": started_at,
                    "finishedAt": int(time.time() * 1000),
                },
            )
            return 1

        run = common.run_subprocess(
            [python, script],
            os.path.join(job_path, "script.stdout"),
            os.path.join(job_path, "script.stderr"),
            os.path.join(job_path, "work"),
            timeout_seconds,
            common.child_env(device if isinstance(device, int) else None),
            params_blob,
        )

        finished_at = int(time.time() * 1000)
        if run["timedOut"]:
            state = "timeout"
        elif run["exitCode"] == 0:
            state = "succeeded"
        else:
            state = "failed"
        payload = {
            "state": state,
            "exitCode": run["exitCode"],
            "timedOut": run["timedOut"],
            "startedAt": started_at,
            "finishedAt": finished_at,
            "durationMs": int((time.monotonic() - started) * 1000),
        }
        if run.get("error"):
            payload["detail"] = run["error"]
        _write_status(status_path, payload)
        return 0
    except Exception as exc:  # noqa: BLE001 - a broken runner must still record a state
        common.log("job %s crashed: %s: %s" % (job_id, type(exc).__name__, exc))
        try:
            _write_status(
                status_path,
                {
                    "state": "failed",
                    "error": "runner_crashed",
                    "detail": "%s: %s" % (type(exc).__name__, exc),
                    "startedAt": started_at,
                    "finishedAt": int(time.time() * 1000),
                },
            )
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
