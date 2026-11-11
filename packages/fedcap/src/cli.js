/** fedcap CLI: list / validate / install / uninstall / doctor. */
import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { loadManifest, validateManifest } from './manifest.js'
import { doctor, installPack, listPacks, uninstallPack } from './install.js'

const USAGE = `Usage: fedcap <command> [options]

Commands:
  list [--json]                         list packs under ./packs
  validate <pack-dir> [--json]          validate one pack manifest and files
  install --pack <pack-dir> --node-config <node.json>
          [--python <exe>] [--values <file>] [--set key=value ...] [--force]
  uninstall --pack-id <id> --node-config <node.json>
  doctor --node-config <node.json> [--json]

Exit codes: 0 ok, 1 error, 2 usage error.`

function parseArgs(argv) {
  const options = { sets: {}, values: {}, positional: [] }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--json') options.json = true
    else if (arg === '--force') options.force = true
    else if (arg === '--pack') options.pack = argv[(index += 1)]
    else if (arg === '--pack-id') options.packId = argv[(index += 1)]
    else if (arg === '--node-config') options.nodeConfig = argv[(index += 1)]
    else if (arg === '--python') options.python = argv[(index += 1)]
    else if (arg === '--values') options.valuesFile = argv[(index += 1)]
    else if (arg === '--set') {
      const pair = argv[(index += 1)] ?? ''
      const separator = pair.indexOf('=')
      if (separator <= 0) throw new UsageError(`--set expects key=value, got "${pair}"`)
      options.sets[pair.slice(0, separator)] = pair.slice(separator + 1)
    } else if (arg.startsWith('--')) throw new UsageError(`unknown option ${arg}`)
    else options.positional.push(arg)
  }
  return options
}

class UsageError extends Error {}

function need(options, ...names) {
  for (const name of names) {
    if (options[name] === undefined || options[name] === '') throw new UsageError(`missing --${name.replace(/[A-Z]/g, (ch) => `-${ch.toLowerCase()}`)}`)
  }
}

async function loadValues(options) {
  if (options.valuesFile === undefined) return {}
  const text = await readFile(resolve(options.valuesFile), 'utf8')
  const values = JSON.parse(text)
  if (values === null || typeof values !== 'object' || Array.isArray(values)) {
    throw new Error('--values file must contain a JSON object')
  }
  if (values.config && typeof values.config === 'object') return values.config
  return values
}

function printHuman(result) {
  if (result.command === 'list') {
    if (result.packs.length === 0) {
      console.log('no packs found under ./packs')
      return
    }
    for (const pack of result.packs) {
      if (pack.error) console.log(`${pack.id}\tERROR\t${pack.error}`)
      else console.log(`${pack.id}\t${pack.version}\t${pack.capabilities} capabilities\t${pack.title}`)
    }
    return
  }
  if (result.command === 'validate') {
    console.log(result.ok ? `ok: ${result.packId} (${result.capabilities} capabilities)` : `invalid: ${result.packId}`)
    for (const error of result.errors) console.log(`- ${error}`)
    return
  }
  if (result.command === 'install') {
    console.log(
      result.changed
        ? `installed ${result.packId}: ${result.capabilities} capabilities -> ${result.target}`
        : `${result.packId} already installed and up to date: ${result.target}`,
    )
    if (result.backup) console.log(`node config backup: ${result.backup}`)
    return
  }
  if (result.command === 'uninstall') {
    console.log(`${result.removed ? 'uninstalled' : 'not installed'}: ${result.packId}`)
    return
  }
  if (result.command === 'doctor') {
    console.log(`${result.ok ? 'ok' : 'problems found'}: ${result.nodeConfig} (node ${result.nodeId ?? 'unknown'})`)
    console.log(`capability files: ${result.capabilityFiles.length}; capabilities: ${result.capabilities.length}`)
    for (const problem of result.problems) console.log(`PROBLEM ${problem}`)
    for (const warning of result.warnings) console.log(`WARN ${warning}`)
  }
}

export async function main(argv) {
  if (argv.length === 0 || argv[0] === '--help' || argv[0] === '-h') {
    console.log(USAGE)
    return
  }
  const command = argv[0]
  const options = parseArgs(argv.slice(1))
  const repoRoot = process.cwd()

  if (command === 'list') {
    const packs = await listPacks(repoRoot, { json: options.json })
    const result = { command, packs }
    if (options.json) console.log(JSON.stringify(result, null, 2))
    else printHuman(result)
    return
  }

  if (command === 'validate') {
    const packDir = options.positional[0] ?? options.pack
    if (!packDir) throw new UsageError('validate requires <pack-dir>')
    const { manifest } = await loadManifest(resolve(packDir))
    const validation = await validateManifest(manifest, { packDir: resolve(packDir) })
    const result = {
      command,
      packId: manifest.id ?? packDir,
      capabilities: Array.isArray(manifest.capabilities) ? manifest.capabilities.length : 0,
      ok: validation.ok,
      errors: validation.errors,
      warnings: validation.warnings,
    }
    if (options.json) console.log(JSON.stringify(result, null, 2))
    else printHuman(result)
    if (!result.ok) process.exitCode = 1
    return
  }

  if (command === 'install') {
    need(options, 'pack', 'nodeConfig')
    const values = await loadValues(options)
    const result = await installPack({
      packDir: resolve(options.pack),
      nodeConfigPath: options.nodeConfig,
      python: options.python ?? 'python3',
      values,
      sets: options.sets,
      force: options.force === true,
    })
    const output = { command, ...result }
    if (options.json) console.log(JSON.stringify(output, null, 2))
    else printHuman(output)
    return
  }

  if (command === 'uninstall') {
    need(options, 'packId', 'nodeConfig')
    const result = await uninstallPack({ packId: options.packId, nodeConfigPath: options.nodeConfig })
    const output = { command, ...result }
    if (options.json) console.log(JSON.stringify(output, null, 2))
    else printHuman(output)
    return
  }

  if (command === 'doctor') {
    need(options, 'nodeConfig')
    const result = await doctor({ nodeConfigPath: options.nodeConfig })
    const output = { command, ...result }
    if (options.json) console.log(JSON.stringify(output, null, 2))
    else printHuman(output)
    if (!result.ok) process.exitCode = 1
    return
  }

  throw new UsageError(`unknown command: ${command}`)
}

export { UsageError }
