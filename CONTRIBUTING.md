# Contributing

Thanks for considering a capability pack. The bar is intentionally small:
one manifest, a few zero-dependency executor scripts, a README, and a
self-check.

## Adding a pack

1. Copy the shape of an existing pack under `packs/<id>/`.
2. Write `fedcap.json` following [`spec/capability-pack-v1.md`](../spec/capability-pack-v1.md).
   Use placeholders (`{{packDir}}`, `{{python}}`, `{{config.key}}`) instead of
   absolute paths or host-specific values.
3. Executor contract: one JSON value on **stdin**, exactly one JSON value on
   **stdout**, diagnostics on **stderr**, bounded output, no third-party
   dependencies unless the README says otherwise.
4. Add `README.md` with the capability table, install example, security notes
   and a local self-check.
5. Run:
   ```bash
   node packages/fedcap/bin/fedcap.js validate packs/<id>
   node scripts/check-packs.mjs
   node scripts/check-sanitize.mjs
   npm test
   ```

## Rules

- **No deployment data in git**: no real host names, IP addresses, home
  directories, user names, e-mail addresses or tokens. Examples use
  `example.invalid`, `127.0.0.1`, `/opt/fedcap`, `REPLACE_ME`.
- **Secrets by reference**: a path to a token file, read at run time.
- **Idempotency**: read-only capabilities must set `idempotent: true`.
  Side-effecting ones set `idempotent: false` and document confirmation.
- **Bounded**: cap response sizes and timeouts; never let an executor stream
  unbounded data.
- **No core changes**: if a capability needs new mesh semantics, document the
  gap and ship a host-side helper instead of forking the protocol.

## Review checklist

- [ ] `fedcap validate` passes
- [ ] `fedcap install` into a scratch `node.json` passes and `doctor` is clean
- [ ] `npm run sanitize` passes (and `sanitize:history` before release)
- [ ] README explains the required host service and how to secure it
- [ ] Executors tested on a clean host (or documented why not)
