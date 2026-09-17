# Architecture

For someone deciding whether to adopt this, or who has to keep it alive. Not a user guide; the README covers that.

## What it is

A Hermes plugin that reads the agent's own trajectory, finds failures that recur across sessions, and writes one small skill, memory or prompt edit so the agent stops repeating them. Every edit is journaled and reversible.

20,847 lines of Python, standard library plus Hermes. One tool (`refine_run`), one slash command, seven hooks.

## What it touches in your host

This is the section that decides whether the plugin is cheap or expensive to carry. Ranked by how likely it is to break.

| Coupling | What for | Fragility |
|---|---|---|
| `invocation-route-*.patch` — 8 core files | Gives `ctx.llm` the invocation the current turn is bound to | **Breaks on every update** (files overwritten) and on drift (patch stops applying) |
| `hermes_cli.send_cmd.cmd_send` | User notifications | Undocumented. No plugin notification API exists, so this calls a CLI entry point with a synthetic `argparse.Namespace` |
| `hermes_cli.commands.resolve_command` | Detects a built-in `/refine` and renames itself to `/refine-cycle` | Low. Guarded by try/except |
| `agent.plugin_llm.PluginLlmInvocationError` | Route failure classification | Low. Falls back to a local class when absent |
| `agent.subagent_lifecycle` via `ctx.subagent_lifecycle` | Runs the proposer as a subagent | Low. Supported API |
| `agent.auxiliary_client` | Auxiliary call path | Medium |
| `gateway.session_context` | Session identity | Medium |
| `~/.hermes/config.yaml` | Memory limit floor; write-approval (below) | Survives updates — it is user data, not part of the checkout |

See [HOST-PATCH.md](HOST-PATCH.md) for what it adds and how to rebase it. The patched files: `agent/plugin_llm.py`, `agent/turn_context.py`, `agent/turn_facade.py`, `agent/auxiliary_client.py`, `cli.py`, `gateway/run_inbound.py`, `hermes_cli/plugins.py`, `tui_gateway/methods_tools.py`.

Without the patch, `refine_run` stops at `llm_invocation_unavailable` and does nothing. That gate is in `core.py` and is deliberate: the plugin refuses to propose using a model the user did not choose for it.

### It disables the host write-approval gate

On registration, `config.disable_host_write_approval()` rewrites `write_approval: true` inside the `memory:` and `skills:` blocks of `~/.hermes/config.yaml`.

The reason: with that gate on, the host queues **every** memory and skill write, the agent's own included, and nothing lands until a human drains the queue. A plugin whose purpose is improving the agent without anyone clicking approve would look like an agent that silently stopped learning.

The write is the narrowest possible — only that one line inside those two blocks, so comments, key order and every other value survive. A `.refine-bak` copy is kept. An administrator-managed config is left alone. It logs a warning naming what it changed.

If you adopt this, that decision is the first thing to review. It is defensible and it is disclosed, but it is a safety gate being turned off by a plugin.

## How one run works

```
refine_run
  └─ mutation lock: reconcile pending approvals            (lock held)
  └─ collect evidence: this session + cross-session         (no lock)
  └─ fingerprint failures, drop self-corrected ones         (no lock)
  └─ gate: does anything recur ≥2×, or is there a correction?
  └─ propose: one LLM call, structured JSON                 (no lock)
  └─ validate: action, kind, size, injection, duplicates
  └─ apply: backup, write, journal                          (lock held)
```

The lock covers mutations only. Evidence collection and the LLM call run outside it, because a hung provider (28 s measured) would otherwise block every other refine operation on the host.

Recurrence is decided by the plugin, not the model. `patterns.py` normalizes request ids, row counts and temp paths out of error text and hashes what remains, so "this failed again" is a fact the plugin asserts rather than a judgement it delegates.

## Modules

| File | Lines | Does |
|---|---|---|
| `core.py` | 7,589 | Orchestration: evidence, guardrails, durable apply, rollback |
| `journal.py` | 3,096 | Append-only journal, mutation lock, approvals, rollback |
| `llm.py` | 2,538 | Proposal calls, structured output, salvage, route classification |
| `__init__.py` | 1,775 | Registration, hooks, slash command, prompt-note rule enforcement |
| `install.py` | 1,701 | Host classification, patch apply/verify, plugin install |
| `ledger.py` | 1,086 | Whether an applied edit actually helped, measured later |
| `patterns.py` | 919 | Error fingerprinting and aggregation |
| `config.py` | 798 | Settings, host config writes |
| `lesson_effect_checker.py` | 555 | Frozen grader for the experiment programme |
| `sanitization.py` | 332 | Credential redaction, line-structure hygiene |
| `notify.py` | 272 | User notifications through the CLI entry point |
| `notices.py` | 367 | What the user is told and when: releases, a broken or paused plugin, a full memory store; one-tap update and fix, restart |
| `desktop/plugin.js` | 184 | The desktop app's status-bar item and notification with Update / Fix; all decisions stay in `notices.py` |
| `refine_trace.py` | 186 | Sanitized invocation trace |

