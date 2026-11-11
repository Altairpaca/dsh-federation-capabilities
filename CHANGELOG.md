# Changelog

All notable changes to this repository are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `fedcap` v0.1: zero-dependency installer/validator/doctor for capability
  packs (`list`, `validate`, `install`, `uninstall`, `doctor`).
- Capability pack specification v1 (`spec/capability-pack-v1.md`).
- Public sanitization gate (`scripts/check-sanitize.mjs`) and pack checker
  (`scripts/check-packs.mjs`), wired into CI.
- GitHub Actions CI for Node.js 20/24 with sanitize, pack validation and
  Python syntax checks.

### Packs

- Planned for this first release: `firecrawl`, `searxng-search`, `gpu-nvidia`,
  `network-probe`.

## [0.1.0] - 2026-11-11

First public skeleton: installer, specification, sanitization and CI.
