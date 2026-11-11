#!/usr/bin/env python3
"""gpu.info - read-only NVIDIA GPU inventory for one mesh host.

Stdin : {} (no arguments).
Stdout: one JSON object with GPU rows, compute processes, the configured
        device allowlist and per-GPU ``allowed`` flags.
"""

import socket
import sys
import time

import _common as common


def main():
    started = time.monotonic()
    result = {
        "ok": False,
        "pack": common.PACK_ID,
        "version": common.VERSION,
        "host": socket.gethostname(),
        "ts": int(time.time() * 1000),
    }
    try:
        common.read_stdin_args()
        allowlist = common.device_allowlist()
        result["deviceAllowlist"] = allowlist
        gpus = common.read_gpus()
        for gpu in gpus:
            gpu["allowed"] = gpu["index"] in allowlist
        result["count"] = len(gpus)
        result["gpus"] = gpus
        result["ok"] = True
    except common.CapabilityError as exc:
        result["error"] = exc.code
        result["detail"] = str(exc)
        result["elapsedMs"] = int((time.monotonic() - started) * 1000)
        return result

    try:
        result["computeApps"] = common.read_compute_apps()
    except common.CapabilityError as exc:
        # GPU inventory is still useful when the process query is unavailable.
        result["computeApps"] = []
        result["computeAppsError"] = str(exc)

    result["elapsedMs"] = int((time.monotonic() - started) * 1000)
    return result


if __name__ == "__main__":
    sys.exit(common.entry(main))
