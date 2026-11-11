#!/usr/bin/env node
/**
 * Validate every pack under ./packs, check its README and that the raw manifest
 * uses placeholders instead of absolute deployment paths.
 */
import { readdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { loadManifest, validateManifest } from '../packages/fedcap/src/manifest.js'
import { pathExists } from '../packages/fedcap/src/util.js'

const root = fileURLToPath(new URL('..', import.meta.url))
const packsDir = join(root, 'packs')

let failures = 0
let count = 0

const entries = await readdir(packsDir, { withFileTypes: true }).catch(() => [])
for (const entry of entries) {
  if (!entry.isDirectory()) continue
  const packDir = join(packsDir, entry.name)
  if (!(await pathExists(join(packDir, 'fedcap.json')))) continue
  count += 1
  const { manifest } = await loadManifest(packDir)
  const validation = await validateManifest(manifest, { packDir })
  if (!validation.ok) {
    failures += 1
    console.error(`FAIL ${entry.name}: ${validation.errors.join('; ')}`)
    continue
  }
  if (!(await pathExists(join(packDir, 'README.md')))) {
    failures += 1
    console.error(`FAIL ${entry.name}: README.md is required`)
  }
  const commands = manifest.capabilities
    .filter((capability) => capability.invoke?.kind === 'shell')
    .map((capability) => capability.invoke.command)
  for (const command of commands) {
    if (/(^|\s)\/(?:home|Users|opt)\//.test(command) || /[A-Za-z]:\\/.test(command)) {
      failures += 1
      console.error(`FAIL ${entry.name}: absolute deployment path in command: ${command}`)
    }
  }
  console.log(`ok ${entry.name}: ${manifest.id}@${manifest.version}, ${manifest.capabilities.length} capabilities`)
}

if (count === 0) {
  failures += 1
  console.error('FAIL no packs found')
}

if (failures > 0) {
  console.error(`\npack check failed: ${failures} finding(s)`)
  process.exit(1)
}
console.log(`\npack check passed: ${count} pack(s)`)
