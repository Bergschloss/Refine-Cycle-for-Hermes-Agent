# Live trace of the round-11 fixes (server, 2026-09-03)

Audit of `SPEC-verified-findings-round11.md` items 1-6 (`8462f26`..`07cedea`) plus the
one fix that audit produced (`4080977`), then a trace against the server's real data.

Host: `arm-24gb-1782247459`. Trajectory `~/.hermes/state.db` (11,683 message rows in the
window read), journal `~/.hermes/refine-data/refine_journal.jsonl` (899 entries readable,
956 lines), ledger `skill_stats.json` (62 audit rows).

**Nothing on the live install was changed.** The deployed plugin is still `611b234` with a
clean `git status`; the live journal and ledger have their pre-trace size and mtime. Both
code versions ran from `/tmp` against a *copy* of the journal under a sandbox
`HERMES_HOME`, `state.db` opened `mode=ro`. The sandbox was deleted afterwards because it
held trajectory fragments.

## How it was traced

`/refine audit` was dispatched through Hermes's own plugin command registry
(`hermes_cli.plugins.get_plugin_commands`) with the host venv interpreter — the same
dispatch the CLI uses — once with `611b234` and once with `4080977`, on identical data.

Two incidental facts, recorded because they cost time:

- The host ships a built-in `/refine`, so the plugin registers as **`/refine-cycle`**
  (`Refine plugin: built-in /refine detected; registering as /refine-cycle instead`).
- `hermes -z '/refine audit'` produces **no output at all** (killed after 5 minutes).
  `-z` does not dispatch slash commands. Use the registry path above for CLI tracing.

## What the trace proves

| Item | Live result |
|---|---|
| 5 — `model_substituted` reaches the audit row | **Proven on real data.** The full audit report is byte-identical between the two versions except for four added lines: `⚠ model substituted: ...`. 12 journal entries carry the flag (all `reported_model: glm-5.3-flash`); 4 of them have a ledger row. Before: 0 warnings ever printed. |
| 2 — `created_ts` follows `entry_ts` | **Proven on real data.** `record_journal_state` is the live caller that supplies it. Mirroring the oldest real `applied` entry (`ts` = 2026-08-15) into an empty ledger: old code wrote `created_ts` = 2026-09-03, `age_days` 0; new code writes 2026-08-15, `age_days` 19. That is the difference between a verdict suppressed as "too early" and one the window supports. |
| 1 — discovery floor | Suite-only, and it bites: raising the floor to 99999 in a throwaway worktree fails with `AssertionError: 1055 not greater than or equal to 99999`. |
| 4 — stdlib-only guard | Suite-only, and it bites on a real repo file: prepending `import requests` to `notify.py` in a throwaway worktree fails with `notify.py:1 imports 'requests'`. |
| 6 — store-unavailable rejection | **Correct but unexercised here.** Of 46 `rejected` journal entries, 43 carry no `result_code` at all (written before `_journal_nonmutation` began filling it) and 3 carry `ok`. None carries `memory_store_unavailable`, so all 29 rejected audit rows still read `rejected`, identically in both versions. The branch is proven by unit test only; historical entries will always read `rejected`, and only rejections written from now on can distinguish. |
| 3 — multiline traceback aggregation | **Correct but inert on this corpus, for a measured reason.** Of 234 trajectory rows containing `Traceback`, **zero** have a real header line: 211 carry it inside a JSON tool-result blob with *escaped* `\n`, so `normalize_error`'s `splitlines()` sees one line and the whole traceback branch never engages. Fingerprints are identical between versions for all 234 rows; recurrence groups `>=2` are 43 in both, covering 91 rows in both. |

## Fix produced by the audit

`4080977` — item 3's fix closed only half of finding 06-03. Python prints every line of a
multi-line exception message at column 0, and the backward scan stopped at the *first*
such line, so only a single-continuation message aggregated. Measured before the change,
two call sites, three-line message: fingerprints differed and the frames stayed in the
normalized text. The shipped tests used an *indented* continuation, which takes a
different path, which is why the gap was invisible. Both opposing directions still hold,
and the frame boundary is now pinned by a test.

Items 1, 2, 4, 5, 6 were audited and need no change: each commit's quoted fail-first
output was reproduced independently against its own parent in a throwaway worktree.

## Open, not fixed here

1. **The traceback branch is dead on this host's tool results.** Tool output arrives as a
   JSON blob with escaped newlines, so no traceback normalization — old or new — ever
   runs on it. If refine is meant to aggregate real Python failures on this host, that,
   not the terminal-line scan, is the binding constraint. Fixing it means decoding
   embedded payloads before normalizing, which changes normalization for every tool and
   is a decision of its own.
2. **Item 6 cannot classify historical rejections.** 43 of 46 predate the field.
