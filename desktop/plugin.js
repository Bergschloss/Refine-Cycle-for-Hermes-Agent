/**
 * Refine Cycle in the Hermes desktop app: one status-bar item with [Update] or
 * [Fix], and a Windows/macOS notification with the same button when the app is
 * in the background.
 *
 * The plugin's Python half owns every decision and every word. This file only
 * asks it for the state (`refine-update desktop-state`), starts the work
 * (`refine-update desktop-start`), polls until it is done, and then restarts the
 * desktop backend so the new code loads — the same restart as Settings ▸
 * "Restart backend". The work runs in the background because Hermes stops
 * waiting for a plugin command after 30 seconds, and an install takes longer.
 *
 * Plain ESM, loaded uncompiled by the desktop runtime loader: jsx() calls, and
 * only @hermes/plugin-sdk, react and react/jsx-runtime resolve.
 */

import { cn, haptic, host, Tip } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'refine'
const COMMAND = 'refine-update'
const IDLE_POLL_MS = 10 * 60 * 1000
const BUSY_POLL_MS = 2000

const listeners = new Set()
let current = null
let pluginCtx = null
let timer = null

function publish(state) {
  current = state
  for (const listener of listeners) listener(state)
}

async function ask(arg) {
  const result = await host.request('command.dispatch', { name: COMMAND, arg })
  const output = result && typeof result === 'object' ? result.output : result
  return JSON.parse(String(output || '{}'))
}

function schedule(ms) {
  clearTimeout(timer)
  timer = setTimeout(refresh, ms)
}

function recycleBackend() {
  try {
    const profile = host.state.profile?.get?.()
    const recycle = window.hermesDesktop?.recycleBackend
    if (typeof recycle === 'function') void recycle(profile)
  } catch {
    // The reply already said Hermes is restarting; a missing bridge only means
    // the new code loads on the next start.
  }
}

function announce(state) {
  if (!pluginCtx || !state || state.job) return
  const key = state.working ? (state.latest ? `update:${state.latest}` : '') : `fix:${state.version}`
  if (!key || pluginCtx.storage.get('announced', '') === key) return
  pluginCtx.storage.set('announced', key)
  const fix = !state.working
  pluginCtx.os.notify({
    title: state.brand,
    body: fix
      ? `${state.brand} stopped working after the Hermes update.`
      : `${state.brand} — update available: ${state.latest}.`,
    actions: [{ id: fix ? 'fix' : 'update', label: fix ? 'Fix' : 'Update', onAction: () => void start() }],
    onActivate: () => void start()
  })
}

function afterRestart(state) {
  if (!pluginCtx || !state) return
  const expected = pluginCtx.storage.get('restartingTo', '')
  if (!expected) return
  pluginCtx.storage.remove('restartingTo')
  host.notify({
    kind: 'success',
    message: state.working ? `${state.brand} ${state.version} is running.` : `${state.brand} is working again.`
  })
}

async function refresh() {
  try {
    const state = await ask('desktop-state')
    publish(state)
    afterRestart(state)
    const job = state.job
    if (job && job.status === 'running') {
      schedule(BUSY_POLL_MS)
      return
    }
    if (job && job.status === 'done' && !pluginCtx?.storage.get(`shown:${job.started}`, false)) {
      pluginCtx?.storage.set(`shown:${job.started}`, true)
      host.notify({ kind: job.restart ? 'success' : 'info', message: job.reply })
      if (job.restart) {
        pluginCtx?.storage.set('restartingTo', state.version)
        setTimeout(recycleBackend, 1500)
        return
      }
    }
    announce(state)
  } catch {
    // Plugin not loaded on the backend yet, or the backend is restarting.
  }
  schedule(IDLE_POLL_MS)
}

async function start() {
  haptic('tap')
  try {
    publish(await ask('desktop-start'))
  } catch {
    // The next refresh shows whatever state the backend is in.
  }
  schedule(BUSY_POLL_MS)
}

function RefineStatus() {
  const [state, setState] = useState(current)

  useEffect(() => {
    listeners.add(setState)
    return () => listeners.delete(setState)
  }, [])

  if (!state) return null

  const busy = state.job && state.job.status === 'running'
  const fix = !state.working
  const update = state.working && state.latest
  const label = busy
    ? `${state.brand} · ${fix ? 'fixing' : 'updating'}…`
    : fix
      ? `${state.brand} · not working`
      : update
        ? `${state.brand} · update available: ${state.latest}`
        : `${state.brand} ${state.version} · working`

  const children = [jsx('span', { children: label }, 'label')]
  if (!busy && (fix || update)) {
    children.push(
      jsx(
        'button',
        {
          className: cn(
            'ml-1 rounded border border-(--ui-border) px-1.5 leading-4',
            'hover:bg-(--chrome-action-hover) hover:text-foreground'
          ),
          type: 'button',
          onClick: () => void start(),
          children: fix ? 'Fix' : 'Update'
        },
        'action'
      )
    )
  }

  return jsx(Tip, {
    label: state.brand,
    children: jsxs('span', {
      className: cn('inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem]', 'text-(--ui-text-tertiary)'),
      children
    })
  })
}

export default {
  id: ID,
  name: 'Refine Cycle',
  description: 'Shows whether Refine Cycle works, with one-click Update and Fix.',
  register(ctx) {
    pluginCtx = ctx
    ctx.register({ id: 'status', area: 'statusBar.left', order: 900, render: () => jsx(RefineStatus, {}) })
    ctx.onDispose(() => {
      clearTimeout(timer)
      listeners.clear()
      pluginCtx = null
    })
    void refresh()
  }
}
