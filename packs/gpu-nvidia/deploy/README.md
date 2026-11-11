# Deploying `gpu-nvidia` on a shared GPU host

This pack is designed for multi-tenant GPU servers where some cards are busy
with other work. The goal is to add mesh capabilities **without** disturbing
those jobs: use only reviewed devices, check free memory before every run,
serialize the pack's own work per card and keep every response small.

## Shared-host safety boundaries

| Boundary | Default | Where it is enforced |
| --- | --- | --- |
| Device selection | `deviceAllowlist = "0,2"` | `gpu_smoke.py`, `gpu_exec.py`, `gpu_job_submit.py`; a requested index outside the list is rejected. |
| Free VRAM floor | `minFreeMiB = 1024` (scripts/jobs), matmul footprint (smoke) | Checked against `nvidia-smi` before the child starts. |
| Per-card serialization | one advisory `flock` per GPU | `DeviceLock`; `gpu.exec` fails fast with `device_busy`, a background job holds it for its lifetime. |
| Script execution | `scriptAllowlist = []` (disabled) | Absolute paths only; must exist, must not be world-writable, must be listed. |
| Working directory | under `jobDir` | `work/<id>` and `jobs/<jobId>/work`; nothing is written outside `jobDir` by the pack. |
| Run time | `gpu.exec` <= 600 s, `gpu.job` <= 3600 s | Hard timeout; the job's own process group is terminated. |
| Response size | < 64 KiB | stdout/stderr embed caps with `truncated` flags. |
| Process safety | never signal unrelated processes | Only the spawned child's process group is killed; the pack never runs `sudo`. |

What the pack deliberately does **not** provide: a container/sandbox, exclusive
GPU access, coordination with other tenants, automatic job cleanup, or a
network policy. An allowlisted script runs with the pack user's permissions.

## Deployment checklist

1. **Inspect the host and pick devices.**

   ```bash
   nvidia-smi
   nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu \
     --format=csv,noheader
   nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv
   ```

   Pick indices that are idle or have enough free VRAM for the intended work.
   Put only those indices in `deviceAllowlist`. The default `0,2` is a
   convenient starting point on a four-card host, not a universal rule.

2. **Create a job directory on a data disk.**

   ```bash
   install -d -m 0700 /opt/fedcap/jobs
   ```

   The directory must be writable by the user that runs the mesh worker and
   must live on a filesystem with room for scripts, logs and temporary files —
   not on a small root filesystem. `~/.fedcap/jobs` (the default) is fine for a
   single-tenant host.

3. **Choose the interpreter.** `python3` from the OS is enough for
   `gpu.info`/`gpu.exec`/`gpu.job.*`. For `gpu.smoke`, point the pack at a venv
   with a CUDA build of torch:

   ```bash
   /opt/fedcap/venv/bin/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
   ```

   If that prints `False`, `gpu.smoke` will report `cuda_unavailable`.

4. **Stage allowlisted scripts.**

   ```bash
   install -d -m 0755 /opt/fedcap/scripts
   install -m 0755 ./train.py /opt/fedcap/scripts/train.py
   chown "$(id -u):$(id -g)" /opt/fedcap/scripts/train.py
   ```

   Scripts must be absolute paths, exist at call time and must not be
   world-writable (`chmod o-w`). They receive `args.params` as one JSON object
   on stdin and should write bounded logs to stdout/stderr.

5. **Install the pack** (values stay on this host, never in git):

   ```bash
   node packages/fedcap/bin/fedcap.js validate packs/gpu-nvidia
   node packages/fedcap/bin/fedcap.js install \
     --pack packs/gpu-nvidia \
     --node-config ~/.mesh/node.json \
     --set python=/opt/fedcap/venv/bin/python \
     --set jobDir=/opt/fedcap/jobs \
     --set deviceAllowlist=0,2 \
     --set scriptAllowlist=/opt/fedcap/scripts/train.py
   node packages/fedcap/bin/fedcap.js doctor --node-config ~/.mesh/node.json
   ```