## What it may write

`_validate_proposal` in `core.py` accepts exactly:

- **action**: `create` or `patch`. Anything else is refused.
- **kind**: `skill`, `memory` or `prompt`. Anything else is refused.
- **content**: non-empty, under `MAX_CONTENT_CHARS`; memory entries additionally under `MEMORY_ENTRY_HARD_LIMIT_CHARS`.

Skill and memory content passes an injection check and a resource check. Memory additionally passes a duplicate check against the store.

Defaults that bound the blast radius: `max_edits_per_run = 1`, `max_edits_per_day = 3`, `auto_cooldown_minutes = 20`, and `max_model_runs_per_day = 30` for spend. Every applied edit is backed up before the write and is reversible by journal id.

## What it stores

Everything under the Hermes home, nothing in the plugin install directory (migrated on first run if an old install left data there):

- journal, append-only, one record per attempt
- backups, one per applied edit
- prompt notes, and the block rules derived from them
- usefulness ledger

## Testing

```
python tests/run_tests.py
```

1,286 tests, standard library `unittest`, no network. Running an individual test file directly will fail on imports; the runner sets the path.

`PathTraceTests` runs every way a pass starts (the `refine_run` tool, `post_llm_call`, `on_session_end`, a deferred session end drained later, the slash command) through the real entry points, against a host double that enforces Hermes's ContextVar rules for the model route and the subagent parent. It asserts the whole trace as one table: trigger, outcome, which proposer ran, why it fell back, which parent the launch saw, and how many structured calls were made.

`install.py --status` reports host state without changing anything: which patch applies, whether all 8 targets carry markers, and whether the plugin is installed.

## Known gaps

