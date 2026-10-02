# Configuration

Split out of the README. Settings, defaults, and the known integration gaps.

## Configuration

All keys live under `plugins.entries.refine`:

| Key | Type | Default | Description |
|---|---|---:|---|
| `auto_enabled` | bool | `true` | Enable automatic turn and session-end attempts. Forced off when the Hermes config cannot be read. |
| `auto_min_messages` | int | `15` | Minimum messages for session-end auto-analysis. |
| `auto_turn_interval` | int | `25` | Assistant messages added since this session's last automatic attempt; `0` disables only the turn trigger. |
| `auto_cooldown_minutes` | int | `20` | Minimum durable journal-derived gap between automatic attempts. |
| `notify_enabled` | bool | `true` | Notify the active chat after an edit is applied; notification failure never changes the refine outcome. |
| `notify_target` | str | unset | Explicit Hermes delivery target used when no active chat is available. There is deliberately no implicit platform target. |
| `max_edits_per_run` | int | `1` | Maximum proposal passes per run. |
| `max_edits_per_proposal` | int | `3` | Maximum inseparable edits one proposal may apply as a single transaction. `1` disables transactions. |
| `max_edits_per_day` | int | `3` | Maximum applied, pending, prepared, rollback-prepared, or pending-rollback **edits** per UTC day. This is the blast-radius limit and is re-checked before every edit. |
| `update_check` | bool | `true` | Asks GitHub for the latest release tag at most once a day per process, and shows a newer version in `/refine status` and on the lesson notification. It never downloads anything; `/refine update` does that, and works with this off. |
| `max_model_runs_per_day` | int | `30` | Maximum refine passes per UTC day that reach a model, manual, automatic and dry runs together. Checked before a pass collects evidence, so a refused pass sends nothing. Automatic passes do not start once it is used up. This is the spend ceiling: every pass runs on the model the session already uses, unless `llm.use_model_for_auto_runs` sends automatic passes to `llm.model`. |
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
| `proposer_subagent_enabled` | bool | `true` | Produce proposals via a read-only subagent that can open skill bodies before deciding. Manual and automatic passes both run it under the turn that triggered them; if that agent is already gone, the structured call is the fallback either way. |
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

Configured model for automatic runs (`plugins.entries.refine.llm.use_model_for_auto_runs`, off by default):

```yaml
llm:
  provider: openrouter                  # optional; needs allow_provider_override
  model: deepseek/deepseek-chat         # the model that writes automatic lessons
  allow_model_override: true
  allow_provider_override: true         # only when provider is set
  use_model_for_auto_runs: true
```

Off, every pass runs on the session's own model, as before. On, automatic
passes (turn trigger and session end) send the evidence to `llm.model` /
`llm.provider` instead. Manual passes (`/refine`, `refine_run`, dry runs) keep
the session model. This sends your conversation evidence to the configured model; leave it
off if the session model was chosen so the evidence stays local.

- The pass still runs on the background worker; the session's turn does not
  wait on it.
- The proposer subagent is not used on these passes (it would run on the
  session model); the structured call is.
- With the setting on but `llm.model` unset, or a trust flag the pin needs
  turned off, the host would answer on the session model, so the pass calls no
  model and records `configured_model_unusable` with the reason.
- A failing call (no key, timeout, provider error) is journaled with its cause
  (`result_code`, e.g. `llm_timeout`) and shown in `/refine status` as
  `last auto run on it`. It is never retried on the session model, and nothing
  about the failures is marked handled, so the next pass sees them again.
- Hermes may move a failing explicit-provider call onto the main agent model.
  The response then names another model, and the pass is refused as
  `configured_model_substituted` without applying anything. The comparison is
  exact: if your provider reports the model under a different id, set
  `llm.model` to the id it reports.
- `/refine status` shows `auto runs model` and the last automatic result on it;
  `/refine audit` lists each lesson with the model that wrote it.

Session-model gate (`plugins.entries.refine.llm.allowed_session_models`, off by default):

```yaml
llm:
  allowed_session_models:
    - openrouter/deepseek/deepseek-chat   # provider/model
    - gpt-5-mini                          # model id alone, any provider
```

A pass bound to the session's route runs on the session's own model. When this
list is set and that model is not on it, the pass stops before it reads any
evidence: no model call, no proposer subagent, no daily model-run or edit budget
spent. The skip is journaled as `session_model_not_allowed`, appears in
`/refine status` (`allowed session models`, `session-model skips today`) and,
for an automatic pass, under `recent auto events`. Nothing is proposed, so the
failures it would have looked at stay eligible for the next pass on an allowed
model. The skip does count as an automatic attempt for `auto_cooldown_minutes`.

- An entry matches when it equals the route's model id or its `provider/model`.
  Matching is exact. Model ids may contain `/` themselves, which is why both
  spellings are accepted.
- It applies to every invocation-bound pass: automatic, `/refine`, `refine_run`
  and dry runs.
- It never changes which model a bound pass calls; it only decides whether to
  call the session model at all. To write automatic lessons with another model,
  use `llm.use_model_for_auto_runs` above; passes on it are not bound, so this
  list does not apply to them.
- Absent or `[]`: the gate is off and nothing changes. A malformed value keeps
  the gate on: unusable entries are dropped and listed in `/refine status`, and
  with no usable entry left no bound pass calls a model.

Use it with a slow local session model. The automatic pass already runs on a
background thread, but a server that generates for one request at a time can
only serve refine's call and the conversation's next turn one after the other.

---

## Known integration gaps

- **No plugin-level post-compaction hook:** Hermes exposes no normal plugin hook
  for `session:compress`; that event is gateway-only. `on_session_reset` is
  used to expire session-scoped notes, not as a claim that refinement runs after
  context compaction. The only plugin-side compaction registration,
  `register_context_engine`, replaces Hermes's built-in `ContextCompressor` and
  permits only one engine per install. Taking it over would make **Refine Cycle**
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
  trust level. Refine wraps and tag-escapes untrusted trajectory content, which is a
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

