# Safety and limits

Split out of the README. What leaves the host, and what the plugin will not do.

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

## What else leaves the host

`/refine status` makes one anonymous request to `api.github.com` for the latest Refine Cycle release tag, at most once a day per running process, and only when `update_check` is on (the default). The request carries no session, config or host data. The result is only displayed; nothing is downloaded or installed, because a plugin that updates itself would bypass the review a pinned install exists for.

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
- **Host approval reconciliation** handles staged skill and memory writes when a
  managed or re-enabled gate remains active. By default, registration attempts
  to turn both host write-approval gates off; plugin-owned prompt notes never use
  a host approval queue.
- **Read-only trajectory** — `state.db` is opened with `mode=ro`.
- **No system prompt access** — the base prompt stays immutable.
- **Host support.** Uses the plugin API available since Hermes 0.17.0
  (`register_tool`, `register_command`, `register_hook`, `ctx.llm`). Verified on
  **0.19.0** (server, patched core), **0.20.1** (desktop, stock core), and
  **0.20.2** (subagent proposal path end to end). New
  proposals additionally need the host route patch (see Installation); without it
  they fail loudly with `llm_invocation_unavailable`, which is the intended honest
  gate. The manifest format cannot express a host requirement, so this is enforced
  at runtime rather than at install time. On **0.21.0** the plugin installs, loads,
  registers, and proposes: `assets/invocation-route-v0.21.0.patch` applies, and
  four proposals on a real 0.21.0 host reached the session's own model — see
  [Hermes version support](INSTALL.md#hermes-version-support).

---

