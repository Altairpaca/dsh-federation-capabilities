import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { test } from 'node:test'
import { validateManifest, resolvePack } from '../src/manifest.js'
import { doctor, installPack, uninstallPack } from '../src/install.js'

const root = resolve(import.meta.dirname, '..')

async function makeFixture() {
  const dir = join(tmpdir(), `fedcap-test-${Date.now()}-${Math.random().toString(16).slice(2)}`)
  const packDir = join(dir, 'packs', 'demo')
  await mkdir(join(packDir, 'bin'), { recursive: true })
  await writeFile(join(packDir, 'bin', 'fetch.py'), 'print("{}")\n')
  await writeFile(
    join(packDir, 'fedcap.json'),
    JSON.stringify({
      v: 1,
      id: 'demo',
      title: 'Demo pack',
      version: '0.1.0',
      config: { baseUrl: { default: 'http://127.0.0.1:9999', description: 'base' } },
      capabilities: [
        {
          id: 'demo.fetch',
          title: 'Demo fetch',
          description: 'demo',
          tags: ['demo'],
          idempotent: true,
          timeoutMs: 10000,
          inputSchema: { type: 'object', properties: {} },
          invoke: {
            kind: 'shell',
            command: '{{python}} {{packDir}}/bin/fetch.py',
            env: { DEMO_BASE_URL: '{{config.baseUrl}}' },
          },
        },
      ],
      checks: [{ command: '{{python}} -m py_compile {{packDir}}/bin/fetch.py' }],
    }),
  )
  const nodeConfig = join(dir, 'node.json')
  await writeFile(
    nodeConfig,
    JSON.stringify({ nodeId: 'demo-node', title: 'demo', kind: 'custom', capabilities: [] }),
  )
  return { dir, packDir, nodeConfig }
}

test('validates a well-formed pack', async () => {
  const { packDir } = await makeFixture()
  const { manifest } = await import('../src/manifest.js').then((module) => module.loadManifest(packDir))
  const result = await validateManifest(manifest, { packDir })
  assert.equal(result.ok, true, result.errors.join('\n'))
})

test('rejects unknown placeholders and missing pack files', async () => {
  const { packDir } = await makeFixture()
  const manifest = JSON.parse(await readFile(join(packDir, 'fedcap.json'), 'utf8'))
  manifest.capabilities[0].invoke.command = '{{python}} {{packDir}}/bin/missing.py {{config.nope}}'
  const result = await validateManifest(manifest, { packDir })
  assert.equal(result.ok, false)
  assert.ok(result.errors.some((error) => error.includes('unknown placeholder {{config.nope}}') || error.includes('config.nope')))
  assert.ok(result.errors.some((error) => error.includes('missing pack file')))
})

test('resolves config, python and packDir placeholders', async () => {
  const { packDir } = await makeFixture()
  const resolved = await resolvePack(packDir, { python: '/usr/bin/python3', sets: { baseUrl: 'http://127.0.0.1:1234' } })
  const invoke = resolved.descriptors[0].invoke
  assert.ok(invoke.command.includes('/usr/bin/python3'))
  assert.ok(invoke.command.includes(`${packDir}/bin/fetch.py`))
  assert.equal(invoke.env.DEMO_BASE_URL, 'http://127.0.0.1:1234')
})

test('installs, is idempotent, refuses a conflicting overwrite, and uninstalls', async () => {
  const { packDir, nodeConfig } = await makeFixture()
  const first = await installPack({ packDir, nodeConfigPath: nodeConfig })
  assert.equal(first.changed, true)
  const installed = JSON.parse(await readFile(nodeConfig, 'utf8'))
  assert.deepEqual(installed.capabilitiesFile, ['capabilities.d/demo.json'])
  const descriptors = JSON.parse(await readFile(first.target, 'utf8'))
  assert.equal(descriptors.length, 1)
  assert.equal(descriptors[0].id, 'demo.fetch')

  const second = await installPack({ packDir, nodeConfigPath: nodeConfig })
  assert.equal(second.changed, false)

  await assert.rejects(() => installPack({ packDir, nodeConfigPath: nodeConfig, sets: { baseUrl: 'http://127.0.0.1:2222' } }))

  const forced = await installPack({ packDir, nodeConfigPath: nodeConfig, sets: { baseUrl: 'http://127.0.0.1:2222' }, force: true })
  assert.equal(forced.changed, true)

  const removal = await uninstallPack({ packId: 'demo', nodeConfigPath: nodeConfig })
  assert.equal(removal.removed, true)
  const after = JSON.parse(await readFile(nodeConfig, 'utf8'))
  assert.equal(after.capabilitiesFile, undefined)
})

test('doctor reports a healthy node and catches missing files', async () => {
  const { packDir, nodeConfig } = await makeFixture()
  await installPack({ packDir, nodeConfigPath: nodeConfig })
  const healthy = await doctor({ nodeConfigPath: nodeConfig })
  assert.equal(healthy.ok, true, healthy.problems.join('\n'))
  assert.equal(healthy.capabilities.length, 1)

  const config = JSON.parse(await readFile(nodeConfig, 'utf8'))
  config.capabilitiesFile = ['capabilities.d/missing.json']
  await writeFile(nodeConfig, JSON.stringify(config))
  const broken = await doctor({ nodeConfigPath: nodeConfig })
  assert.equal(broken.ok, false)
  assert.ok(broken.problems.some((problem) => problem.includes('does not exist')))
})

test('accepts a node configuration with a UTF-8 BOM', async () => {
  const { packDir, nodeConfig } = await makeFixture()
  const raw = await readFile(nodeConfig, 'utf8')
  await writeFile(nodeConfig, `\uFEFF${raw}`)
  const result = await installPack({ packDir, nodeConfigPath: nodeConfig })
  assert.equal(result.changed, true)
})

void root
