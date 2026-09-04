# Refine Cycle for Hermes Agent

![Refine Cycle — a self-improvement plugin for Hermes Agent](assets/banner.gif)

**A measurement layer for Hermes Agent's self-improvement.** Hermes already
learns from the conversation in front of it. Refine Cycle asks a different
question: *what keeps breaking across many sessions, and did fixing it actually
help?*

It reads the agent's own trajectory, normalizes errors into comparable shapes,
counts how often each shape recurs **and in how many separate sessions**, then
proposes the **smallest possible edit** — an agent-created skill, a memory entry,
or a bounded, plugin-owned prompt note. Every mutation is prepared in a durable
journal before it runs, carries conflict-aware recovery metadata, and is later
graded on whether the failure it targeted actually stopped.

This is a port of the `/refine` concept from
[Prime Intellect's Prime Agent](https://www.primeintellect.ai/blog/prime-agent)
(Continual Harness) built on the Hermes plugin system. The plugin only loads
when it is explicitly enabled; it does not modify Hermes itself.

![What it does: a mistake happens twice or more, the plugin writes a fix, and the loop continues next session](assets/what-it-does.gif)

> **One thing to know before you install.** Status, audit, rollback, and
> journaling work on a stock Hermes host. **New proposals** additionally need
> the host route patch that `install.sh` applies to the Hermes checkout (see
> "Host route patch" under Installation). Without it, a proposal run fails
> loudly with `llm_invocation_unavailable` — it never pretends to work.

---

## How this differs from Hermes's built-in self-improvement

Hermes ships its own background review: after a turn or a session it looks at the
current conversation and saves what is worth keeping — a useful tactic, a user
preference, a correction. It answers **"is there something here worth
remembering?"**

Refine Cycle answers a different question, over a different window, and then
checks its own work:

| | Hermes background review | Refine Cycle |
|---|---|---|
| **Trigger** | anything worth keeping | the same failure, at least twice |
| **Window** | the current session | many sessions |
| **Evidence** | the conversation as written | errors normalized to invariant shapes and fingerprinted, so `HTTP 429 for /users/8821` and `HTTP 429 for /users/9134` count as one failure |
| **Threshold** | qualitative judgement | a mechanical signal gate: recurrence count *and* distinct-session count |
| **After the edit** | — | grades it: `working`, `did not help`, `unused`, `churning` — or names honestly why no verdict exists yet (`too early`, `no recurrence window`, `unreliable`) |
| **Blast radius** | host policy | 3 edits/day, dedup window, cooldown, per-edit journal, per-edit rollback |

The two are complementary, not alternatives. Hermes captures fresh experience;
Refine Cycle hunts chronic failures and measures whether its own fixes held.

Both can write to the same skills and memory, so the plugin is built to notice
that: a skill patch is refused outright when the target changed after planning,
and `/refine audit` reports when an entry it created was modified by something
else — because an effectiveness verdict on a file someone else edited is not a
verdict worth trusting.

---

## Why

An agent that fixes the same problem every week is not learning. The hard part is
not noticing a failure — it is knowing which failures are *chronic*, and knowing
whether a fix worked.

The table above says what the difference is; the part worth spelling out is why
fingerprinting carries it. Raw error strings never repeat exactly, so volatile
parts — ids, paths, ports, timestamps — have to collapse before "again" means
anything, while genuinely different errors must stay apart. Those two
requirements pull against each other, and every serious defect in this plugin so
far has been one of them winning too hard. An edit is then treated as a
hypothesis with a falsifiable `expected_outcome`, which is what makes a verdict
afterwards possible at all.

Ambiguous trajectories still get one conservative reviewer pass rather than
silently ending at the mechanical gate, so a real lesson with no repeat count is
not lost.

The base system prompt is never touched. Only **agent-created** skills and
memory entries are editable; built-in, pinned, and hub-installed skills remain
off-limits. Prompt notes live only in Refine Cycle's own store, never in host
memory or a skill.

---

## How it works

![How the Refine Cycle plugin works: a session ends, repeated failures are found, the gate opens only on recurrence, one edit is proposed, safety checks run, the edit is journaled then applied, and it is checked later — with three exits where the plugin stops, rejects, or rolls back](assets/refine-cycle.gif)

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

### Why fingerprinting

"The same failure happened again" is a question about shapes, not strings.
`HTTP 429 for /users/8821` and `HTTP 429 for /users/9134` are one failure, not
two. Normalizing volatile parts and hashing the result turns a flat list of
error text into countable patterns.

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
name+description alone. It requires a bound parent turn — on hosts or in call
forms where the subagent route is unavailable (no parent turn bound, launch
refused, answer unparsable), the run falls back to the **structured call**,
which judges from bounded name+description overviews. The structured path is a
documented fallback, not the primary route: on long sessions it does not keep
up (in paired measurement it timed out twice out of five passes at the 4,000-row
scan cap), so hosts whose integrations never bind a parent turn get materially
worse proposals on long sessions. The 45-second timeout on the structured
path and reviewer reads is deliberate and was not raised; the subagent
wait is separately configurable via `proposer_subagent_timeout_seconds`
(default 180). `proposer_subagent_strict` (default `false`) makes a subagent
failure a journaled error (`subagent_strict_error`) instead of a downgrade.

---

## Installation

> **Note:** this is a plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/docs). It needs the plugin API available since Hermes 0.17.0 and does not run standalone. Install, registration, the full test suite (777 tests), `/refine status`, and `/refine audit` are verified on Hermes 0.20.1; the subagent proposal path (launch, fallback, strict) is additionally verified end to end on 0.20.2. Only **new proposals** additionally require the host route patch (see below).

