/**
 * Install / uninstall / doctor for capability packs.
 *
 * Installation writes nothing into `dsh-federation`: it produces resolved
 * capability descriptor files and points the node configuration's existing
 * `capabilitiesFile` at them (0.8.0 behaviour).
 */
import { readdir, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import {
  backupFile,
  isPlainObject,
  pathExists,
  readJson,
  relativePosix,
  resolvePath,
  writeFileAtomic,
} from './util.js'
import { loadManifest, resolvePack, validateDescriptor } from './manifest.js'

function capabilitiesDirOf(nodeConfigPath) {
  return join(dirname(resolve(nodeConfigPath)), 'capabilities.d')
}

export async function loadNodeConfig(nodeConfigPath) {
  const path = resolvePath(nodeConfigPath)
  if (!(await pathExists(path))) throw new Error(`node configuration not found: ${path}`)
  const config = await readJson(path)
  if (!isPlainObject(config)) throw new Error(`node configuration must be a JSON object: ${path}`)
  return { path, config }
}

function existingCapabilityFiles(config) {
  const raw = config.capabilitiesFile
  if (raw === undefined || raw === null) return []
  if (typeof raw === 'string') return [raw]
  if (Array.isArray(raw)) return raw.filter((entry) => typeof entry === 'string')
  throw new Error('node configuration capabilitiesFile must be a string or an array of strings')
}

function withCapabilityFile(config, relativePath) {
  const files = existingCapabilityFiles(config)
  if (!files.includes(relativePath)) files.push(relativePath)
  return { ...config, capabilitiesFile: files }
}

function withoutCapabilityFile(config, relativePath) {
  const files = existingCapabilityFiles(config).filter((entry) => entry !== relativePath)
  const next = { ...config }
  if (files.length === 0) delete next.capabilitiesFile
  else next.capabilitiesFile = files
  return next
}

function relativeCapabilityFile(nodeConfigPath, descriptorPath) {
  return relativePosix(dirname(resolve(nodeConfigPath)), resolve(descriptorPath))
}

export async function installPack({ packDir, nodeConfigPath, python = 'python3', values = {}, sets = {}, force = false }) {
  const { path: configPath, config } = await loadNodeConfig(nodeConfigPath)
  const { manifest, descriptors } = await resolvePack(packDir, { python, values, sets })
  const target = join(capabilitiesDirOf(configPath), `${manifest.id}.json`)
  const relative = relativeCapabilityFile(configPath, target)
  const text = `${JSON.stringify(descriptors, null, 2)}\n`
  let changed = true
  if (await pathExists(target)) {
    const current = await readFile(target, 'utf8')
    if (current === text && existingCapabilityFiles(config).includes(relative)) {
      changed = false
    } else if (!force) {
      throw new Error(
        `capabilities.d/${manifest.id}.json already exists with different content; pass --force to replace it`,
      )
    }
  }
  if (!changed) {
    return { changed, packId: manifest.id, target, relative, capabilities: descriptors.length }
  }
  await writeFileAtomic(target, text)
  const backup = await backupFile(configPath)
  await writeFileAtomic(configPath, `${JSON.stringify(withCapabilityFile(config, relative), null, 2)}\n`)
  return { changed, packId: manifest.id, target, relative, backup, capabilities: descriptors.length }
}

export async function uninstallPack({ packId, nodeConfigPath }) {
  const { path: configPath, config } = await loadNodeConfig(nodeConfigPath)
  const target = join(capabilitiesDirOf(configPath), `${packId}.json`)
  const relative = relativeCapabilityFile(configPath, target)
  const hadFile = await pathExists(target)
  if (hadFile) await rm(target)
  const hadEntry = existingCapabilityFiles(config).includes(relative)
  if (hadEntry) {
    await backupFile(configPath)
    await writeFileAtomic(configPath, `${JSON.stringify(withoutCapabilityFile(config, relative), null, 2)}\n`)
  }
  return { removed: hadFile || hadEntry, packId, target, relative }
}

async function readCapabilityFile(path) {
  const data = await readJson(path)
  if (Array.isArray(data)) return data
  if (isPlainObject(data) && Array.isArray(data.capabilities)) return data.capabilities
  throw new Error(`capability file must be an array or { capabilities: [] }: ${path}`)
}

export async function doctor({ nodeConfigPath }) {
  const { path: configPath, config } = await loadNodeConfig(nodeConfigPath)
  const files = existingCapabilityFiles(config)
  const problems = []
  const warnings = []
  const capabilities = []
  for (const entry of files) {
    const path = resolvePath(entry, dirname(configPath))
    if (!(await pathExists(path))) {
      problems.push(`capabilitiesFile entry does not exist: ${entry} (${path})`)
      continue
    }
    let descriptors
    try {
      descriptors = await readCapabilityFile(path)
    } catch (error) {
      problems.push(`cannot read capability file ${entry}: ${error instanceof Error ? error.message : String(error)}`)
      continue
    }
    descriptors.forEach((descriptor, index) => {
      const where = `${entry}[${index}]`
      const errors = []
      validateDescriptor(descriptor, where, errors)
      for (const message of errors) problems.push(message)
      if (isPlainObject(descriptor) && typeof descriptor.id === 'string') {
        capabilities.push({
          id: descriptor.id,
          file: entry,
          kind: descriptor.invoke?.kind,
          command: descriptor.invoke?.kind === 'shell' ? descriptor.invoke.command : undefined,
        })
      }
    })
  }
  const seen = new Set()
  for (const capability of capabilities) {
    if (seen.has(capability.id)) problems.push(`duplicate capability id across files: ${capability.id}`)
    seen.add(capability.id)
    if (capability.command) {
      const firstToken = capability.command.split(/\s+/)[0]
      if ((firstToken.includes('/') || firstToken.includes('\\')) && !(await pathExists(firstToken))) {
        warnings.push(`executor may be missing: ${capability.id}: ${firstToken}`)
      }
    }
  }
  return {
    ok: problems.length === 0,
    nodeConfig: configPath,
    nodeId: typeof config.nodeId === 'string' ? config.nodeId : typeof config.id === 'string' ? config.id : null,
    capabilityFiles: files,
    capabilities,
    problems,
    warnings,
  }
}

export async function listPacks(repoRoot, options = {}) {
  const packsDir = join(repoRoot, 'packs')
  const packs = []
  if (!(await pathExists(packsDir))) return packs
  for (const entry of await readdir(packsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue
    const packDir = join(packsDir, entry.name)
    const manifestPath = join(packDir, 'fedcap.json')
    if (!(await pathExists(manifestPath))) continue
    try {
      const { manifest } = await loadManifest(packDir)
      packs.push({
        id: manifest.id,
        title: manifest.title,
        version: manifest.version,
        capabilities: Array.isArray(manifest.capabilities) ? manifest.capabilities.length : 0,
        dir: packDir,
        ...(options.json ? { manifest } : {}),
      })
    } catch (error) {
      packs.push({ id: entry.name, error: error instanceof Error ? error.message : String(error), dir: packDir })
    }
  }
  return packs
}
