/**
 * What desktop/plugin.js does, driven for real.
 *
 * The Python suite cannot execute the desktop half, and it is 283 lines of
 * user-facing logic: two of its defects (a poll loop that outlived dispose, a
 * notification that never fired twice) reached a release because it was only ever
 * checked by hand. This is that check, kept.
 *
 * Run by DesktopHalfTests in tests/run_tests.py, which skips when node is not on
 * the machine. Directly:
 *   node tests/desktop_probe.mjs <path to desktop/plugin.js> <empty temp dir>
 *
 * The SDK, react and the jsx runtime are stubbed in the temp directory, because
 * bare imports resolve from the importing file. setTimeout is virtual, so the
 * ten-minute idle poll costs nothing.
 */

import { mkdirSync, copyFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const [pluginPath, workspace] = process.argv.slice(2)
if (!pluginPath || !workspace) {
  console.error('usage: node desktop_probe.mjs <plugin.js> <temp dir>')
  process.exit(2)
}

// -- the stub packages ------------------------------------------------------------

const sdkDir = join(workspace, 'node_modules', '@hermes', 'plugin-sdk')
const reactDir = join(workspace, 'node_modules', 'react')
mkdirSync(sdkDir, { recursive: true })
mkdirSync(reactDir, { recursive: true })
writeFileSync(join(sdkDir, 'package.json'),
  '{"name":"@hermes/plugin-sdk","version":"0","type":"module","main":"index.js"}')
writeFileSync(join(sdkDir, 'index.js'), `
export const calls = { request: [], notify: [] }
export const cn = (...parts) => parts.filter(Boolean).join(' ')
export const haptic = () => {}
export const Tip = 'Tip'
export const host = {
  state: { profile: { get: () => 'default' } },
  request: async (name, args) => {
    calls.request.push([name, args])
    if (globalThis.__backendDown) throw new Error('backend restarting')
    return { output: JSON.stringify(globalThis.__state) }
  },
  notify: (payload) => {
    if (globalThis.__notifyThrows) {
      globalThis.__notifyThrows -= 1
      throw new Error('no notification surface')
    }
    calls.notify.push(payload)
  }
}
`)
writeFileSync(join(reactDir, 'package.json'),
  '{"name":"react","version":"0","type":"module","main":"index.js",' +
  '"exports":{".":"./index.js","./jsx-runtime":"./jsx-runtime.js"}}')
writeFileSync(join(reactDir, 'index.js'),
  'export const useEffect = () => {}\nexport const useState = (v) => [v, () => {}]\n')
writeFileSync(join(reactDir, 'jsx-runtime.js'),
  'export const jsx = (t, p, k) => ({ t, p, k })\nexport const jsxs = jsx\n')

const pluginCopy = join(workspace, 'plugin.mjs')
copyFileSync(pluginPath, pluginCopy)

// -- a virtual clock, so a ten-minute poll is free --------------------------------

let clock = 0
let nextTimer = 1
const timers = new Map()
globalThis.setTimeout = (fn, ms) => {
  const id = nextTimer++
  timers.set(id, { at: clock + Number(ms || 0), fn })
  return id
}
globalThis.clearTimeout = (id) => {
  timers.delete(id)
}
const drain = () => new Promise((resolve) => setImmediate(resolve))

async function advance(ms) {
  const until = clock + ms
  for (;;) {
    const due = [...timers.entries()]
      .filter(([, timer]) => timer.at <= until)
      .sort((a, b) => a[1].at - b[1].at)[0]
    if (!due) break
    const [id, timer] = due
    timers.delete(id)
    clock = timer.at
    timer.fn()
    await drain()
    await drain()
  }
  clock = until
  await drain()
}

// -- the harness ------------------------------------------------------------------

const { calls } = await import(pathToFileURL(join(sdkDir, 'index.js')).href)
const plugin = (await import(pathToFileURL(pluginCopy).href)).default

let failures = 0
function check(name, actual, expected) {
  const got = JSON.stringify(actual)
  const want = JSON.stringify(expected)
  if (got === want) {
    console.log(`ok   ${name}`)
  } else {
    failures += 1
    console.log(`FAIL ${name}\n       expected ${want}\n       got      ${got}`)
  }
}

let store = new Map()
let disposers = []
let recycled = 0
let contribution = null

function context() {
  store = new Map()
  contribution = null
  disposers = []
  calls.request.length = 0
  calls.notify.length = 0
  return {
    storage: {
      get: (key, fallback) => (store.has(key) ? store.get(key) : fallback),
      set: (key, value) => store.set(key, value),
      remove: (key) => store.delete(key)
    },
    register: (entry) => { contribution = entry },
    onDispose: (fn) => disposers.push(fn)
  }
}

function state(over = {}) {
  return {
    brand: 'RC', version: '1.3.12', working: true, latest: null, job: null,
    backend: 'A', hermes: '0.21.4', ...over
  }
}

const withBridge = () => { globalThis.window = { hermesDesktop: { recycleBackend: () => { recycled += 1 } } } }
const withoutBridge = () => { globalThis.window = {} }
const messages = () => calls.notify.map((entry) => entry.message)
const dispose = () => { for (const fn of disposers) fn() }

// What the status bar actually renders, and pressing what it puts there: the
// buttons are the whole point of this half, so the probe drives them, not a copy
// of their handler.
function rendered() {
  const node = contribution.render()
  return typeof node.t === 'function' ? node.t(node.p || {}) : node
}
function walk(node, hit) {
  if (!node || typeof node !== 'object') return null
  const found = hit(node)
  if (found) return found
  const kids = node.p?.children
  for (const kid of Array.isArray(kids) ? kids : [kids]) {
    const deeper = walk(kid, hit)
    if (deeper) return deeper
  }
  return null
}
const button = () => walk(rendered(), (node) =>
  typeof node.p?.onClick === 'function' && typeof node.p?.children === 'string' ? node : null)
const label = () => walk(rendered(), (node) =>
  typeof node.p?.children === 'string' && String(node.p.children).startsWith('RC') ? node.p.children : null)
const pressButton = () => button().p.onClick()

// 1. A finished job is reported once, and does not mute the release notification.
withBridge()
globalThis.__state = state({
  latest: '1.3.13',
  job: { status: 'done', started: 1, restart: false, reply: 'RC is up to date.' }
})
plugin.register(context())
await advance(1)
check('a finished job is toasted once', messages(), ['RC is up to date.'])
check('the status bar offers the update', label(), 'RC · update available: 1.3.13')
check('with a button that says so', button().p.children, 'Update')
await advance(20 * 60 * 1000)
check('the toast is not repeated on later polls', messages(), ['RC is up to date.'])
dispose()

// 2. A restart job with the bridge: honest wording, recycle, and a confirmation
//    only once a different backend answers.
withBridge()
recycled = 0
globalThis.__state = state({
  job: { status: 'done', started: 2, restart: true, reply: 'RC updated to 1.3.13.' }
})
plugin.register(context())
await advance(1)
check('the restart toast says what happens', messages(), ['RC updated to 1.3.13. Restarting Hermes…'])
check('the restart is awaited', store.get('restartingTo'), '1.3.12')
await advance(1600)
check('the backend is recycled', recycled, 1)
await advance(2100)
check('the old backend confirms nothing', messages(), ['RC updated to 1.3.13. Restarting Hermes…'])
globalThis.__state = state({ backend: 'B', version: '1.3.13' })
await advance(2100)
check('a new backend confirms once',
  messages(), ['RC updated to 1.3.13. Restarting Hermes…', 'RC 1.3.13 is running.'])
check('the wait is cleared', store.get('restartingTo'), undefined)
dispose()

// 3. Without the recycle bridge, nothing claims a restart and nothing waits.
withoutBridge()
globalThis.__state = state({
  job: { status: 'done', started: 3, restart: true, reply: 'RC updated to 1.3.13.' }
})
plugin.register(context())
await advance(1)
check('without the bridge the toast is honest',
  messages(), ['RC updated to 1.3.13. It loads the next time Hermes starts.'])
check('without the bridge nothing is awaited', store.get('restartingTo'), undefined)
dispose()

// 4. Dispose is final, for a poll and for a button press alike.
withBridge()
globalThis.__state = state({ latest: '1.3.13' })
const ctx4 = context()
plugin.register(ctx4)
await advance(1)
dispose()
let requests = calls.request.length
await advance(60 * 60 * 1000)
check('dispose stops the poll loop', calls.request.length - requests, 0)

globalThis.__state = state({ working: false })
const ctx5 = context()
plugin.register(ctx5)
await advance(1)
const press = pressButton()   // the status-bar Fix button calls start()
dispose()
await press
requests = calls.request.length
await advance(60 * 60 * 1000)
check('dispose stops a loop a button press started', calls.request.length - requests, 0)

// 6. A backend that cannot answer is not a crash, and polling continues.
withBridge()
globalThis.__state = state()
const ctx7 = context()
plugin.register(ctx7)
await advance(1)
globalThis.__backendDown = true
await advance(11 * 60 * 1000)
globalThis.__backendDown = false
requests = calls.request.length
await advance(11 * 60 * 1000)
check('an unreachable backend does not stop the loop', calls.request.length - requests, 1)
dispose()

// 7. A notification surface that refuses must not swallow the result, and must
//    not stop the update from loading.
withBridge()
recycled = 0
globalThis.__state = state({
  job: { status: 'done', started: 7, restart: true, reply: 'RC updated to 1.3.13.' }
})
const ctx8 = context()
globalThis.__notifyThrows = 1
plugin.register(ctx8)
await advance(1)
check('a refused toast is not marked as shown', store.get('shown:7'), undefined)
check('the restart happens even when the toast failed', store.get('restartingTo'), '1.3.12')
await advance(1600)
check('the backend is recycled even when the toast failed', recycled, 1)
await advance(2100)
check('the result is reported on the next poll instead',
  messages(), ['RC updated to 1.3.13. Restarting Hermes…'])
dispose()

// 8. When the backend is unreachable on first load, retry within seconds instead
//    of leaving the status bar empty for IDLE_POLL_MS (10 minutes).
withBridge()
globalThis.__backendDown = true
const ctx9 = context()
plugin.register(ctx9)
await advance(1)
requests = calls.request.length
globalThis.__backendDown = false
globalThis.__state = state({ working: true })
await advance(3500)
check('an unreachable backend on start retries promptly', calls.request.length - requests, 1)
dispose()

console.log(failures ? `${failures} failed` : 'all ok')
process.exit(failures ? 1 : 0)