The plugin lives in `<HERMES_HOME>/plugins/refine/` — `~/.hermes/plugins/refine/`
on Linux and macOS, and `%LOCALAPPDATA%\hermes\plugins\refine\` on Windows.
Under a Hermes profile it follows that profile; the plugin resolves the location
through `hermes_constants.get_hermes_home()`.

> **Runtime data location.** The default `journal_dir` is
> `<HERMES_HOME>/refine`, separate from plugin source. On startup, an install
> still using the former `<HERMES_HOME>/plugins/refine` default is migrated under
> a cross-process lock: all artifacts are staged first, a completion marker is
> published last, and the old directory is renamed rather than deleted. If any
> copy or publication step fails, the intact legacy directory remains the active
> store for that process and `/refine status` reports the fallback. An explicitly
> configured non-empty `journal_dir` is never migrated automatically.

Install and enable it from the public repository:

```bash
hermes plugins install Bergschloss/Refine-Cycle-for-Hermes-Agent
hermes plugins enable refine
hermes gateway restart
```

`plugins install` clones the repository into `<HERMES_HOME>/plugins/refine/`,
`plugins enable` registers it with Hermes, and the restart activates it in the
running gateway. This exact sequence is verified end to end on Hermes 0.20.1
and 0.20.2; see VERIFICATION.md.

> **The plugin works inside the running gateway.** The LLM invocation route is
> bound by the live gateway process, so in a bare command-line process
> `refine_run` returns `llm_invocation_unavailable` by design (and `/refine
> status` names that blocker directly). Automatic refinement, proposals, and
> apply/rollback all run inside the gateway — test with a real session or the
> restart above, not with a one-shot script.

Then, optionally, configure it in `config.yaml`:

```yaml
plugins:
  enabled:
    - refine
  entries:
    refine:
      journal_dir: "<HERMES_HOME>/refine-data"   # keep data separate from plugin source
      llm:
        allow_model_override: false
        allow_provider_override: false
```

`plugins enable` manages the `enabled` list itself; the `entries` block holds
the plugin's own settings, and `journal_dir` keeps runtime data separate from
plugin source (see "Runtime data location" above).

Restart Hermes after any config change:

```bash
hermes gateway restart
```

Verify:

```
hermes plugins list
# refine  0.13.0  Measurement layer ...  enabled
```

Then check that automatic refinement can actually run:

```
/refine status
# auto: on
# turn interval: 25
# min messages: 15
# cooldown: 20 min
# edits today: 0/3
# model: your-cheap-model @ your-provider (source: live)
# journal: /home/you/.hermes/refine-data (does not exist yet, will be created on first write)
# blockers: none — automatic refinement is active
```

`blockers` lists every reason a pass would not start; `warnings` lists what does
not stop it but will cost you later, such as runtime data sitting in the plugin
directory, or a journal directory that could not be inspected at all.

Status is read-only: it creates no directory — not even the journal directory it
reports on — writes no journal record, spends no budget, and calls no model. It
does not reconcile pending approvals, so an unresolved staged edit still counts
toward the budget it reports.

### Host route patch — required for new proposals

The plugin asks the LLM through Hermes's *active invocation route*: the same
model binding that the user's live session uses, so that a proposal costs the
host's own provider creds and never a hardcoded key. Stock Hermes does not
expose that binding to plugins. The installer ships one patch per Hermes base —
currently `assets/invocation-route-v2026.8.16.patch` and
`assets/invocation-route-v2026.8.31.patch` — each touching nine files including
`agent/plugin_llm.py`, `agent/auxiliary_client.py`, `gateway/run.py`, and
`hermes_cli/plugins.py` in the Hermes checkout.

Which patch fits a host is decided by **trying** it with `git apply --check`,
newest base first, not by comparing version strings. Hermes moved 72 commits
across these files between v2026.8.16 and v2026.8.31, and the 8.16 patch still
lands 39 of its 40 hunks on 8.31 — so a version test would refuse hosts a patch
fits and accept hosts it does not.

- **Without the patch:** `/refine status`, `/refine audit`, `/refine rollback`,
  journaling, and the test suite all work. A proposal run stops honestly with
  `llm_invocation_unavailable` and journals the record.
- **With the patch:** proposal runs reach the model (subject to the configured
  trust policy).

`install.sh` pins the **result**, not the input. It first checks whether the
route is already present — in which case it does nothing at all — then tries
the patch with `git apply`, falling back to three-way merge with decreasing
context (`-3`, `-3 -C1`, `-3 -C0`), and refuses **only** when it cannot produce
a working route. After applying it verifies by outcome: route symbol present,
no conflict markers, every touched file compiles, and the core module still
imports. If any check fails, the pre-patch state is restored byte-for-byte. A
refusal names the host HEAD and the patch base (the patch was built against
stock v2026.8.16, commit `df4b65147d`) and lists the failed attempts — it
never refuses without trying. A host whose core version has drifted slightly
will often still get a working patch; one that genuinely cannot is told so.

```bash
# from the plugin directory
./install.sh            # apply core patch (with backup)
./install.sh --patch-only
```

On hosts that already carry the route (patched earlier, or upstream), the
script detects it and does nothing. See the script's header for the exact
behaviour and the one-command undo (`git apply -R`).

### The memory budget the install raises

`install.py` raises Hermes's memory character limit to a floor of **4400**. This
is deliberate and it is for the plugin's sake, so it is stated here rather than
left to be discovered in a diff.

Stock Hermes ships `memory_char_limit: 2200` — roughly 800 tokens. That number was
chosen when the models driving Hermes were smaller and shorter-context; a compact
store was the right trade then. It is no longer the constraint it was, and current
models carry 4400 characters of durable memory without difficulty.

For this plugin the stock size is actively too small. Refine's whole output is
lessons written into that store, and it accumulates: on a real install, six applied
edits consumed about a third of the stock budget in a single day. A plugin that
fills the store it depends on is not usable at 2200.

Two files are changed, because neither alone reaches everybody:

- `<HERMES_HOME>/config.yaml` — Hermes writes `memory_char_limit` into the
  generated config, so for anyone who has already run Hermes this file is what
  decides, and the code default is never consulted.
- `hermes_cli/config_defaults.py` in the Hermes checkout — what a user who
  installs the plugin *before* Hermes has ever generated a config will get.
  `--plugin-only` skips this one, since that flag promises no writes into the host
  checkout, and says so at the time.

The rule is a **floor, not an override**:

| Current value | What happens |
|---|---|
| below 4400 (including the stock 2200) | raised to 4400 |
| exactly 4400 | nothing |
| above 4400 | left alone — your number wins |

So a limit you chose yourself is never overwritten and never *lowered*, and
running the installer twice changes nothing the second time. `--rollback` reverses
it by putting each file's own previous number back — not a blanket 2200, so a host
that installed at 3000 returns to 3000. It reverses one integer rather than
restoring a file copy, because `config.yaml` is a live file you edit and a restored
copy would silently discard everything else you changed since.

The plugin itself never hardcodes 4400. It reads whatever limit the host reports
and shows it to you at every write (for example `memory 1443/4400`), so raising the limit
further is a host decision the plugin follows rather than fights.

---

## Usage

### What to expect

A pass on quiet data is a `no_op` — that is the normal, correct result, not a
failure. The outcome families are `no_op`, `applied`, `rejected`,
`pending_approval`, `conflict`, `llm_incomplete`, `llm_invocation_unavailable`,
and `failed`, plus the rollback and grading terms in `/refine audit`.

A real 90-day history (the long-running install this README was tested
against) shows eight refine-created entries whose effectiveness verdicts
distribute between `too early`, `rolled back`, `rejected`, and
`unreliable` — with `unreliable` meaning *someone else modified the artifact
after refine touched it*, so no verdict is possible. Expect exactly that mix:
most passes doing nothing, some edits reverting, and very few edits surviving
to a `working` verdict.

### Manual

```
/refine
/refine focus on Gmail API failures
/refine audit
/refine status
/refine dry-run
/refine dry-run focus on Gmail API failures
/refine dry-run session <session_id>
/refine session <session_id>
/refine model
/refine model your-cheap-model
/refine model your-provider/your-cheap-model
/refine model auto
/refine rollback 1f2a3b4c5d6e
```

`audit`, `status`, `dry-run`, `model`, `session <session_id>`, and
`rollback <12-character-id>` are exact subcommands. `status` reports whether
automatic refinement is active, which session and database source would be
analyzed, configured source skips, what blocks refinement, which model it will
use, and the active journal/migration state. `dry-run [reason]` runs the normal
proposal path and journals the preview without applying an edit or consuming the
daily edit budget. `dry-run session <session_id>` previews one exact historical
session after confirming it through the read-only Hermes sessions table.

`model` shows or sets the model refine asks for. Bare `model` prints the
effective target and whether host trust allows it; `model <name>` or
`model <provider>/<name>` pins one; `model auto` removes the override. `auto`
returns to the next source in the priority order, which is the configured
`plugins.entries.refine.llm` value when there is one, and the live Hermes model
only when there is not. The override is stored in `model_override.json` inside
`journal_dir` — refine does not put its own settings in the Hermes config. It
writes there exactly once, for one key that is not its own: see below.

Both stores are validated the same way: a provider must be a single token, a
model id may be namespaced, and a value matching a credential pattern is refused
rather than stored. A configured value that fails either rule is dropped and
reported in `/refine status` and `/refine model`.

In the command, **the first slash is always the provider separator** and every
later one belongs to the model id: `/refine model openrouter/deepseek/deepseek-chat`
pins provider `openrouter` and model `deepseek/deepseek-chat`. There is therefore
no command form for a namespaced model with no provider — set
`plugins.entries.refine.llm.model` for that. And a pinned provider only reaches
the host when `allow_provider_override` is true, which `/refine model` reports.

Other text is passed to the proposal model as the manual reason. That includes
text beginning with a subcommand word, with one deliberate exception: after
`model`, a single token shaped like an identifier (`deepseek-v4`, `a/b`) is
treated as a target, so `/refine model drift` pins a model rather than asking for
a refinement about drift. Use `/refine drift` or `/refine model auto` to undo.

### Automatic refinement

Automatic refinement is **enabled by default** (`auto_enabled: true`). After
enabling the plugin and restarting Hermes, it begins analyzing sessions and
proposing improvements without additional configuration. To disable it, set
`auto_enabled: false` in `plugins.entries.refine`.

`post_llm_call` counts the assistant messages in the history Hermes supplies and
starts at most one background refinement attempt once that count has grown by
`auto_turn_interval` since this session's previous attempt. It compares a delta
rather than an exact multiple, because a single tool-using turn appends several
assistant messages and would otherwise step straight over the boundary. The hook
itself does not mutate or queue work. It skips an attempt when another pass owns
the lock, and derives its cooldown from durable journal records, so the cooldown
is visible across processes. `on_session_end` remains a background fallback based
on the minimum message count.

```yaml
plugins:
  entries:
    refine:
      auto_enabled: true
      auto_min_messages: 15
      auto_turn_interval: 25
      auto_cooldown_minutes: 20
