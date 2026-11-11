# Security policy

## Reporting

Please report suspected vulnerabilities privately via GitHub Security
Advisories on this repository. Do not open a public issue with exploit details.

## What this project does and does not protect

Capability packs run on a **worker host you control** and are invoked by a
federation hub over a transport (usually SSH). The trust boundary is the mesh
transport and the host account:

- The transport authenticates the hub (SSH key / HTTP bearer token); packs do
  not add their own peer authentication. See the federation security model
  (`dsh-federation/docs/004-security.md`).
- Executors run with the worker account's privileges. Install only packs you
  reviewed.
- Manifests and committed files must not contain secrets. Secrets are file
  references read at run time.

## Hardening expectations for packs

- Prefer loopback bindings for local services; expose remote access only
  through a token-protected gateway (see `packs/firecrawl/deploy/`).
- Treat scrapers and fetch capabilities as SSRF surfaces: reject private and
  loopback targets unless a pack explicitly documents an allowlist.
- GPU packs run on shared machines: device allowlists, memory pre-checks,
  per-device locking and bounded outputs are mandatory.
- Side-effecting capabilities must be `idempotent: false` and documented.

## Sanitization

This is the public distribution of the capability layer. Deployment-specific
data must never be committed. `npm run sanitize` and
`npm run sanitize:history` enforce that; CI runs the tree scan. If you find
deployment data in history, report it privately — rewriting public history is
a maintainer decision.

## Supported versions

Only the latest `main` is supported during 0.x.
