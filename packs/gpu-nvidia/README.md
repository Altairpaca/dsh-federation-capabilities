# gpu-nvidia capability pack

Expose one NVIDIA GPU host to a DSH Federation mesh as four capability
families: inventory (`gpu.info`), a bounded compute health probe (`gpu.smoke`),
a whitelisted short-task runner (`gpu.exec`) and detached background jobs
(`gpu.job.*`). Everything is a fixed shell command over the standard pack
contract — one JSON object on stdin, one JSON object on stdout, diagnostics on
stderr — with no third-party Python dependencies.

The pack is meant for shared GPU machines: it never executes an arbitrary shell
string, pins every GPU run with `CUDA_VISIBLE_DEVICES`, checks free VRAM first,
serializes its own per-device work with an advisory lock and caps every response
below 64 KiB.

## Requirements

| Requirement | Notes |
| --- | --- |
| Linux host with the NVIDIA driver | `nvidia-smi` must be on `PATH` (or set `NVIDIA_SMI_PATH`). |
| Python 3.8+ | Standard library only. `torch` is optional and used **only** by `gpu.smoke`. |
| dsh-federation >= 0.8.0 | Capability descriptors plus `capabilitiesFile`. |
| `fedcap` v0.1 | `node packages/fedcap/bin/fedcap.js` in this repository. |

If the configured interpreter cannot import `torch`, `gpu.smoke` returns
`ok:false` with `error: "torch_unavailable"` instead of failing the mesh call.
On non-POSIX platforms advisory locking is skipped with a stderr warning; the
pack is supported on Linux only.

## Capabilities

| Id | Action / arguments | Idempotent | Timeout | GPU side effects |
| --- | --- | --- | --- | --- |
| `gpu.info` | none | yes | 20 s | none (read-only) |
| `gpu.smoke` | `size` 256..8192 (default 4096), `dtype` `float32`/`float16`, optional `device` | yes | 180 s | one bounded matmul, no model |
| `gpu.exec` | `action`: `nvidia_smi_query` (`query`: `summary`/`list`/`processes`) or `python_script` (`script`, `params`, `device`, `timeoutSec`, `minFreeMiB`) | no | 600 s | per-device lock + VRAM check for scripts |
| `gpu.job.submit` | `action` (`python_script`), `script`, `params`, `device`, `timeoutSec` 1..3600, `minFreeMiB`, `operationKey` | no | 30 s to answer | detached job, per-device lock for its lifetime |
| `gpu.job.status` | `jobId` | yes | 10 s | none (read-only) |
| `gpu.job.result` | `jobId`, `tailBytes` 1024..65536 | yes | 20 s | none (read-only) |

Behavior details:

- `gpu.smoke` and `gpu.exec`/`gpu.job.submit` default to the **allowlisted GPU
  with the most free VRAM**, so a busy co-tenant card is avoided automatically.
  Pass `device` to pin a specific allowlisted index.
- `gpu.exec` never accepts a shell string, an inline code flag or a URL. The
  only two actions are a fixed `nvidia-smi` query set and an allowlisted script.
- `gpu.job.submit` writes `jobId`-scoped state under `jobDir` and returns
  immediately; the detached runner holds the device lock for the whole job and
  kills the job's own process group when `timeoutSec` expires.
- `operationKey` is a best-effort idempotency key: resubmitting the same key
  returns the original `jobId` instead of starting a second job.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `python` | `python3` | Interpreter for every executor and for allowlisted scripts. Use a venv with torch to enable `gpu.smoke`. |
| `jobDir` | `~/.fedcap/jobs` | Job workspaces, logs, locks and results. Use a data disk. |
| `deviceAllowlist` | `0,2` | Comma-separated GPU indices the pack may use. |
| `scriptAllowlist` | `[]` | Absolute paths of scripts that `gpu.exec` and `gpu.job.submit` may run. Empty disables script execution. |

`fedcap` renders `scriptAllowlist` into `FEDCAP_GPU_SCRIPT_ALLOWLIST` as a
comma-separated list (or JSON array text); the executors accept both forms and
reject relative paths.

## Install

