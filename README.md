# dsh-federation-capabilities

Pluggable **host-capability packs** for [DSH Federation](https://github.com/Altairpaca/dsh-federation)
(a federated agent mesh built on [DSH](https://github.com/deepseek-ai)). A pack
turns one local service or host resource into mesh capabilities — Firecrawl
scraping, SearXNG search, NVIDIA GPU inspection/compute, outbound-network
probes — without modifying federation core.

Status: **v0.1.0 (early)**. The `fedcap` installer and the pack format are
stable enough to use; APIs may change before 1.0.

## Why packs

A mesh worker executes capabilities it declares in its node configuration. A
capability descriptor is just JSON (`id`, `invoke`, `timeoutMs`, schemas), but
writing deployment-specific descriptors by hand is error-prone: absolute paths,
venv pythons, token files, per-host service URLs, and the "did I remember to
add it to `capabilitiesFile`?" step.

A pack packages the descriptor templates **and** the executor scripts, then
`fedcap install` resolves the deployment values, writes
`capabilities.d/<pack>.json`, and points the node's existing
`capabilitiesFile` at it:

```
pack (fedcap.json + bin/) --fedcap install--> capabilities.d/*.json
        --> mesh-node manifest --> hub registry --> DSH mesh_* tools / MCP / A2A
```

No core changes: packs target `dsh-federation >= 0.8.0`.

## Packs

| Pack | Capabilities | Needs |
| --- | --- | --- |
| [`firecrawl`](packs/firecrawl/) | `firecrawl.scrape`, `firecrawl.search` | self-hosted Firecrawl API (loopback, optional bearer-token gateway template) |
| [`searxng-search`](packs/searxng-search/) | `web.search` | SearXNG with the JSON API enabled |
| [`gpu-nvidia`](packs/gpu-nvidia/) | `gpu.info`, `gpu.smoke`, `gpu.exec`, `gpu.job.*` | NVIDIA driver + `nvidia-smi` (torch only for `gpu.smoke`) |
| [`network-probe`](packs/network-probe/) | `net.egress`, `web.fetch` | Python 3, outbound network |

## Quick start

```bash
# 1. clone this repository on the worker host
git clone https://github.com/Altairpaca/dsh-federation-capabilities
cd dsh-federation-capabilities

# 2. see what is available
node packages/fedcap/bin/fedcap.js list
node packages/fedcap/bin/fedcap.js validate packs/network-probe

# 3. install into the node configuration (values stay local, never committed)
node packages/fedcap/bin/fedcap.js install \
  --pack packs/network-probe \
  --node-config ~/.mesh/node.json

# 4. check the result: files, ids, executor paths
node packages/fedcap/bin/fedcap.js doctor --node-config ~/.mesh/node.json

# 5. the worker now advertises the capabilities on its next manifest request
mesh-node manifest --config ~/.mesh/node.json
```

Install values can come from `--values values.json`, `--set key=value`, or the
manifest defaults. See [`spec/capability-pack-v1.md`](spec/capability-pack-v1.md)
for placeholder semantics and config precedence.

## Repository layout

```
packages/fedcap/      zero-dependency installer/validator/doctor
packs/<id>/           one capability pack: fedcap.json + bin/ + deploy/ + README
spec/                 capability pack specification v1
examples/             sanitized node/hub/topology snippets
docs/                 integration and pack-authoring guides
scripts/              sanitization gate, pack check
.github/workflows/    CI (Node 20/24, sanitize, packs, python syntax)
```

## Security model

- Packs never contain deployment values. Host names, addresses, home
  directories and tokens are supplied at install time and stay out of git.
- Secrets are referenced as **file paths** and read at execution time; never as
  manifest literals.
- Executors are fixed commands; user input arrives as JSON on stdin. GPU
  `gpu.exec` only runs a configured allowlist of scripts/actions.
- The public repository is sanitized by `npm run sanitize` (tree) and
  `npm run sanitize:history` (all commits); CI runs the same gate.
- Self-hosted services should bind loopback; remote access goes through a
  bearer-token gateway (see `packs/firecrawl/deploy/`).

See [SECURITY.md](SECURITY.md) for reporting and the full threat notes.

## Development

```bash
npm test              # fedcap unit tests
npm run sanitize      # no deployment data in the working tree
npm run check:packs   # every pack validates and ships a README
npm run ci            # all of the above
```

Node.js >= 20 for the installer; executor scripts are Python 3 and have no
third-party dependencies (except `gpu.smoke`, which optionally imports torch).

## License

MIT — see [LICENSE](LICENSE).
