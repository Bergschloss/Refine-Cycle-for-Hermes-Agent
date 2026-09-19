/**
 * Refine Cycle in the Hermes desktop app: one status-bar item with [Update] or
 * [Fix]. No system notifications: the plugin already says everything it has to
 * say in chat, and a second copy in the corner of the screen is noise.
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
// Read off the namespace, never imported by name: a named import of an export
// this app does not have fails the whole module, and with it the status bar.
import * as sdk from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'refine'
const COMMAND = 'refine-update'
const IDLE_POLL_MS = 10 * 60 * 1000
const BUSY_POLL_MS = 2000
const STARTUP_POLL_MS = 3000
// How long a restart may take before this stops waiting for its confirmation. A
// backend that never comes back must not leave the poll running at BUSY forever.
const RESTART_WAIT_MS = 2 * 60 * 1000

const listeners = new Set()
let current = null
let pluginCtx = null
let timer = null
let restartTimer = null
let succeeded = false
// Bumped by dispose. A refresh already in flight when the plugin is disposed
// finishes after it, and without this it rescheduled itself and re-fired the
// finished job's toast on every poll -- forever, because the "already shown"
// check reads storage through the context dispose just cleared.
let generation = 0

function publish(state) {
  current = state
  succeeded = true
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

function canRecycle() {
  try {
    return typeof window.hermesDesktop?.recycleBackend === 'function'
  } catch {
    return false
  }
}

function recycleBackend() {
  try {
    const profile = host.state.profile?.get?.()
    const recycle = window.hermesDesktop?.recycleBackend
    if (typeof recycle === 'function') void recycle(profile)
  } catch {
    // Said in the toast as "loads the next time Hermes starts", because
    // canRecycle() answered for that case before the toast was written.
  }
}

function forgetRestart() {
  pluginCtx?.storage.remove('restartingTo')
  pluginCtx?.storage.remove('restartingFrom')
  pluginCtx?.storage.remove('restartingAt')
}

function waitingForRestart() {
  if (!pluginCtx || !pluginCtx.storage.get('restartingTo', '')) return false
  const since = Number(pluginCtx.storage.get('restartingAt', 0)) || 0
  if (since && Date.now() - since > RESTART_WAIT_MS) {
    // The backend never came back. The reply already said what happened, so stop
    // polling fast and stop waiting for a confirmation that is not coming.
    forgetRestart()
    return false
  }
  return true
}

function afterRestart(state) {
  if (!pluginCtx || !state) return
  const expected = pluginCtx.storage.get('restartingTo', '')
  if (!expected) return
  // The backend that ran the update keeps answering until it is actually gone,
  // and it reads the new version straight off disk. Confirm only once a different
  // backend process answers, or this says "is running" about the old code.
  const from = pluginCtx.storage.get('restartingFrom', '')
  if (from && state.backend === from) return
  try {
    host.notify({
      kind: 'success',
      message: state.working ? `${state.brand} ${state.version} is running.` : `${state.brand} is working again.`
    })
  } catch {
    // Still waiting, so the next poll tries again until the deadline in
    // waitingForRestart() runs out. Forgetting first would lose it for good.
    return
  }
  forgetRestart()
}

async function refresh() {
  const mine = generation
  try {
    // Tell the backend whether this app renders the `::refine` card. On an app
    // without transcript directives the card would show under a reply as raw
    // text, so the backend only appends it when this says it can render.
    const state = await ask(CARDS ? 'desktop-state cards' : 'desktop-state')
    if (mine !== generation) return
    publish(state)
    afterRestart(state)
    const job = state.job
    if (job && job.status === 'running') {
      schedule(BUSY_POLL_MS)
      return
    }
    if (!pluginCtx) return
    if (job && job.status === 'done' && !pluginCtx.storage.get(`shown:${job.started}`, false)) {
      // The restart sentence is written here, not by the backend: this side is
      // the one that knows whether it can recycle the backend at all.
      const tail = job.restart
        ? (canRecycle() ? ' Restarting Hermes…' : ' It loads the next time Hermes starts.')
        : ''
      try {
        host.notify({ kind: job.restart ? 'success' : 'info', message: `${job.reply}${tail}` })
        // Marked as reported only once it was reported: a toast that threw would
        // otherwise lose the only account of what the update did.
        pluginCtx.storage.set(`shown:${job.started}`, true)
      } catch {
        // Nothing else changes. The restart below does not depend on the toast:
        // an update that installed has to load either way.
      }
      // Without the bridge there is no restart to wait for, so no fast polling
      // and no confirmation to expect either.
      if (job.restart && canRecycle()) {
        pluginCtx.storage.set('restartingTo', state.version)
        pluginCtx.storage.set('restartingFrom', state.backend || '')
        pluginCtx.storage.set('restartingAt', Date.now())
        restartTimer = setTimeout(recycleBackend, 1500)
        // Keep polling. Recycling the backend leaves this renderer mounted, so
        // nothing else would ever call refresh() again, and the confirmation
        // ("… is running.") is only sent once the new backend answers.
        schedule(BUSY_POLL_MS)
        return
      }
    }
  } catch {
    // Plugin not loaded on the backend yet, or the backend is restarting.
  }
  if (mine !== generation) return
  schedule(waitingForRestart() ? BUSY_POLL_MS : (!succeeded ? STARTUP_POLL_MS : IDLE_POLL_MS))
}

async function start() {
  haptic('tap')
  // Same generation check as refresh(): a press whose answer lands after dispose
  // must not publish to a torn-down context, and must not start a poll loop that
  // nothing can stop.
  const mine = generation
  try {
    const state = await ask('desktop-start')
    if (mine !== generation) return
    publish(state)
  } catch {
    // The next refresh shows whatever state the backend is in.
  }
  if (mine !== generation) return
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
    // Said before the press: an update or a fix restarts Hermes.
    label: !busy && (fix || update) ? `${fix ? 'Fix' : 'Update'}: Hermes will restart` : state.brand,
    children: jsxs('span', {
      className: cn('inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem]', 'text-(--ui-text-tertiary)'),
      children
    })
  })
}

/**
 * `::refine{}` — the same Update / Fix decision as a card inside the chat.
 *
 * Hermes renders a widget or a card ONLY inside an assistant message: a plugin's
 * own command output arrives as a `system` message and is drawn as plain linkified
 * text, so a plugin cannot put a button in chat by answering. What it CAN do is
 * claim a directive name, which is what this is: when the agent writes `::refine{}`
 * on its own line, this card renders there, with buttons wired straight to the
 * plugin's backend command -- no HTML file, no hidden user turn, no model call.
 *
 * The area is the literal string on purpose. Importing the SDK constant would make
 * the whole desktop half fail to load on an app that does not export it yet; a
 * name this app does not know is simply an area nobody reads.
 */
