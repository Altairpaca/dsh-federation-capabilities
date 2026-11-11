#!/usr/bin/env node
/**
 * Public-repository sanitization gate.
 *
 * Fails when the tree (or, with --history, any commit) contains deployment
 * specific data: real IP addresses, personal home directories, user names,
 * e-mail addresses, tokens, or this deployment's host names.
 *
 *   node scripts/check-sanitize.mjs
 *   node scripts/check-sanitize.mjs --history
 */
import { readFile, readdir } from 'node:fs/promises'
import { execFileSync } from 'node:child_process'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(fileURLToPath(new URL('.', import.meta.url)), '..')
const skipDirs = new Set(['.git', 'node_modules', 'coverage', 'dist', '__pycache__'])
const historyMode = process.argv.includes('--history')

const rules = [
  { name: 'ipv4 address', re: /\b(?:\d{1,3}\.){3}\d{1,3}\b/ },
  { name: 'windows user path', re: /[A-Za-z]:\\Users\\/ },
  { name: 'windows drive path', re: /\b[A-Za-z]:\\(?:Programs|Tools|Temp)\b/ },
  { name: 'personal home path', re: /\/home\/(?!runner\b|mesh\b|opt\b|fedcap\b)[a-z0-9._-]+\//i },
  { name: 'email address', re: /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/i },
  { name: 'github token', re: /\bgh[op]_[A-Za-z0-9]{20,}/ },
  { name: 'api key shape', re: /\bsk-[A-Za-z0-9]{16,}/ },
  // Deployment identifiers of the reference deployment. Built from parts so
  // this gate never contains the literal names it forbids (history mode scans
  // this file too).
  {
    name: 'deployment host name',
    re: new RegExp(
      `\\b(?:${['altair' + '-server', 'bld' + '-lzz', 'gpu' + '1', 'idea' + '2story', 'tail' + '5f60', 'firecrawl' + '-selfhost'].join('|')})\\b`,
      'i',
    ),
  },
]

const allow = [
  // Reserved example domains and protocol constants are placeholders.
  /@[a-z0-9.-]*example\.(?:invalid|com)\b/i,
  /\b127\.0\.0\.1\b/,
  /\b0\.0\.0\.0\b/,
]

function scanLine(text, where) {
  for (const rule of rules) {
    if (!rule.re.test(text)) continue
    if (allow.some((pattern) => pattern.test(text))) continue
    console.error(`${where}: ${rule.name}: ${text.trim().slice(0, 200)}`)
    return 1
  }
  return 0
}

let failures = 0

if (historyMode) {
  let output = ''
  try {
    output = execFileSync('git', ['log', '-p', '--all', '--no-color'], {
      cwd: root,
      encoding: 'utf8',
      maxBuffer: 64 * 1024 * 1024,
    })
  } catch (error) {
    console.error(`cannot read git history: ${error instanceof Error ? error.message : String(error)}`)
    process.exit(2)
  }
  for (const line of output.split('\n')) {
    if (/^(Author|Commit|Date|Merge):/.test(line)) continue
    failures += scanLine(line, 'git-history')
  }
} else {
  async function* walk(dir) {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      if (skipDirs.has(entry.name)) continue
      const path = join(dir, entry.name)
      if (entry.isDirectory()) yield* walk(path)
      else if (entry.isFile()) yield path
    }
  }
  for await (const file of walk(root)) {
    // This script necessarily contains the patterns it searches for.
    if (file.endsWith('check-sanitize.mjs')) continue
    if (/\.(png|jpg|jpeg|webp|ico|gif|pdf|zip|tgz)$/i.test(file)) continue
    const text = await readFile(file, 'utf8').catch(() => '')
    text.split('\n').forEach((line, index) => {
      failures += scanLine(line, `${relative(root, file)}:${index + 1}`)
    })
  }
}

if (failures > 0) {
  console.error(`\nSanitization check failed: ${failures} finding(s).`)
  process.exit(1)
}
console.log(
  historyMode
    ? 'Sanitization check passed: git history contains no deployment-specific data.'
    : 'Sanitization check passed: no deployment-specific data found.',
)