```

Automatic and manual runs share a cross-thread and cross-process mutation lock,
then recheck the daily budget inside that lock.

### Reviewer fallback

When `min_signal_required` is enabled but the mechanical gate finds neither a
repeated pattern nor an explicit correction, a substantial session can receive
one structured reviewer call (`max_tokens: 300`). It asks only whether there is
a durable lesson worth persisting. The reviewer has its own cooldown.

A reviewer decline, malformed verdict, or reviewer error never reaches the
proposal call. Declines are recorded as sanitized `no_op` journal entries so
they can be audited. An approval supplies its narrow instructions to the normal
proposal flow; it does not bypass guardrails, budget, backups, approvals, or the
journal.

### Prompt notes and scope

A `prompt` proposal creates a short conditional policy in
`<journal_dir>/prompt_notes.json`. Valid notes contain one or two policy lines
beginning with `When <specific condition>, <one action>.`; they are not skills,
memories, procedures, or system-prompt replacements.

`pre_llm_call` returns a self-labelled `Refine notes:` context block. Hermes
adds that ephemeral context to the current turn; Refine Cycle never reads or
writes the base system prompt. Injection is bounded by
`prompt_notes_max_count` and `prompt_notes_max_chars`; when necessary it drops
whole oldest notes, never partial text. Empty, unavailable, unsafe, or
out-of-scope note stores inject nothing and do not raise on the user path.

Injection prefers the mutation lock but does not depend on it: the store is only
ever replaced atomically, so a running refine pass never costs a turn its notes.

New prompt notes use `prompt_notes_default_scope`:

- `global` (the default) is injected in every session.
- `session` stores the session identifier resolved while reading `state.db` and
  injects only when the hook receives that same identifier. Session notes are
  removed from the plugin-owned store after `on_session_end` or
  `on_session_reset` for that session.

Cleanup runs on the host's callback thread, so it waits only briefly for the
mutation lock instead of the full lock timeout. If a refine pass still owns the
lock, the note is left in place — it can no longer be injected, because its
session is gone — and it is removed at the next end or reset for that id.

That expiry is itself journaled, so a crash cannot turn "the note landed and was
then cleaned up" into "the note never landed": the entry moves `applied` (or
`prepared`, for a note that landed before its own finalization completed) →
`cleanup_prepared`, fsynced *before* the store changes, and only reaches
`cleanup_resolved` once the exact note is proven absent from a fresh read. Both
states count against the daily budget, because the edit really happened — normal
session expiry is not a refund and not rollback evidence. Consequently a
session-scoped note stops being reversible once its session ends: `/refine
rollback <id>` then reports the entry as not reversible, since the artifact it
would remove is already gone. Ledger rows for the two states read *session
cleanup pending* and *session note expired*.

Cleanup removes only a note whose id, content, scope, and session still match
the intent recorded in the journal. A note that was hand-edited or moved to
another scope or session is **retained** and reported by id, and an entry already
at `cleanup_prepared` stays there until the store is repaired. That is
deliberate — refine does not delete what it cannot prove it owns — but it does
not clear itself; see *Known integration gaps*.

The prompt-note store is plugin-owned, so there is **no host approval gate** for
these notes. Creation, target-state proof, audit rows, and conflict-aware
rollback are still journaled; host approval remains in force for host-managed
skills and memory.

### Host write approval is turned off on load

If `memory.write_approval` or `skills.write_approval` is on, refine sets it to
`false` when it registers, logs a warning naming what it changed, and leaves a
copy of the previous file at `config.yaml.refine-bak`.

That is a deliberate exception to "refine does not write to the Hermes config",
and it exists because the gate does not do what its name suggests to an
autonomous plugin. It queues **every** memory and skill write — the agent's own as
much as refine's — and nothing lands until a human drains the queue by hand.
Nothing reports that. It presents as an agent that quietly stopped learning:
memory unchanged, skills missing, no error anywhere. In one real install it ran
that way for days, with 3 memory writes and 25 skill writes stranded and four
skills the agent believed it had saved absent from disk.

The write is the narrowest one possible: only a `write_approval: true` line inside
the `memory:` or `skills:` block is rewritten, so comments, key order and every
other value survive. The same key under any other section is left alone, and a
config pinned by an administrator (managed scope) is never touched — there refine
only warns. `/refine status` reports the gate whenever it is on, so re-enabling it
later is visible rather than silent.

If you want approval gating on those subsystems, disable refine instead of turning
the gate back on; the two are answers to the same question and only one of them
can win.

### How a memory entry is identified for rollback, and one disclosed gap

Adding a memory entry goes through the host's gated memory tool, so with
`memory.write_approval` enabled it stages as `pending_approval` like any other
gated write. **Removing it does not go through that gate**, and that is a
deliberate trade rather than an oversight.

The host's removal identifies an entry by *substring*, and pops a single match
even when that match is a strict superstring of the text it was given. Under the
gate a removal is staged and replayed later, so between staging and approval the
entry can be replaced or extended — and the replay would then delete the **user's**
entry. That is a delete of something refine never created, which this plugin may
never do, and it would outrank the value of the gate.

So refine removes its own append itself: it re-reads under the host's per-file
memory lock, proves the entry is its own — exact content, at or after the position
recorded when the edit was planned, with everything before that position pinned by
a digest — and deletes only that entry, all inside the lock. If its exact text is
no longer there, rollback refuses and removes nothing, and the entry stops being
advertised as reversible. A longer entry that merely contains refine's text is not
a problem: identification is by exact content, not substring.

Two consequences worth knowing:

- A memory rollback is not reviewable through `memory.write_approval`. Skill
  rollback does stage under `skills.write_approval` — but note that staging does
  not make it safer in this respect: the host replays a staged skill delete by
  name, without re-checking content, so a skill edited during the approval window
  is deleted as approved. Rolling back a refine-created skill while skill write
  approval is on is best done promptly, or not at all if the skill has since been
  edited by hand.
- With the gate on and an interactive prompt registered, the *forward* memory
  write can block on that prompt while the refine pass holds the shared mutation
  lock, so a concurrent `/refine` waits out its lock timeout and the automatic
  session-end pass skips that round.

One ambiguity remains and is not solvable from the host API: an entry written by
something else that is byte-identical to refine's own. The host refuses exact
duplicates, so this requires another writer reproducing refine's scrubbed text
verbatim.

### Multi-edit transactions

Some lessons are not one edit. A new skill and the memory entry that says when to
reach for it are inseparable: applied separately, the state between them is
inconsistent. A proposal may therefore carry an `edits` array under one shared
reason, `expected_outcome`, and `summary`, capped by `max_edits_per_proposal`.

Durably, nothing new was invented. Each edit still gets its own journal record,
its own recovery metadata, and its own rollback ID, tied together only by an
additive `group` field (`id`, `index`, `size`, `summary`, and `dropped` when
edits were discarded). That is what keeps `/refine rollback <id>`, approval
reconciliation, dedup, and the ledger working exactly as before — and it is why
the daily budget counts edits rather than proposals.

Edits apply in order and the run stops at the first failure. A partial
transaction is never reported as clean:

- Applied and reserved edits are `applied` / `pending_approval` as usual.
- An edit whose host write landed but whose journal finalization failed still
  owns a recovery ID and is listed as one.
- Edits the daily budget refused, and edits not attempted after an earlier
  failure, are journaled as `rejected`, which consumes no budget.
- Edits discarded while shaping the proposal — past the cap, unusable, or
  repeating a target already claimed in the same proposal — are counted, block a
  `completed` verdict, and are reported in `group.dropped`.

So which edits of a transaction landed is readable from the journal alone, not
only from a message that automatic runs discard.

There is no `delete` action: a transaction can only create or patch.

### Auditing what refine wrote

`/refine audit` reports whether refine-created entries were used and whether the
failure fingerprint recurred after the edit. Timestamp-aware host counts are
preferred. If the host exposes only an all-time aggregate, the report labels it
`all:` and does not claim post-edit use from it. Pending approvals remain marked
as pending rather than applied. On the next audit, run, or rollback request, the
plugin checks the host pending store and actual skill or memory target: an exact
target match becomes applied, an unresolved host record stays pending, and a
removed host record without a target match becomes rejected.

```
Refine-created entries (3):

  name                           age  ver     uses  recurred  verdict
  gmail-scope-fix                12d   v2        5        no  working
      expects: Gmail sends stop returning insufficient_scope
  prisma-migrate-note             9d   v1       ~0         —  too early
      expects: —
  bash-path-hint                  3d   v3        2       yes  did not help
      expects: PATH errors stop appearing before shell commands

