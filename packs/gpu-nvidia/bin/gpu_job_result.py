#!/usr/bin/env python3
"""gpu.job.result - read the captured output of a background job.

Args (stdin JSON):
  jobId     : string returned by gpu.job.submit
  tailBytes : 1024..65536, how much of each stream to return (default 32768)

Returns the job status plus the tail of stdout and stderr. Both streams are
capped so the whole response stays below 64 KiB; ``truncated`` flags say
whether older bytes were dropped. A running job returns its output so far.
"""

import os
import sys
import time

import _common as common

DEFAULT_TAIL_BYTES = 32 * 1024
MIN_TAIL_BYTES = 1024
MAX_TAIL_BYTES = 64 * 1024


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
        tail_bytes = common.bounded_int(
            args, "tailBytes", DEFAULT_TAIL_BYTES, MIN_TAIL_BYTES, MAX_TAIL_BYTES
        )
        path, meta, status = common.job_snapshot(job_id)
        result.update(common.snapshot_payload(job_id, meta, status))
        result["jobDir"] = path
        result["workDir"] = meta.get("workDir")
        embedded = min(tail_bytes, common.MAX_JOB_STREAM_EMBED)
        result["stdout"] = common.read_limited(os.path.join(path, "script.stdout"), embedded, tail=True)
        result["stderr"] = common.read_limited(os.path.join(path, "script.stderr"), common.MAX_STDERR_EMBED, tail=True)
        result["ok"] = True
    except common.CapabilityError as exc:
        result["error"] = exc.code
        result["detail"] = str(exc)
    result["elapsedMs"] = int((time.monotonic() - started) * 1000)
    return result


if __name__ == "__main__":
    sys.exit(common.entry(main))
