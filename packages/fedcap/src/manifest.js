/**
 * Capability pack manifest loading, validation and placeholder resolution.
 *
 * Spec: spec/capability-pack-v1.md
 */
import { readFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { isPlainObject, pathExists, readJson } from './util.js'

export const MANIFEST_FILE = 'fedcap.json'
export const MANIFEST_VERSION = 1
export const PACK_ID_RE = /^[a-z0-9][a-z0-9-]*$/
export const CAPABILITY_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/
export const CONFIG_KEY_RE = /^[A-Za-z][A-Za-z0-9]*$/
export const PLACEHOLDER_RE = /\{\{([A-Za-z0-9_.-]+)\}\}/g
export const INVOKE_KINDS = ['agent', 'shell', 'mcp', 'tool']
export const VISIBILITIES = ['shared', 'node-local']

function push(errors, message) {
  errors.push(message)
}

export function collectPlaceholders(value, found = new Set()) {
  if (typeof value === 'string') {
    for (const match of value.matchAll(PLACEHOLDER_RE)) found.add(match[1])
    return found
  }
  if (Array.isArray(value)) {
    for (const item of value) collectPlaceholders(item, found)
    return found
  }
  if (isPlainObject(value)) {
    for (const item of Object.values(value)) collectPlaceholders(item, found)
  }
  return found
}

export function validateDescriptor(descriptor, where, errors) {
  if (!isPlainObject(descriptor)) {
    push(errors, `${where}: capability must be an object`)
    return
  }
  if (typeof descriptor.id !== 'string' || !CAPABILITY_ID_RE.test(descriptor.id)) {
    push(errors, `${where}: capability.id must match ${CAPABILITY_ID_RE}`)
  }
  if (!isPlainObject(descriptor.invoke)) {
    push(errors, `${where}: capability.invoke must be an object`)
    return
  }
  const invoke = descriptor.invoke
  if (!INVOKE_KINDS.includes(invoke.kind)) {
    push(errors, `${where}: invoke.kind must be one of ${INVOKE_KINDS.join(', ')}`)
  }
  if (invoke.kind === 'shell' && (typeof invoke.command !== 'string' || invoke.command.trim() === '')) {
    push(errors, `${where}: shell invoke requires a non-empty command`)
  }
  if (invoke.kind === 'shell' && typeof invoke.command === 'string' && /[\r\n]/.test(invoke.command)) {
    push(errors, `${where}: shell command must be a single line`)
  }
  if (invoke.kind === 'agent' && (typeof invoke.prompt !== 'string' || invoke.prompt.trim() === '')) {
    push(errors, `${where}: agent invoke requires a prompt`)
  }
  if (invoke.kind === 'mcp' && (typeof invoke.server !== 'string' || typeof invoke.tool !== 'string')) {
    push(errors, `${where}: mcp invoke requires server and tool`)
  }
  if (invoke.kind === 'tool' && typeof invoke.tool !== 'string') {
    push(errors, `${where}: tool invoke requires tool`)
  }
  if (invoke.timeoutMs !== undefined && !(Number.isFinite(invoke.timeoutMs) && invoke.timeoutMs > 0)) {
    push(errors, `${where}: invoke.timeoutMs must be a positive number`)
  }
  if (descriptor.timeoutMs !== undefined && !(Number.isFinite(descriptor.timeoutMs) && descriptor.timeoutMs > 0)) {
    push(errors, `${where}: timeoutMs must be a positive number`)
  }
  if (descriptor.idempotent !== undefined && typeof descriptor.idempotent !== 'boolean') {
    push(errors, `${where}: idempotent must be a boolean`)
  }
  if (descriptor.visibility !== undefined && !VISIBILITIES.includes(descriptor.visibility)) {
    push(errors, `${where}: visibility must be one of ${VISIBILITIES.join(', ')}`)
  }
  for (const field of ['inputSchema', 'outputSchema']) {
    if (descriptor[field] !== undefined && !isPlainObject(descriptor[field])) {
      push(errors, `${where}: ${field} must be an object`)
    }
  }
}

export async function loadManifest(packDir) {
  const manifestPath = join(packDir, MANIFEST_FILE)
  if (!(await pathExists(manifestPath))) {
    throw new Error(`manifest not found: ${manifestPath}`)
  }
  let manifest
  try {
    manifest = await readJson(manifestPath)
  } catch (error) {
    throw new Error(`cannot parse ${manifestPath}: ${error instanceof Error ? error.message : String(error)}`)
  }
  return { manifest, manifestPath }
}

/**
 * Validate one manifest. Returns { ok, errors, warnings }.
 * When `packDir` is given, referenced `{{packDir}}/...` files are checked.
 */
export async function validateManifest(manifest, { packDir } = {}) {
  const errors = []
  const warnings = []
  if (!isPlainObject(manifest)) return { ok: false, errors: ['manifest must be a JSON object'], warnings }
  if (manifest.v !== MANIFEST_VERSION) push(errors, `manifest.v must be ${MANIFEST_VERSION}`)
  if (typeof manifest.id !== 'string' || !PACK_ID_RE.test(manifest.id)) {
    push(errors, `manifest.id must match ${PACK_ID_RE}`)
  }
  if (typeof manifest.version !== 'string' || !/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(manifest.version)) {
    push(errors, 'manifest.version must be semver (e.g. 0.1.0)')
  }
  if (typeof manifest.title !== 'string' || manifest.title.trim() === '') {
    push(errors, 'manifest.title is required')
  }
  if (manifest.config !== undefined) {
    if (!isPlainObject(manifest.config)) {
      push(errors, 'manifest.config must be an object')
    } else {
      for (const [key, entry] of Object.entries(manifest.config)) {
        if (!CONFIG_KEY_RE.test(key)) push(errors, `config key "${key}" must be alphanumeric`)
        if (!isPlainObject(entry)) {
          push(errors, `config.${key} must be an object`)
          continue
        }
        if (entry.default === undefined) push(errors, `config.${key}.default is required`)
        else if (typeof entry.default === 'string' && entry.default.includes('\n')) {
          push(errors, `config.${key}.default must not contain newlines`)
        }
      }
    }
  }
  if (!Array.isArray(manifest.capabilities) || manifest.capabilities.length === 0) {
    push(errors, 'manifest.capabilities must be a non-empty array')
  } else {
    const seen = new Set()
    manifest.capabilities.forEach((descriptor, index) => {
      validateDescriptor(descriptor, `capabilities[${index}]`, errors)
      if (isPlainObject(descriptor) && typeof descriptor.id === 'string') {
        if (seen.has(descriptor.id)) push(errors, `duplicate capability id: ${descriptor.id}`)
        seen.add(descriptor.id)
      }
    })
  }
  if (manifest.checks !== undefined) {
    if (!Array.isArray(manifest.checks)) push(errors, 'manifest.checks must be an array')
    else {
      manifest.checks.forEach((check, index) => {
        if (!isPlainObject(check) || typeof check.command !== 'string' || check.command.trim() === '') {
          push(errors, `checks[${index}] must have a command`)
        }
      })
    }
  }
  const placeholders = collectPlaceholders({
    capabilities: manifest.capabilities,
    checks: manifest.checks,
    config: manifest.config,
  })
  const configKeys = isPlainObject(manifest.config) ? Object.keys(manifest.config) : []
  for (const placeholder of placeholders) {
    if (placeholder === 'packDir' || placeholder === 'python' || placeholder === 'node') continue
    if (placeholder.startsWith('config.')) {
      const key = placeholder.slice('config.'.length)
      if (!configKeys.includes(key)) push(errors, `placeholder {{config.${key}}} has no config entry`)
      continue
    }
    push(errors, `unknown placeholder {{${placeholder}}}`)
  }
  if (packDir !== undefined) {
    for (const [index, descriptor] of manifest.capabilities.entries()) {
      const command = descriptor.invoke?.kind === 'shell' ? descriptor.invoke.command : ''
      for (const match of String(command).matchAll(/\{\{packDir\}\}\/([^\s"']+)/g)) {
        const referenced = resolve(packDir, match[1])
        if (!(await pathExists(referenced))) {
          push(errors, `capabilities[${index}] references missing pack file: ${referenced}`)
        }
      }
    }
  }
  return { ok: errors.length === 0, errors, warnings }
}

/** Resolve config values from defaults + values file + --set overrides. */
export function resolveConfig(manifest, values = {}, sets = {}) {
  const config = {}
  for (const [key, entry] of Object.entries(isPlainObject(manifest.config) ? manifest.config : {})) {
    config[key] = entry.default
  }
  for (const [key, value] of Object.entries(values)) {
    if (config[key] !== undefined) config[key] = value
  }
  for (const [key, value] of Object.entries(sets)) {
    if (config[key] === undefined) throw new Error(`--set ${key} is not a config key of pack ${manifest.id}`)
    config[key] = value
  }
  for (const [key, value] of Object.entries(config)) {
    if (typeof value === 'string' && value.includes('\n')) throw new Error(`config.${key} must not contain newlines`)
  }
  return config
}

export function substitute(value, replacements) {
  if (typeof value === 'string') {
    let output = value
    for (const [key, replacement] of Object.entries(replacements)) {
      output = output.split(`{{${key}}}`).join(String(replacement))
    }
    return output
  }
  if (Array.isArray(value)) return value.map((item) => substitute(item, replacements))
  if (isPlainObject(value)) {
    const output = {}
    for (const [key, item] of Object.entries(value)) output[key] = substitute(item, replacements)
    return output
  }
  return value
}

/**
 * Resolve a pack into concrete capability descriptors.
 * @returns {{ manifest, descriptors, config }}
 */
export async function resolvePack(packDir, { python = 'python3', values = {}, sets = {}, nodeExecutable = process.execPath } = {}) {
  const { manifest } = await loadManifest(packDir)
  const validation = await validateManifest(manifest, { packDir })
  if (!validation.ok) throw new Error(`invalid pack (${manifest.id ?? packDir}):\n- ${validation.errors.join('\n- ')}`)
  const config = resolveConfig(manifest, values, sets)
  const replacements = { packDir: resolve(packDir), python, node: nodeExecutable }
  for (const [key, value] of Object.entries(config)) replacements[`config.${key}`] = value
  const resolvedDescriptors = substitute(manifest.capabilities, replacements)
  const unresolved = collectPlaceholders(resolvedDescriptors)
  if (unresolved.size > 0) {
    throw new Error(`unresolved placeholders after substitution: ${[...unresolved].join(', ')}`)
  }
  return { manifest, descriptors: resolvedDescriptors, config }
}

export function manifestPathOf(packDir) {
  return join(packDir, MANIFEST_FILE)
}

export async function readTextFile(path) {
  return readFile(path, 'utf8')
}