Candidates for removal:
  bash-path-hint — /refine rollback 8c1d2e3f4a5b
```

The audit deletes nothing. It prints a rollback command only for recorded
candidates. Skill rows keep their plain names; memory and prompt-note rows use
`memory:` / `prompt:` prefixes so same-named entries remain distinguishable.
Every row shows the model's sanitized expected outcome (`—` when omitted)
alongside its observed result. Later edits of the same entry advance a version;
version 3 or later is labelled `churning` only when the normal verdict would
otherwise be `unclear`. Skills that remain unused are fed into later proposals
as negative examples.

Two honesty rules behind the verdicts:

- **`no recurrence window`** — the pattern table had no post-edit rows at all
  (typically after a restored or rebuilt `state.db`). An empty scan cannot
  tell "the failure stopped" from "the evidence was lost", so the row names
  the gap instead of drifting into `unclear` or claiming `working`.
- **Recurrence horizon** (`refine.audit_recurrence_horizon_days`, also accepted
  as `refine.recurrence_horizon_days`, default **3**).
  On the reference journal, the median gap between recurrences of a chronic
  failure is minutes and the 95th percentile is 2.17 days — so silence shorter
  than the horizon is indistinguishable from a pause. Fingerprintless rows
  (no recurrence signal at all) earn `working` only after `age >= horizon`;
  edits younger than that stay `too early`. Raise the key only if your
  failures genuinely pause longer than that; the default is measured, not
  guessed. This horizon governs recurrence verdicts only — `unused_skills`'
  separate `min_age_days` (14) answers a different question ("has the skill
  been left idle") and is unchanged.
- **A kind with no usage counter still earns `working` — on recurrence alone.**
  The host counts uses only for skills, so `uses` is structurally unavailable for
  memory entries and prompt notes. Until recently that made `working` unreachable
  for them: the branch required `uses > 0`, so the edit kind refine produces most
  often could never be reported as successful however long it held, and the column
  read `unclear` forever. Recurrence now carries the verdict alone for those kinds,
  under the same bar the usage path uses and not a lower one — the silence must be
  *measured* (`recurred` false, never unmeasured), a fingerprint must exist (with
  neither a fingerprint nor a counter there is no evidence at all, and the row stays
  `unclear`), and the edit must be older than the recurrence horizon. The row still
  prints `uses` as `—`, so it stays visible which evidence carried the verdict. The
  gate is on **kind**, not on `usage_scope`: a skill whose usage lookup merely
  *failed* also reports `unavailable`, and that is an unmeasured dimension rather
  than an absent one, so it does not borrow this path.
- **Memory rows check presence, not usage.** The host keeps no usage counter
  for memory entries, so the only checkable fact for an applied memory edit is
  whether the exact content refine appended is still in the store. Exact
  membership cannot tell an edit from a removal — both make the string
  disappear — so when the content is gone the verdict is
  `unreliable — no longer present as applied`, never "was deleted". If the
  host memory state cannot be read at all, the row says
  `unreliable — target state unavailable` rather than guessing.

### Agent-invocable tool

The agent gets a `refine_run` tool (toolset `refine`) and may trigger the same
serialized flow with an optional `reason`. It also accepts `session_id` for one
exact historical session and `dry_run: true` to preview without applying. The
handler validates an explicit session against the read-only sessions table
before any model call and forwards all three arguments to `core.refine_run`.

The tool must run inside an active Hermes gateway turn: it reuses the
host-provided `ctx.llm`, which carries that turn's active runtime routing. An
external script that constructs `PluginLlm(plugin_id="refine")` is not
equivalent; outside a gateway turn it can fall back to a configured provider
instead of the active model.

### Which model refine uses

By default refine inherits the user's **live main model**. Hermes resolves the
model inside its own `call_llm`: with no explicit provider/model it takes the
`auto` path, whose first step is "main provider + main model", and the main
model is read from a process-local runtime override that the agent refreshes at
the top of every turn. So a model switched mid-session is intended to apply to
refine as well, without any plugin-side plumbing.

One caveat is worth knowing, and it depends on the Hermes version. On Hermes
builds older than 2026-07-17, auxiliary clients are cached under a key that does
**not** include the resolved model; a plugin call passes no live-runtime dict, so
the key is constant and the first cached client keeps supplying the model
captured when it was built, outliving a mid-session switch until the entry is
evicted or the process restarts. Upstream closed this in `73057ed16`
("scope runtime state to each turn") and `fdc6c32d7` ("isolate runtime cache by
live context"), both dated 2026-07-17 — verified by reading the Hermes repository,
not from this one, so re-check against your own checkout before relying on it.
Never a refine bug either way; on an older host, restarting the gateway clears it.

`/refine model` reports which source **refine** resolved, and with `source: live`
the value it read from the host at that moment. It cannot report which model a
cached host client will actually use, so on an older host it is not a way to
confirm a mid-session switch took effect. Restart the gateway, or pin the target.

Pinning refine's own target sidesteps all of that and makes the choice
deterministic:

```yaml
plugins:
  entries:
    refine:
      llm:
        allow_provider_override: true   # required for `provider` below
        allow_model_override: true      # required for `model` below
        provider: your-provider
        model: your-cheap-model