```bash
# from the repository root, on the GPU worker host
node packages/fedcap/bin/fedcap.js validate packs/gpu-nvidia

node packages/fedcap/bin/fedcap.js install \
  --pack packs/gpu-nvidia \
  --node-config ~/.mesh/node.json \
  --set python=/path/venv/bin/python \
  --set deviceAllowlist=0,2 \
  --set jobDir=/opt/fedcap/jobs

node packages/fedcap/bin/fedcap.js doctor --node-config ~/.mesh/node.json
mesh-node manifest --config ~/.mesh/node.json
```

The short form is equivalent when `fedcap` is on `PATH`:

```bash
fedcap install --pack packs/gpu-nvidia --node-config ~/.mesh/node.json --set python=/path/venv/bin/python
```

`/opt/fedcap/jobs` is an example data path: create it with mode 0700 and make
it writable by the pack user, or keep the default `~/.fedcap/jobs`.

### Enabling script execution

`gpu.exec` (`python_script`) and `gpu.job.submit` stay disabled until you list
absolute script paths. Comma-separated:

```bash
node packages/fedcap/bin/fedcap.js install \
  --pack packs/gpu-nvidia \
  --node-config ~/.mesh/node.json \
  --set scriptAllowlist=/opt/fedcap/scripts/train.py,/opt/fedcap/scripts/evaluate.py
```

or via a values file (`--values deploy/values.example.json`):

```json
{ "scriptAllowlist": ["/opt/fedcap/scripts/train.py", "/opt/fedcap/scripts/evaluate.py"] }
```

Scripts must be absolute, must exist at call time, must not be world-writable
and should be owned by the pack user (a warning is logged otherwise). See
[`deploy/README.md`](deploy/README.md) for the shared-host checklist.

## Calling the capabilities

The hub sends one `mesh/1` request; the worker answers with one response. A
minimal request envelope:

```json
{
  "v": 1,
  "id": "req-0001",
  "type": "capability.invoke",
  "from": "router",
  "at": 1760000000000,
  "payload": { "capability": "gpu.info", "args": {}, "trace": "task-0001" }
}
```

Local invocation with the node CLI (stdin is the args object):

```bash
echo '{}' | mesh-node invoke --capability gpu.info --args-json -
echo '{"size": 4096, "dtype": "float16"}' | mesh-node invoke --capability gpu.smoke --args-json -
```

Examples of `payload.args` per capability:

```jsonc
// gpu.info
{}

// gpu.smoke — one bounded matmul, no device pinned
{ "size": 4096, "dtype": "float32" }

// gpu.exec — fixed read-only nvidia-smi query
{ "action": "nvidia_smi_query", "query": "summary" }

// gpu.exec — one allowlisted script, at most 60 s, at least 4 GiB free
{
  "action": "python_script",
  "script": "/opt/fedcap/scripts/train.py",
  "params": { "steps": 50 },
  "device": 0,
  "timeoutSec": 60,
  "minFreeMiB": 4096
}

// gpu.job.submit — detached job, recoverable via operationKey
{
  "action": "python_script",
  "script": "/opt/fedcap/scripts/evaluate.py",
  "params": { "split": "validation" },
  "timeoutSec": 900,
  "operationKey": "eval-validation-0001"
}
// note: "args" is accepted as an alias for "params" in gpu.exec and gpu.job.submit

// gpu.job.status
{ "jobId": "20260101T000000Z-00000000" }

// gpu.job.result — last 16 KiB of each stream
{ "jobId": "20260101T000000Z-00000000", "tailBytes": 16384 }
```

## What an allowlisted script receives

- The JSON object from `args.params` (the alias `args.args` is accepted) as one
  JSON object on **stdin**.
- A scrubbed environment: `PATH`, `HOME`, `LANG`, `LC_ALL`,
  `PYTHONUNBUFFERED`, and `CUDA_VISIBLE_DEVICES=<pinned index>`. Tokens and
  other variables from the pack user's shell are **not** inherited.
- A private working directory under `jobDir` (`work/<id>` for `gpu.exec`,
  `jobs/<jobId>/work` for background jobs).
- The configured `python` interpreter, and the GPU whose free VRAM passed the
  preflight.

