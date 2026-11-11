#!/usr/bin/env python3
"""gpu.job.status - poll one background job submitted with gpu.job.submit.

Args (stdin JSON):
  jobId : string returned by gpu.job.submit

Returns the job state (``queued`` | ``running`` | ``succeeded`` | ``failed`` |
``timeout`` | ``lost``), exit code, device, script and timing metadata. This
capability only reads the job directory; it never touches the job process.
"""

import os
import sys
import time

import _common as common


def main():
    started = time.monotonic()
    result = {
        "ok": False,
        "pack": common.PACK_ID,
        "version": common.VERSION,
        "ts": int(time.time() * 1000),
    }
    try:
        args = common.read_stdin_args()
        job_id = common.validated_job_id(args.get("jobId"))
        path, meta, status = common.job_snapshot(job_id)
        result.update(common.snapshot_payload(job_id, meta, status))
        result["jobDir"] = path
        result["stdoutBytes"] = common.file_size(os.path.join(path, "script.stdout"))
        result["stderrBytes"] = common.file_size(os.path.join(path, "script.stderr"))
        result["ok"] = True
    except common.CapabilityError as exc:
        result["error"] = exc.code
        result["detail"] = str(exc)
    result["elapsedMs"] = int((time.monotonic() - started) * 1000)
    return result


if __name__ == "__main__":
    sys.exit(common.entry(main))
