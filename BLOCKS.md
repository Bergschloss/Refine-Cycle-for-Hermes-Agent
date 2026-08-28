# BLOCKS.md — block map of the Refine-Cycle plugin

Derived from the code at `c05997b` (main). Line ranges are indicative, not a
contract; the owning function names are. "Consumes/produces" lists the data at
the block's boundary. "Dependents" lists blocks that break if this one breaks.

| # | Block | Owns (functions) | Consumes → Produces | Dependents |
|---|-------|------------------|---------------------|------------|
| 1 | **Plugin registration** | `__init__.py`: `register()`, command/tool wiring | Hermes plugin API → `/refine`, `refine_run`, hooks | everything (entry point) |
| 2 | **Config resolution** | `config.py`: `hermes_home()`, `state_db_path()`, `get_*`, `_get_fail_closed_bool` | config.yaml → typed values; paths | all blocks reading config |
| 3 | **Scrubbing** | `sanitization.py`: `scrub_text`, credential patterns | raw strings → redacted strings | every path out of state.db |
| 4 | **Detection / session resolution** | `core.py`: `resolve_session_id`, session-db lookup | session_id or live db → resolved session + source | evidence, run orchestration |
| 5 | **Normalisation & fingerprinting** | `patterns.py`: error normalisation, `fingerprint` | error rows → stable fingerprints | aggregation, dedup |
| 6 | **Evidence collection** | `core.py`: `_collect_evidence` (ro SQL over state.db) | resolved session → scrubbed evidence pack | signal gate, proposers |
| 7 | **Cross-session aggregation** | `patterns.py`: `aggregate`, recurrence window | fingerprints + journal → chronic set | signal gate |
| 8 | **Signal gate** | `patterns.py`: `passes_signal_gate` | aggregated stats → go/no-go | proposer chain |
| 9 | **Proposer context assembly** | `core.py`: `_render_proposer_context`, `_active_prompt_notes_safe`, `llm._render_notes_block` | evidence + prompt notes → context text | structured & subagent proposers |
| 10 | **Structured proposer** | `llm.py`: `propose`, schema, `json_mode` fallback, parse/validate | context + route → validated proposal | run orchestration |
| 11 | **Subagent proposer** | `core.py`: `_propose_with_subagent`, lifecycle wait/result, strict gate | context + notes → proposal (or strict error) | run orchestration |
| 12 | **Content guardrails** | `core.py`: `_skill_or_memory_injection_error`, `_prompt_note_content_error`, `_validate_proposal` | proposal → accept/refusal reason | apply |
| 13 | **Resource & credential checks** | `core.py`: `_RESOURCE_*` regexes, `_memory_host_reference`, `_prompt_note_credential_field` | content → refusal reason | guardrails |
| 14 | **Journal state machine** | `journal.py`: append, `mutation_lock`, dedup, prompt-note store, `memory_baseline` | events → durable JSONL + notes store | apply, audit, rollback |
| 15 | **Apply** | `core.py`: `_apply_proposal` (backup → write → verify) | validated proposal → applied edit + backup path | ledger |
| 16 | **Rollback** | `journal.py`: `rollback(journal_id)` | journal id → reverted state | CLI, user request |
| 17 | **Ledger & audit verdicts** | `ledger.py`: `audit`, `_latest_applied_skill_digests`, `_latest_applied_memory_contents`, `snapshot_skill_baselines` | journal + stats + baselines → verdict rows | `/refine audit` |
| 18 | **Budget & dedup** | `core.py`: `daily_limit_reached`, journal dedup | date + fingerprints → allow/deny | run orchestration |
| 19 | **Hooks** | `__init__.py`: `pre_llm_call`, `pre_tool_call`, `on_session_end`, reset | hook API → notes reset, auto-run | notes store, autorun |
| 20 | **Block-rule parsing & matching** | `core.py` block-rule parsing + matcher | rule text → matcher | hooks, guardrails |
| 21 | **Recurrence verdict (D1)** | `ledger.py`: horizon logic around `no recurrence window` | audit rows + horizon → `recurred` | audit |
| 22 | **Install & route patch** | `install.sh`, `assets/*.patch` | host checkout → verified route or honest refusal | plugin LLM route |
| 23 | **CLI commands** | `__init__.py`: `/refine status|audit|rollback` | user args → output | user |

## Honesty notes

- Line ranges drift with every commit; the function names are the stable keys.
- Block 10/11 route attribution lives in `llm_meta` (`proposal_source`,
  `reported_provider`, `reported_model`) — written by both proposer paths.
- Block 14's `memory_baseline` cannot distinguish "edited" from "removed"
  (exact-membership limit); verdicts therefore say "no longer present as
  applied", never "deleted".