```

Model availability depends on provider, account, and region. A `403 RegionError`
means the provider received the request and refused that model — commonly an
account or region restriction that needs an explicit opt-in with the provider.
Because refine inherits the live main model, a restricted main model makes refine
fail for as long as the main model does; the fix is to opt in or select an
available model with `hermes model`, not to pin refine elsewhere. Check
`llm_meta.reported_provider` and `reported_model` to see which target was
actually refused.

Both `allow_*` flags are fail-closed in Hermes: with them off, a pinned value is
refused rather than applied. Leave `provider`/`model` unset to inherit the live
main model as described above. Every path — the `/refine` command, the
`refine_run` tool, and both automatic triggers — shares the one host-provided
client and honors this setting identically.

---

## Configuration

All keys live under `plugins.entries.refine`:

| Key | Type | Default | Description |
|---|---|---:|---|
| `auto_enabled` | bool | `true` | Enable automatic turn and session-end attempts. Forced off when the Hermes config cannot be read. |
| `auto_min_messages` | int | `15` | Minimum messages for session-end auto-analysis. |
| `auto_turn_interval` | int | `25` | Assistant messages added since this session's last automatic attempt; `0` disables only the turn trigger. |
| `auto_cooldown_minutes` | int | `20` | Minimum durable journal-derived gap between automatic attempts. |
| `max_edits_per_run` | int | `1` | Maximum proposal passes per run. |
| `max_edits_per_proposal` | int | `3` | Maximum inseparable edits one proposal may apply as a single transaction. `1` disables transactions. |
| `max_edits_per_day` | int | `3` | Maximum applied, pending, prepared, rollback-prepared, or pending-rollback **edits** per UTC day. This is the blast-radius limit and is re-checked before every edit. |
| `only_agent_created` | bool | `true` | Only patch agent-created skills. |
| `journal_dir` | path | `<HERMES_HOME>/refine` | Journal, lock, ledger, backups, prompt notes, and the `/refine model` override. An empty value uses this default. |
| `overview_max_entries` | int | `40` | Existing skills and memory snippets listed per kind in a proposal prompt. |
| `overview_max_chars` | int | `240` | Maximum characters in each structured overview or history line. |
| `history_max_entries` | int | `20` | Recent create/patch outcomes fed back into a proposal prompt. |
| `min_signal_required` | bool | `true` | Require a signal before the proposal call; may enable reviewer fallback. |
| `min_pattern_count` | int | `2` | Repeats before a failure counts as a mechanical signal. |
| `apply_min_sessions` | int | `2` | Distinct sessions required before a proposed edit may be applied. |
| `apply_min_occurrences` | int | `5` | Failure occurrences required before a proposed edit may be applied. |
| `reviewer_fallback_enabled` | bool | `true` | Allow one reviewer call when the mechanical gate finds nothing; its approved proposal is advisory and is never applied. |
| `reviewer_min_messages` | int | `20` | Minimum session size for reviewer fallback. |
| `reviewer_cooldown_minutes` | int | `60` | Minimum durable gap between reviewer decisions. |
| `proposer_subagent_enabled` | bool | `true` | Produce proposals via a read-only subagent that can open skill bodies before deciding. Requires a bound parent turn; without one the structured call is the fallback either way. |
| `proposer_subagent_strict` | bool | `false` | Make a subagent failure a journaled `subagent_strict_error` instead of silently downgrading to the structured call. |
| `proposer_subagent_timeout_seconds` | int | `180` | Wall-clock bound on the subagent proposal wait (minimum 5). The structured-call and reviewer timeouts are constants in `llm.py` (`_PROPOSAL_TIMEOUT_SECONDS`, `_REVIEW_TIMEOUT_SECONDS`, both 180 s) and are not configurable. All three describe the same piece of work and are deliberately the same number. |
| `prompt_notes_enabled` | bool | `true` | Permit `prompt` proposals and note injection. |
| `prompt_notes_max_count` | int | `5` | Maximum active notes injected into one turn. |
| `prompt_notes_max_chars` | int | `600` | Maximum characters in the complete injected note block. |
| `prompt_notes_default_scope` | str | `global` | Scope for newly created prompt notes: `global` or `session`; invalid values fall back to `global`. |
| `cross_session_enabled` | bool | `true` | Aggregate failures across recent sessions. |
| `skip_session_sources` | list[str] | `["cron"]` | Skip matching session sources before any trajectory messages are read; each skip is journaled without consuming edit budget. |
| `cross_session_days` | int | `7` | Interactive cross-session look-back window. |
| `cross_session_max_sessions` | int | `25` | Interactive session scan cap. |
| `cross_session_max_rows` | int | `4000` | Maximum trajectory rows scanned by an interactive cross-session pass. |
| `dedup_window_days` | int | `7` | Refuse an edit identical to a recent applied, pending, or prepared edit. |
| `audit_recurrence_horizon_days` | int | `3` | Days of post-edit silence after which `/refine audit` reads "no recurrence" as fixed rather than paused. Also accepted as `recurrence_horizon_days`; the explicit `audit_` key wins when both are set. |

LLM trust policy (`plugins.entries.refine.llm`):

```yaml
llm:
  allow_model_override: false
  allow_provider_override: false