6. **Smoke it before wiring it into the mesh.**

   ```bash
   echo '{}' | mesh-node invoke --capability gpu.info --args-json -
   echo '{"size": 4096, "dtype": "float32"}' | mesh-node invoke --capability gpu.smoke --args-json -
   echo '{"action": "nvidia_smi_query", "query": "processes"}' \
     | mesh-node invoke --capability gpu.exec --args-json -
   echo '{"action": "python_script", "script": "/opt/fedcap/scripts/train.py", "params": {"steps": 5}}' \
     | mesh-node invoke --capability gpu.exec --args-json -
   ```

   Then run one short background job and poll it:

   ```bash
   echo '{"action": "python_script", "script": "/opt/fedcap/scripts/train.py",
          "params": {"steps": 20}, "timeoutSec": 120, "operationKey": "deploy-check-1"}' \
     | mesh-node invoke --capability gpu.job.submit --args-json -
   echo '{"jobId": "REPLACE_ME"}' | mesh-node invoke --capability gpu.job.status --args-json -
   echo '{"jobId": "REPLACE_ME", "tailBytes": 8192}' | mesh-node invoke --capability gpu.job.result --args-json -
   ```

7. **Expose it privately.** The worker is normally reached over SSH
   (`transport: ssh` in the hub node entry) or a private mesh HTTP endpoint with
   a bearer token. Do not publish the mesh port on the public internet: every
   allowlisted script is reachable through it.

## Example node configuration fragment

After installation the pack writes `capabilities.d/gpu-nvidia.json` next to the
node configuration and appends it to `capabilitiesFile`:

```jsonc
{
  "nodeId": "gpu-worker",
  "kind": "custom",
  "capabilitiesFile": ["capabilities.d/gpu-nvidia.json"],
  "stateDir": "~/.mesh/state"
}
```

An SSH transport entry on the hub side looks like this (values are sanitized
placeholders):

```yaml
- id: gpu-worker
  title: GPU worker
  transport: ssh
  sshHost: gpu-worker.example.invalid
  sshUser: REPLACE_ME
  nodeBin: /opt/fedcap/bin/mesh-node.js
  capabilities: [gpu.info, gpu.smoke, gpu.exec, gpu.job.submit, gpu.job.status, gpu.job.result]
```

## Disk, cleanup and retention

- Every `gpu.exec` call creates one directory under `jobDir/work/`; each job
  gets `jobDir/jobs/<jobId>/` with `meta.json`, `params.json`, `status.json`,
  `runner.pid`, `script.stdout`, `script.stderr` and its `work/` tree.
- `status.json` is owned by the detached runner: `gpu.job.submit` only writes
  `queued` plus `runner.pid`, so a job that finishes immediately can never be
  reported back as "running".
- The pack never deletes job data. Schedule your own cleanup, for example:

  ```bash
  find /opt/fedcap/jobs/jobs -mindepth 1 -maxdepth 1 -type d -mtime +14 -print
  # review the list, then delete the stale directories yourself
  ```

- `jobDir/operations/` holds one small JSON record per `operationKey`; it is safe
  to keep, but large retention means large directories.
- Keep `jobDir` at mode 0700: job output can contain data you do not want other
  local users to read.

## Failure modes on a shared host

| Symptom | What to check |
| --- | --- |
| `device_busy` | Another pack job or `gpu.exec` holds the card; check `gpu.job.status` / `jobDir/locks/`. |
| `insufficient_vram` | Co-tenant allocated memory after your preflight or the card is busy; lower `minFreeMiB` or pick the other allowlisted card. |
| job ends with CUDA out-of-memory | The preflight is not a reservation; make the script batch smaller or retry on the other allowlisted card. |
| `script_not_allowed` | Path not allowlisted, allowlist empty, file missing, or the file is world-writable. |
| `state: "lost"` | The detached runner vanished (host reboot, OOM killer, manual cleanup). Inspect `runner.err` and `script.stderr`, then resubmit. |
| worker hangs on `gpu.info` | `nvidia-smi` itself is stuck (driver/Xid); run it manually on the host to confirm. |

## Security boundary, restated

The trust boundary is the mesh transport and the script allowlist:

1. Only the mesh hub may invoke capabilities; the worker should not be publicly
   reachable.
2. Only operator-approved absolute scripts run, never arbitrary shell text.
3. Everything the script can do, the hub can do through it — treat a compromised
   hub and a compromised allowlisted script as the same incident.
4. The pack is not an isolation layer. If you need stronger guarantees, run the
   worker in a container/VM with its own GPU partition (MIG or a dedicated card)
   and put the allowlist inside that boundary.