- **The spend ceiling counts passes, not tokens.** `max_model_runs_per_day` (default 30) stops a pass before it reaches the model, but one pass can make more than one call (a json_schema attempt retried as json_mode, a reviewer call, a proposer subagent's own API calls), and passes differ in size.
- **A prompt-note rule cannot tell a tool from a shell binary at parse time**, because `pre_llm_call` carries neither `valid_tool_names` nor the toolset config. Enforcement covers it: a bare name blocks both the tool and the binary of that exact name, and core tools and load-bearing binaries are never blocked.
- **The notification path is undocumented coupling** and is expected to break.
- **One filler ordering, one model, one route** in every measurement. See the research report.

## Appendix: implementation notes

Moved from the README.

### Why fingerprinting

"The same failure happened again" is a question about shapes.
`HTTP 429 for /users/8821` and `HTTP 429 for /users/9134` are one failure.
Normalizing volatile parts and hashing the result turns a flat list of error
text into countable patterns.

A pattern that appears in several **different** sessions is stronger evidence
than one repeated twice inside a conversation. Interactive prompts remain
bounded, while `/refine audit` evaluates recurrence over the complete available
post-edit period.

### Provider compatibility

The proposal is requested via `json_schema` structured output, with an automatic
fallback to `json_mode` and then raw-text JSON salvage for providers that reject
`response_format.type=json_schema`.

### Proposer path

Two arms produce the proposal. **The subagent arm is the default**: a read-only
child that can open skill bodies (`skills_list`/`skill_view`) before deciding,
which measurably produces fewer unusable proposals than judging from
name+description alone. It requires a bound parent turn. Hermes binds the
parent through a ContextVar for the agent's turn only, and the automatic pass
runs on a worker thread after the hook returns, so `post_llm_call` and
`on_session_end` capture the parent (as a weak reference) and the worker binds
it for the pass. Before that, the live host refused every automatic launch with
"No active Hermes parent session is available". Where the subagent route is
still unavailable (no parent turn bound, the agent already gone, launch
refused, answer unparsable), the run falls back to the **structured call**,
which judges from bounded name+description overviews. The structured path is a
documented fallback, not the primary route: on long sessions it does not keep
up (in paired measurement it timed out twice out of five passes at the 4,000-row
scan cap), so hosts whose integrations never bind a parent turn get materially
worse proposals on long sessions. Structured proposal and reviewer calls each
have a 180-second timeout; the subagent wait is separately configurable via
`proposer_subagent_timeout_seconds` (default 180).
`proposer_subagent_strict` (default `false`) makes a subagent failure a
journaled error (`subagent_strict_error`) instead of a downgrade.

---


## Repository layout

Runtime modules and installation assets live at the repository root; the
installer copies the shipped plugin subset to `<HERMES_HOME>/plugins/refine/`.

```
Refine-Cycle-for-Hermes-Agent/
├── plugin.yaml          # Hermes plugin manifest
├── __init__.py          # command, tool, and hook registration
├── config.py            # plugins.entries.refine config reader
├── core.py              # evidence, guardrails, serialized apply orchestration
├── sanitization.py      # recursive credential redaction
├── patterns.py          # normalization, fingerprints, aggregation, signal gate
├── ledger.py            # timestamp-aware usefulness ledger and audit report
├── llm.py               # structured proposal, reviewer, and patch regeneration
├── journal.py           # append-only journal, lock, notes, recovery, rollback
├── notify.py            # failure-isolated applied-edit notification delivery
├── refine_trace.py      # synthetic trace helper shipped with the plugin
├── install.py           # cross-platform full installer, status, and rollback
├── assets/              # bundled invocation-route patches and README media
└── tests/
    └── run_tests.py     # hermetic regression and cross-process proof
```

---


## Appendix: stage-by-stage

Moved from the README. The pipeline summary above is the short form of this.

![How the Refine Cycle plugin works: a session ends, repeated failures are found across sessions, the gate opens only on recurrence, one edit is proposed, safety checks run, the edit is journaled then applied, and it is checked later — with three exits where the plugin stops, rejects, or rolls back](../assets/refine-cycle.gif)

```
trajectory (state.db) → scrub → fingerprint + aggregate → signal gate
                                                  ├→ reviewer decline → journaled no_op
                                                  └→ proposal → guardrails + prepare
                                                              → apply → finalized outcome
                                                                      → usefulness ledger
```

| Stage | What happens |
|---|---|
| **1. Collect evidence** | Reads the last N messages of the selected session from `<HERMES_HOME>/state.db` with `mode=ro`. Credentials are redacted before downstream use. |
| **2. Aggregate** | Normalizes errors to invariant shapes, records complete 12-character fingerprints, and counts recurrence within and across sessions. |
| **3. Signal gate and reviewer** | Repeated patterns or explicit corrections reach the proposal model. If neither exists, a substantial session may receive one small, conservative reviewer call; a decline is a sanitized, journaled `no_op`. |
| **4. LLM proposal** | Requests one structured `create`, `patch`, or `no_op` proposal with an optional one-sentence, falsifiable `expected_outcome`. Kinds are `skill`, `memory`, and `prompt`. A proposal may instead carry an `edits` array of inseparable edits under one shared reason, `expected_outcome`, and `summary`. Every model-bound field is sanitized. The proposal output budget is derived locally from the shared 15,000-character content limit and scales with `max_edits_per_proposal`; the reviewer remains separately capped at 2,400 tokens. A cut-off, malformed, or reasoning-only reply is journaled as `llm_incomplete` rather than presented as a normal `no_op`. Skill patches receive the current complete `SKILL.md` only when it is unchanged by scrubbing and no larger than 15,000 characters. |
| **5. Guardrails** | Enforces agent-created patch targets, fresh create names, content/frontmatter, prompt-note policy shape, size limits, daily budget, and recent-duplicate rejection. Every check runs per edit, so a later edit of a transaction is measured against the edits already applied before it. |
| **6. Prepare** | Captures a skill's pre-edit content as both a journal snapshot and a readable `.bak` file, or memory/prompt-note recovery metadata, then appends and `fsync`s a `prepared` journal record before mutation. |
| **7. Apply and reconcile** | Runs the standard host API for skills/memory (`patch` maps to host `edit`) or atomically writes the plugin-owned prompt-note store. It proves target state and records `applied`, `pending_approval`, `conflict`, or `error`. A `conflict` occurs when a skill patch was planned against content that changed before apply, disappeared, or can no longer be read reliably; the budget is not consumed and the edit is not advertised as reversible. Host pending approvals reconcile lazily before later runs, audit, or rollback. |
| **8. Rollback** | Journals `rollback_prepared` before a rollback side effect. A rollback is finalized only after target-state proof; staged host rollbacks remain `pending_rollback` until approval reconciliation. |

---

## Appendix: why fingerprinting carries it

Moved from the README.

## Why

An agent that fixes the same problem every week is not learning. The hard part is
not noticing a failure; it is knowing which failures are *chronic*, and knowing
whether a fix worked.

The table above says what the difference is; the part worth spelling out is why
fingerprinting carries it. Raw error strings never repeat exactly, so volatile
parts — ids, paths, ports, timestamps — have to collapse before "again" means
anything, while genuinely different errors must stay apart. Those two
requirements pull against each other, and every serious defect in this plugin so
far has been one of them winning too hard. An edit is then treated as a
hypothesis with a falsifiable `expected_outcome`, which is what makes a verdict
afterwards possible at all.

Ambiguous trajectories can still receive one conservative reviewer pass rather
than silently ending at the mechanical gate. Reviewer-approved proposals are
journaled as advisory `reviewer_only` outcomes and are never applied without the
normal recurrence evidence.

The base system prompt is never touched. Only **agent-created** skills and
memory entries are editable; built-in, pinned, and hub-installed skills remain
off-limits. Prompt notes live only in **Refine Cycle**'s own store, never in host
memory or a skill.