```

---

## Known integration gaps

- **No plugin-level post-compaction hook:** Hermes exposes no normal plugin hook
  for `session:compress`; that event is gateway-only. `on_session_reset` is
  used to expire session-scoped notes, not as a claim that refinement runs after
  context compaction. The only plugin-side compaction registration,
  `register_context_engine`, replaces Hermes's built-in `ContextCompressor` and
  permits only one engine per install. Taking it over would make Refine Cycle
  responsible for the agent's whole compaction strategy and conflict with any
  real context-engine plugin. A safe integration needs an observer-only
  `VALID_HOOKS` member fired at the compaction boundary.
- **No plugin-level reasoning-effort control:** Hermes's structured plugin call
  exposes no provider reasoning/thinking setting. A model that returns only
  reasoning and no final text is reported as `llm_incomplete`; pin a
  non-reasoning model for refine with `plugins.entries.refine.llm` (`model` /
  `provider`) under the existing trust policy when that mitigation is needed.
- **A model switch can be masked by Hermes's auxiliary client cache, on older
  hosts only:** plugin calls resolve through the `auto` path, which prefers the
  live main model, but before `73057ed16` / `fdc6c32d7` (both 2026-07-17) the
  client cache key omitted the resolved model and a plugin call supplied no
  live-runtime dict, so the key never changed and a cached client kept its
  original model until eviction or restart. Refine cannot close this from the
  plugin side and does not try, and it cannot detect which host version it runs
  on, so `/refine model` cannot tell you whether you are affected. On a current
  host it is fixed; otherwise restart the gateway or pin `llm.model` /
  `llm.provider`.
- **The live main model is read through a private host API:** `live_main_target()`
  imports `_read_main_provider` / `_read_main_model` from
  `agent.auxiliary_client`. Hermes exposes no public accessor. Both names were
  confirmed present in a real installation, but a private name can move without
  notice, so the import is guarded and simply yields no live value on failure —
  `/refine model` then reports `source: host_default` rather than claiming a
  target it does not have.
- **Text-only trust boundary:** `PluginLlmTextInput` accepts text but no typed
  trust level. Refine wraps and scrubs untrusted trajectory content, which is a
  mitigation rather than hard separation; a guarantee requires a typed
  trust-level input from Hermes.
- **Approval terminal states are not exported:** the plugin can observe pending
  writes and reconcile the target, but Hermes does not expose distinct
  `accepted`, `rejected`, and `cancelled` terminal states.
- **Exact timestamped usage is unavailable:** existing SQL and host counters are
  approximate. Reliable `working` / `unused` conclusions require timestamped
  usage events from Hermes.
- **PrimeIntellect comparison was not completed during the audit:** access to
  the required network/source material was blocked, so no equivalence claim is
  made.
- **Production frequency and storage growth are unmeasured:** the audit did not
  read the real `state.db`; it therefore makes no claim about production event
  frequency or long-term storage growth.
- **No host approval for the prompt-note store:** it is a plugin-owned atomic
  file, not a host memory or skill write. Host-managed skill and memory changes
  still respect staged approvals and reconciliation.
- **A session note that stops matching its cleanup intent has no terminal
  state:** if the note store is hand-edited or a note is moved to another scope
  or session after `cleanup_prepared` was journaled, the note is retained and
  the entry stays `cleanup_prepared` — non-terminal, and not reversible, because
  the artifact rollback would remove is not the one the entry describes. Every
  later end or reset of that same session id reports it again by note id. A
  terminal state would have to keep counting against the daily budget (the edit
  did happen) and needs its own crash-ordering matrix, so it is deliberately
  left as a design decision rather than approximated. Repairing or removing the
  offending entry in `prompt_notes.json` by hand clears it.
- **Rollback is not modeled as an ordinary proposal:** rolling back a skill
  `create` means deleting it, and the no-delete guardrail rejects any proposal
  carrying a delete. Routing rollback through the proposal path would therefore
  need a privileged bypass of that guardrail. It would also replace the
  `rollback_prepared` / `pending_rollback` / `rolled_back` transitions that
  approval reconciliation and `/refine rollback <id>` idempotence depend on, and
  break rollback for every record written before the change. Rollback keeps its
  own path; what it gained is journal snapshots, so it no longer depends on a
  file surviving on disk.

- **`hermes plugins remove` fails on Windows for git-managed plugins (host
  defect, not this repo's code):** the CLI removes the directory with a bare
  `shutil.rmtree` that does not handle read-only files, and git marks
  `.git/objects/*` read-only. Removal aborts midway with `WinError 5`, leaving a
  half-deleted directory; runtime data and `config.yaml` are untouched.
  Workaround: delete the directory from PowerShell
  (`Remove-Item -Recurse -Force`) or clear the read-only attribute first. An
  upstream `onerror` handler that clears the bit and retries would fix it
  properly.

---

## Rollback

A successful mutation returns a rollback command only when its journal record is
actually reversible:

```
/refine rollback <journal_id>
```

Create rollback deletes a skill only if current content still exactly matches
the refine proposal. Patch rollback refuses to overwrite a later change before
restoring its pre-edit content. Memory rollback removes only the exact appended
entry and preserves unrelated later entries. Prompt-note rollback removes only
its exact unchanged note and preserves later notes; a changed or missing note is
a conflict and is left untouched.

### Where the restored content comes from

A skill patch records its pre-edit content twice: as a `snapshot` inside the
journal record, and as a `.bak` file under `journal_dir/backups`. Both come from
one host read, so they cannot disagree. Rollback prefers the snapshot, so losing
the backup file no longer costs the rollback.

Credential scrubbing needs two layers here, because the journal redacts
credentials from everything it writes — including a snapshot. The first layer is
the proposal path: a skill whose current `SKILL.md` is changed by scrubbing is
never patched at all, and the patch becomes a `no_op` before the model is
called. The second is a SHA-256 digest of the real pre-edit content stored beside
the snapshot. If the stored text no longer matches that digest, the snapshot is
refused and the raw `.bak` file is used instead, so redacted text is never
written over a skill.

`is_reversible` asks the restore path the same question rollback does, so an
entry is never advertised as reversible when neither source survives. In that
case rollback refuses with an explicit error and changes nothing, and a staged
rollback whose state cannot be proven stays `pending_rollback` rather than being
declared rejected.

Records written before snapshots existed carry only `backup_path` and keep
rolling back from it unchanged.

### Rolling back a transaction

Each edit of a multi-edit transaction owns its own journal record and its own
rollback ID; there is no transaction-level undo. Recovery IDs are listed
**newest first**, which is the order to follow: memory recovery is positional, so
undoing an earlier append before a later one shifts the later entry and its
rollback fails closed as a conflict.

If mutation succeeded but journal finalization failed, the returned recovery ID
points to the durable `prepared` record. Pending forward approvals consume budget
but are not advertised as reversible until the target exactly matches the
proposal. Rollback intent is journaled before its side effect; a staged rollback
returns a pending ID and is not called rolled back until the target change is
confirmed. A rejected rollback returns the entry to `applied`, so it can be
retried.

---

## Tests

```bash
cd <HERMES_HOME>/plugins/refine
python -m tests.run_tests
```

The regression suite uses only the Python standard library and a fake Hermes
host. It installs that fake host before importing the plugin.
Every database, journal, backup, skill, memory file, ledger, and lock lives
under a fresh `TemporaryDirectory`; running the suite cannot touch live Hermes
or profile state. It covers proposal completion, host action mapping,
backup/journal failures, create/patch/memory/prompt rollback conflicts, secret
sanitation, approval reconciliation, automatic triggers and cooldowns, reviewer
fallback, prompt-note injection and scope cleanup, append-only journal recovery,
and full-history aggregation.

The suite also starts two real Python processes against one temporary Hermes
root using a filesystem rendezvous and bounded timeouts. With
`max_edits_per_day: 1`, it proves exactly one mutation is applied, one budget
slot is consumed, and one ledger/skill record survives.

---

## Repository layout

```
refine/
├── plugin.yaml          # Hermes plugin manifest and registered hooks
├── __init__.py          # command, tool, and hook registration
├── config.py            # plugins.entries.refine config reader
├── core.py              # evidence, guardrails, serialized apply orchestration
├── sanitization.py      # recursive credential redaction
├── patterns.py          # normalization, fingerprints, aggregation, signal gate
├── ledger.py            # timestamp-aware usefulness ledger and audit report
├── llm.py               # structured proposal, reviewer, and patch regeneration
├── journal.py           # atomic journal, lock, prompt notes, recovery, rollback
└── tests/
    └── run_tests.py     # hermetic regression and cross-process proof
```

---

## What gets sent to the model

Refine sends sanitized aggregated error patterns, explicit correction excerpts,
a bounded structured overview of existing skills (name, description, category,
and a known local version) and memory snippets, the optional manual
reason/prior-pass note, and up to 8,000 characters of sanitized recent
trajectory to the configured provider. Each overview line is bounded by
`overview_max_chars`; each kind is capped by `overview_max_entries`, with a
visible `+N more` marker. It also sends up to `history_max_entries` of its own
most recent create/patch outcomes, including expected outcomes, so prior results
can inform the next proposal. Empty history sends no history block; the existing
negative examples for unused skills remain separate.

If the mechanical signal gate has no signal, the reviewer receives only the
bounded sanitized trajectory and returns a tiny verdict. When a skill patch is
selected, a second structured request receives the target's current complete
`SKILL.md` only if it is safe and no larger than the shared 15,000-character
input/output limit. The proposal budget derives from that limit locally because
Hermes exposes no model output-limit capability. Unsafe or oversized current
skill content becomes `no_op`; it is never redacted, truncated, or used to
generate a destructive replacement.

Credentials are redacted first, but remaining content is ordinary conversation
or skill content. Automatic analysis is on by default; set
`auto_enabled: false` if model-bound session analysis must be manually
initiated.

---

## Safety & limits

- **Credential scrubbing** covers evidence, reasons, proposals, reviewer
  verdicts, host errors, prompt notes, and recursively nested journal fields.
- **Stale-plan guard** — a skill patch proposal carries a SHA-256 baseline
  digest captured at planning time. Before backup, and again against the
  recovery snapshot captured for rollback, the plugin re-reads the live host
  state and refuses the edit with a non-budget-consuming `conflict` journal
  outcome when the literal content has already changed, disappeared, or cannot
  be read reliably. Host preprocessing is disabled for these reads so inline
  shell directives are not executed and cannot alter the baseline. Transaction
  preflight applies zero edits when any target is stale at that point.
  Proposals without a baseline (manually assembled or legacy) bypass this check
  unchanged. Hermes does not expose an atomic compare-and-write operation:
  another process can still race the final check and host write, and an approved
  staged write can race changes made while approval is pending. A transaction
  can therefore become partial if a target changes after preflight.
- **Signal gate and reviewer** reject one-off noise; reviewer failures and
  malformed output decline safely without a proposal call.
- **Incomplete model replies are visible:** a malformed, token-limited, or
  reasoning-only reply becomes a non-budget-consuming `llm_incomplete` journal
  outcome, never a false "nothing to propose" result.
- **Shared proposal limit:** the proposal token budget derives from the
  15,000-character content guardrail, while the reviewer uses its independent
  2,400-token cap (raised from 300 after measurement: a reasoning model spent
  the whole 300 thinking and returned no verdict at all).
- **Agent-created skills only** for patches; creates require a free normalized
  name and cannot use the reserved `hermes-` prefix.
- **No autonomous skill delete** — skill deletion is used only by an explicit
  rollback of an unchanged skill created by refine.
- **Bounded ephemeral prompt context** is labelled, sanitized, whole-note
  bounded, scoped, and never changes the base system prompt.
- **Serialized budget** counts applied, pending-approval, and unresolved
  prepared records after acquiring the process-safe mutation lock. Lock
  acquisition is bounded for both in-process and cross-process contention, so a
  contended run reports a timeout instead of hanging its caller.
- **Durable append journal** writes one locked, fsynced JSON line per state
  transition without rewriting history. A corrupt trailing line is skipped and
  isolated before the next valid record; backup, ledger, and note-store writes
  are atomic.
- **Conflict-aware rollback** preserves later skill, memory, and prompt-note
  changes.
- **Approval gate respected** for host-managed staged forward and rollback
  writes; plugin-owned prompt notes do not pretend to have a host approval.
- **Read-only trajectory** — `state.db` is opened with `mode=ro`.
- **No system prompt access** — the base prompt stays immutable.
- **Host support.** Uses the plugin API available since Hermes 0.17.0
  (`register_tool`, `register_command`, `register_hook`, `ctx.llm`). Verified on
  **0.19.0** (server, patched core), **0.20.1** (desktop, stock core), and
  **0.20.2** (subagent proposal path end to end). New
  proposals additionally need the host route patch (see Installation); without it
  they fail loudly with `llm_invocation_unavailable`, which is the intended honest
  gate. The manifest format cannot express a host requirement, so this is enforced
  at runtime rather than at install time.

---

## What is not yet proven in the field

Everything above describes what the code does and what its tests hold it to. This
section is about something else: how much of it has been *exercised on real
installations*, as opposed to proven by construction and by test. The two are not
the same, and the gap is stated here rather than left for a user to discover.

- **The proposer path is far less exercised than the code implies.** On the
  reference server journal (824 entries), 302 passes ended in `llm_error` and 115
  in `llm_invocation_unavailable` — **51% never reached a usable model result**.
  Most of that is provider and host-route trouble rather than plugin logic, and
  every one of those outcomes is journaled honestly rather than reported as
  "nothing to propose". But it does mean the proposal, guardrail, and apply chain
  has run end to end far fewer times than the surrounding machinery has.

- **The skill path has almost no field data, and it is the one with the largest
  blast radius.** It is the only path that writes into another agent's skill
  files. On the reference host: **1 applied skill edit** (itself synthetic), **0
  skill patches**, **0 skill rollbacks**. The full create → patch → rollback cycle
  *is* proven — six journal states in one run, external verification on disk, and
  `sha256` before the patch identical to `sha256` after the rollback — but that was
  done in an isolated, disposable `HERMES_HOME`, not against a real skill store.
  Treat the skill path as mechanically sound and field-untested.

- **Memory and prompt-note paths carry the real field evidence.** 19 applied
  prompt notes and 4 applied memory entries on the same host, with one memory
  rollback exercised end to end. These are the paths a first user will actually
  meet.

- **Crash behaviour is tested by its consequences, not by killing a process.**
  Partial journal tails (including a crash inside the plugin's own append, between
  the bytes landing and `fsync` returning), interrupted staging, stuck `prepared`
  records, abandoned rollbacks, and stale cross-process locks left by a dead owner
  all have tests. What has never been done is pulling power from a real host
  mid-write and observing recovery on the resulting state.

- **The independent review found five real defects, and they are fixed.** Every
  audit before it was written by an agent that had also written the code. One
  review was then run by a model with no part in writing it and no access to the
  authors' reasoning: it produced five hypotheses, all five held on inspection,
  and all five are fixed with regression tests proven to fail on the parent commit
  and pass after — see [`docs/INDEPENDENT-REVIEW.md`](docs/INDEPENDENT-REVIEW.md).
  Two of them (a poisoned timestamp consuming the whole query budget; a
  session-scoped rule enforced against every session) were defects no amount of
  self-review had surfaced.

None of the above is a known defect. They are the places where confidence rests on
construction and tests rather than on accumulated field use — which is exactly
where this codebase has historically been wrong before.

---

## License

MIT © 2026 Taras Boiko