Stdout is captured and returned as a string field (not re-parsed as JSON), so a
script may print arbitrary diagnostics; only the executor's own stdout is the
capability JSON.

## Security notes and risks

- **Not a sandbox.** An allowlisted script runs as the pack user and can read
  the same files, use the same network and consume the same GPU as that user.
  Only allowlist scripts you wrote or reviewed, owned by your user, and not
  world-writable.
- **The allowlist is the whole safety boundary** for script execution. There is
  no inline code, no shell interpolation and no URL fetch action.
- **The device lock is advisory and pack-local.** It serializes this pack's own
  runs; other tenants and other tools on the host are not coordinated. Free VRAM
  is checked before a run (TOCTOU): a co-tenant can still take the memory
  afterwards, so scripts should handle CUDA out-of-memory gracefully.
- **`CUDA_VISIBLE_DEVICES` is not exclusivity.** It hides other cards from the
  child process but does not reserve the selected card.
- **Timeouts only kill the job's own process group.** The pack never signals
  unrelated processes and never runs `sudo`.
- **Job state can contain sensitive data.** `params.json`, `script.stdout` and
  `script.stderr` are written under `jobDir` (mode 0600); `gpu.job.result`
  returns their tails to the hub. Do not pass secrets in `params`; pass a file
  path and keep the secret outside the job directory.
- **Cleanup is manual.** Jobs are never deleted by the pack; remove old
  `jobDir/jobs/<jobId>` directories yourself. `operationKey` records live in
  `jobDir/operations/`.
- **Prefer private transports.** Expose the worker over SSH or a private mesh
  network; a mesh HTTP endpoint that is reachable from the internet hands the
  caller every allowlisted script.

## Local self-check

```bash
# 1. syntax of every executor (CI also runs this)
python3 -m py_compile packs/gpu-nvidia/bin/*.py

# 2. JSON contract without configuration
echo '{}' | python3 packs/gpu-nvidia/bin/gpu_info.py
echo '{}' | python3 packs/gpu-nvidia/bin/gpu_smoke.py
echo '{"action": "nvidia_smi_query", "query": "summary"}' | python3 packs/gpu-nvidia/bin/gpu_exec.py
echo '{"action": "python_script", "script": "/opt/fedcap/scripts/REPLACE_ME.py"}' \
  | python3 packs/gpu-nvidia/bin/gpu_exec.py

# 3. with a scratch job directory and one script allowlisted
export FEDCAP_GPU_JOB_DIR="$(mktemp -d)"
export FEDCAP_GPU_DEVICE_ALLOWLIST=0
export FEDCAP_GPU_SCRIPT_ALLOWLIST=/opt/fedcap/scripts/REPLACE_ME.py
# then call gpu.exec / gpu.job.submit with that absolute script path
```

Every stdin case above must print exactly one JSON object and exit 0. On a host
without `nvidia-smi` the expected result is `ok:false` with
`error: "nvidia_smi_unavailable"` — the shell contract is still intact.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `nvidia_smi_unavailable` | `nvidia-smi` missing from `PATH`; install the driver or set `NVIDIA_SMI_PATH`. |
| `torch_unavailable` | The configured `python` has no torch; install it or `--set python=/path/venv/bin/python`. |
| `cuda_unavailable` | torch cannot see a CUDA device (driver/torch mismatch, or a CPU-only build). |
| `device_busy` | Another pack job holds this GPU's lock; poll `gpu.job.status` or use the other allowlisted card. |
| `insufficient_vram` | The card's free memory is below `minFreeMiB`; lower it or wait for the co-tenant. |
| `script_not_allowed` | The absolute path is not in `FEDCAP_GPU_SCRIPT_ALLOWLIST`, is world-writable, or the allowlist is empty. |
| `job_not_found` | Wrong `jobId`, or the job directory was cleaned up. |
| `operation_key_stale` | The `operationKey` points at a job whose directory is gone; use a new key. |
| `state: "lost"` | The runner disappeared before writing a final state (host reboot or OOM kill); check the job logs. |
| `truncated: true` | Output exceeded the embed cap; read the full file from `jobDir` on the worker. |

## License

MIT — see the repository [`LICENSE`](../../LICENSE).
