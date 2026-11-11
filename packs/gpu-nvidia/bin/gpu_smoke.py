#!/usr/bin/env python3
"""gpu.smoke - bounded CUDA matmul health probe.

Args (stdin JSON):
  size   : 256..8192   square matrix edge (default 4096)
  dtype  : "float32" | "float16" (default "float32")
  device : optional GPU index from the configured device allowlist

The allowlisted GPU with the most free VRAM is used unless ``device`` is given.
torch is optional: when the configured interpreter cannot import it, the probe
returns ``ok:false`` JSON (``torch_unavailable``) instead of failing the shell
capability. No model is loaded and GPU work is bounded to a few milliseconds.
"""

import os
import sys
import time

import _common as common

DEFAULT_SIZE = 4096
MIN_SIZE = 256
MAX_SIZE = 8192
DTYPES = ("float32", "float16")


def _run_matmul(size, dtype_name, device, need_mib, result):
    # Pin the physical device before torch initialises CUDA.
    os.environ["CUDA_VISIBLE_DEVICES"] = str(device)
    try:
        import torch
    except ImportError as exc:
        raise common.CapabilityError(
            "torch_unavailable",
            "the configured python cannot import torch (%s); install torch or point "
            "config python at a torch-enabled interpreter" % exc,
        )

    result["torch"] = torch.__version__
    result["cudaAvailable"] = bool(torch.cuda.is_available())
    if not torch.cuda.is_available():
        raise common.CapabilityError("cuda_unavailable", "torch.cuda.is_available() is false")

    visible = torch.cuda.device_count()
    result["visibleDevices"] = visible
    if visible < 1:
        raise common.CapabilityError("cuda_unavailable", "torch sees no CUDA device")

    props = torch.cuda.get_device_properties(0)
    free_bytes, total_bytes = torch.cuda.mem_get_info()
    result["deviceName"] = props.name
    result["deviceTotalMiB"] = int(total_bytes / 1048576)
    result["deviceFreeMiB"] = int(free_bytes / 1048576)
    result["multiProcessorCount"] = props.multi_processor_count
    if free_bytes < need_mib * 1048576:
        raise common.CapabilityError(
            "insufficient_vram",
            "GPU %d has %d MiB free after CUDA init; need %d MiB"
            % (device, int(free_bytes / 1048576), need_mib),
        )

    dtype = torch.float16 if dtype_name == "float16" else torch.float32
    torch.manual_seed(0)
    left = torch.randn((size, size), device="cuda", dtype=dtype)
    right = torch.randn((size, size), device="cuda", dtype=dtype)
    torch.cuda.synchronize()
    torch.matmul(left, right)  # warm-up
    torch.cuda.synchronize()

    timings = []
    product = None
    for _ in range(3):
        tick = time.monotonic()
        product = torch.matmul(left, right)
        torch.cuda.synchronize()
        timings.append((time.monotonic() - tick) * 1000.0)

    result["finite"] = bool(torch.isfinite(product).all().item())
    result["timingsMs"] = [round(value, 2) for value in timings]
    result["gflops"] = round(2 * size ** 3 / (min(timings) / 1000.0) / 1e9, 1)
    result["ok"] = result["finite"]


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
        size = common.bounded_int(args, "size", DEFAULT_SIZE, MIN_SIZE, MAX_SIZE)
        dtype_name = args.get("dtype", "float32")
        if dtype_name not in DTYPES:
            raise common.CapabilityError("invalid_input", "dtype must be one of %s" % (list(DTYPES),))
        requested = common.optional_device(args)

        bytes_per_element = 2 if dtype_name == "float16" else 4
        # matmul needs left + right + product, plus CUDA workspace headroom.
        need_mib = max(1024, int((size * size * bytes_per_element * 3) // 1048576) + 256)
        result["size"] = size
        result["dtype"] = dtype_name
        result["needMiB"] = need_mib

        device, gpu = common.select_device(requested, need_mib)
        result["device"] = device
        result["requestedDevice"] = requested
        result["deviceFreeMiB"] = gpu["memoryFreeMiB"]
        _run_matmul(size, dtype_name, device, need_mib, result)
    except common.CapabilityError as exc:
        result["error"] = exc.code
        result["detail"] = str(exc)
    result["elapsedMs"] = int((time.monotonic() - started) * 1000)
    return result


if __name__ == "__main__":
    sys.exit(common.entry(main))
