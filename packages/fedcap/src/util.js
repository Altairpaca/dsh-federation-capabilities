/** Shared filesystem and JSON helpers. */
import { access, mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { dirname, join, relative, resolve } from 'node:path'
import { homedir } from 'node:os'

export function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export function expandHome(value) {
  if (typeof value !== 'string') return value
  if (value === '~') return homedir()
  if (value.startsWith('~/')) return join(homedir(), value.slice(2))
  return value
}

export function resolvePath(value, baseDir = process.cwd()) {
  return resolve(baseDir, expandHome(value))
}

export async function readJson(path) {
  const text = (await readFile(path, 'utf8')).replace(/^\uFEFF/, '')
  return JSON.parse(text)
}

export async function pathExists(path) {
  try {
    await access(path)
    return true
  } catch {
    return false
  }
}

export async function writeFileAtomic(path, text, mode = 0o644) {
  await mkdir(dirname(path), { recursive: true })
  const temporary = `${path}.tmp-${process.pid}-${Date.now()}`
  await writeFile(temporary, text, { encoding: 'utf8', mode })
  await rename(temporary, path)
}

export async function backupFile(path) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-')
  const target = `${path}.bak-${stamp}`
  await writeFileAtomic(target, await readFile(path, 'utf8'))
  return target
}

export function toPosixPath(value) {
  return value.split('\\').join('/')
}

export function relativePosix(fromDir, toPath) {
  return toPosixPath(relative(fromDir, toPath))
}