const DIRECTIVE_AREA = 'transcript.directives'
const DIRECTIVE_NAME = 'refine'
const CARDS = typeof sdk.TRANSCRIPT_DIRECTIVE_AREA === 'string'

function RefineCard() {
  const [state, setState] = useState(current)

  useEffect(() => {
    listeners.add(setState)
    void refresh()
    return () => listeners.delete(setState)
  }, [])

  const brand = state ? state.brand : 'Refine Cycle'
  const busy = Boolean(state && state.job && state.job.status === 'running')
  const fix = Boolean(state && !state.working)
  const update = Boolean(state && state.working && state.latest)
  const line = !state
    ? 'checking…'
    : busy
      ? `${fix ? 'fixing' : 'updating'}…`
      : fix
        ? 'stopped working after the Hermes update'
        : update
          ? `update available: ${state.latest}`
          : `${state.version} is running`

  const children = [
    jsx('span', { className: cn('font-medium'), children: brand }, 'brand'),
    jsx('span', { className: cn('text-(--muted-foreground)'), children: line }, 'line')
  ]
  if (state && !busy && (fix || update)) {
    children.push(
      jsx(
        'button',
        {
          className: cn(
            'rounded border border-(--ui-border) px-2 py-0.5 text-xs',
            'hover:bg-(--chrome-action-hover) hover:text-foreground'
          ),
          type: 'button',
          onClick: () => void start(),
          children: fix ? 'Fix' : 'Update'
        },
        'action'
      )
    )
    // Said before the press: it restarts Hermes and cuts off work in progress.
    children.push(
      jsx('span', { className: cn('text-xs text-(--muted-foreground)'), children: 'Hermes will restart' }, 'warning')
    )
  }

  return jsxs('span', {
    className: cn('my-2 inline-flex items-center gap-2 rounded-md border border-(--ui-border)',
      'bg-(--card) px-3 py-2 text-[0.8125rem]'),
    children
  })
}

export default {
  id: ID,
  name: 'Refine Cycle',
  description: 'Shows whether Refine Cycle works, with one-click Update and Fix.',
  register(ctx) {
    pluginCtx = ctx
    ctx.register({ id: 'status', area: 'statusBar.left', order: 900, render: () => jsx(RefineStatus, {}) })
    // Claimed once, used whenever an assistant message carries `::refine{}`. An
    // app that does not know this area ignores the registration.
    ctx.register({
      id: 'transcript.refine',
      area: DIRECTIVE_AREA,
      data: { name: DIRECTIVE_NAME, render: () => jsx(RefineCard, {}) }
    })
    ctx.onDispose(() => {
      generation += 1
      clearTimeout(timer)
      timer = null
      // A restart scheduled just before dispose must not fire for a plugin that
      // is gone.
      clearTimeout(restartTimer)
      restartTimer = null
      listeners.clear()
      pluginCtx = null
      current = null
      succeeded = false
    })
    void refresh()
  }
}
