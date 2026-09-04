# Spec — stop the evidence from being poisoned, then find out why 84% of production passes fail

Baseline: HEAD `e18ca5d`. Suite at the time of writing: **1097 OK, 0 skipped** — confirm it yourself
and do not inherit the number.

Everything below was measured, not reasoned about. Each item says what was measured and with what.
Where a claim is unverified, it says so — do not upgrade it to fact on my word.

**Item 1 already has code in the working tree. It is unverified.** `core.py`, 97 insertions,
uncommitted. Your first job is to prove it right or wrong, not to assume either.

---

## Rules (AGENTS.md governs)

1. **Python 3 standard library only.** No new dependencies.
2. Suite from the repo root: `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`.
   If bare `python` resolves to a Windows Store stub, use
   `C:\Users\relig\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`.
3. **One commit per item.** Push after each. Author
   `263254659+Bergschloss@users.noreply.github.com`.
4. **Fail-first is mandatory.** Write the test, run it against the parent, **read** the failure,
   quote it in the commit or your report. A test that never failed proves nothing.
5. `git diff --check` clean and `py_compile` clean before each commit.
6. New tests go in `tests/run_tests.py` — the only file CI executes.
7. **Do not change the live server.** Items 3 and 4 are read-only there: no deploy, no gateway
   restart, no config edit, no journal edit. The daily budget of 10 on that host is deliberate for
   testing — leave it.
8. `state.db` and every corpus copy open `mode=ro`. Always.
9. CI cancels in-progress runs; confirm the run for your **final** SHA.

---

## Item 1 — Refine mines its own failures, and fetched web pages vote on the agent's memory

**File:** `core.py`. **Code is already written and sitting uncommitted in the working tree.**

### What was measured

Against `G:\Hermes\refine-b\server-A.db` (200 MB, 391 sessions, a real trajectory), using the
plugin's own `core._is_error_content` and `patterns.fingerprint` — not a hand-rolled approximation:

```
messages 14394   sessions 391   fingerprints 742
span distribution: 1sess:682  2sess:34  3sess:5  4sess:8  5sess:1  6sess:2
fingerprints in >=2 sessions: 60    max span: 15
```

The five strongest cross-session patterns:

| fingerprint | span | tool | what it actually is |
|---|---:|---|---|
| `e8fbc354229a` | 15 | `execute_code` | `BLOCKED: execute_code runs arbitrary local Python…` — a genuine durable environment fact |
| `e041d640d2fe` | 14 | `browser_navigate` | `<untrusted_tool_result source="browser_navigate">` — **a fetched web page** |
| `4f76df7cefd7` | 11 | `terminal` | `{"output": "", "exit_code": 2, "error": null}` — a genuine failure |
| `4794a3111a57` | 10 | `refine_run` | `{"success": false, "outcome": "llm_error", …}` — **refine's own output** |
| `303ae82759ec` | 9 | *(empty)* | the same refine result, with no tool name |

So two of the five strongest signals are refine reading its own broken model calls, and one is an
external web page. Three of five are noise, and they are the *loudest* signals in the corpus.

**Two distinct defects:**

**(a) Refine's own tool results are evidence.** `refine_run` returns `{"success": false, "outcome":
"llm_error", …}`, that lands in the trajectory as a tool row, `_is_error_content` says True, and the
next pass proposes a lesson about refine's own failure. A closed loop: the more the model call fails,
the more the evidence is about the model call failing. Note fingerprint `303ae82759ec` — the same
payload with an **empty** `tool_name`, so a name-only filter misses it.

**(b) Untrusted regions are stripped of their tags but keep their content.** `_strip_untrusted_tags`
removes only the boundary tags — deliberately, and its docstring explains why. But it runs *after*
`_is_error_content` has already admitted the row, and the enclosed foreign text stays in the content
that gets fingerprinted. Verified by direct probe:

```
untrusted wrapper alone  -> is_error=False
the real 14-session row  -> is_error=True
```

The wrapper itself does not trigger the classifier. The *page content inside it* does. That makes an
external website a co-author of the agent's durable memory, which is precisely what the boundary tag
exists to prevent.

**While you are here, one thing that is NOT broken:** I suspected the historical `"error": null`
defect had returned, because `4f76df7cefd7` looks like it. It has not. Direct probe:

```
exit 0 + error null  -> is_error=False
exit 1 + error null  -> is_error=True
```

`4f76df7cefd7` is `exit_code: 2` — a real failure. Do not "fix" this.

### The code already in the tree

Three helpers next to `_strip_untrusted_tags`, plus one shared admission call replacing the
`_is_error_content` check at **both** places rows leave the database:

- `_UNTRUSTED_TOOL_REGION` — matches the tag *and everything it encloses*. An unterminated opening
  tag consumes to end of text, so a truncated result cannot become trusted by losing its closing tag.
- `_strip_untrusted_regions(text)` — removes those regions to a fixed point.
- `_is_refine_own_result(content, tool_name)` — name first, then payload shape (`outcome` **and**
  `llm_called` both present), because of the empty-`tool_name` case above.
- `_evidence_text_or_none(raw_content, tool_name)` — the single admission decision, returning the
  trusted text to fingerprint or `None` to skip. Used by `collect_evidence` **and**
  `collect_cross_session_patterns`, so the two cannot drift.

In the cross-session collector the admission still runs **before** the session cap. Do not move it;
the comment there explains what breaks (13 of 25 session slots went to sessions with no failure).

### Your job

1. **Verify or refute it.** Fail-first tests, both defects, both directions:
   - a `refine_run` row is not evidence; the same payload with an empty `tool_name` is also not
     evidence; a different tool returning JSON with `outcome` but no `llm_called` **is** still
     evidence.
   - a row whose only error shape is inside an untrusted region is not evidence; a row with a genuine
     failure of its own *outside* the region still is, and its pattern text no longer contains the
     foreign text.
   - an unterminated `<untrusted_tool_result …>` with no closing tag is treated as untrusted to the
     end of the row.
2. **The risk I did not test — test it.** `_strip_untrusted_regions` runs on raw content *before*
   classification, and `_is_error_content` deliberately classifies raw JSON so its structured-status
   path can parse the payload. If a JSON tool result carries an untrusted wrapper *inside a string
   value*, removing that span can make the JSON unparseable and cost the row its structured verdict —
   the mechanism that once counted 479 successes as failures. Construct that row, measure what
   happens, and if it regresses, fix it by only stripping regions when the payload does not parse as
   JSON, or by stripping inside the parsed value. **Report the measurement either way.**
3. **Measure the effect on real data, not just tests.** Re-run the survey over
   `G:\Hermes\refine-b\server-A.db` before and after, read-only, and report the new span table. The
   expected outcome is that `e041d640d2fe`, `4794a3111a57` and `303ae82759ec` are gone from the
   cross-session list and `e8fbc354229a` and `4f76df7cefd7` remain. If a genuine pattern disappeared
   too, that is a defect in the fix — say so instead of shipping it.
4. Note in the commit that mixed rows change fingerprint once, re-partitioning that row's pattern
   history. That is accepted; it must not be silent.

If you decide the tree code is wrong, throw it away and write your own. It is not sacred.

---

## Item 2 — The model timeout and the real latency do not agree

### What was measured

Live journal, `/home/ubuntu/.hermes/refine-data/refine_journal.jsonl`, 985 entries. All-time failing
passes: **453**. Grouped by cause:

```
267  route_unavailable
113  timeout
 39  other
 21  "401"-shaped
 10  "402"-shaped   <- regex noise, see below
  3  "403"-shaped
```

And the latency of passes that **succeeded**, from `llm_meta`:

```
latency_ms 84194 / 89914 / 119063     output_tokens 10647 / 10940 / 14791
```

Successful proposals take **84 to 119 seconds**. 113 passes died on timeout. Those two facts belong
to the same story.

**A correction to save you time:** I first read the `402`s as a billing failure and said so. It was
my regex matching `402931` inside a timestamp. There is no 402 problem. The `401`/`403` counts come
from the same loose matching and are probably also noise — if you need them, count them properly.

### Your job

This is the known-fragile *"two limits that must agree"* area from AGENTS.md, which has bitten this
project once already (2048 tokens vs 15000 characters).

1. Find every timeout and size limit on the proposal path — the request timeout, the output token
   budget, the content guardrail — and write down the actual numbers. `llm.py` and `config.py`.
2. Compare against the measured 84–119 s / 10.6–14.8k tokens above. State plainly which limit the
   real successful calls are already brushing against.
3. If they disagree, derive them from one constant, as AGENTS.md requires. If they already agree and
   the timeouts have another cause, **say that and stop** — do not invent a fix to close the item.
4. Tests for whichever direction you change.

Do not tune the timeout by feel. A number without the measurement beside it is not a fix.

---

## Item 3 — Two thirds of production failures are `route_unavailable`, and nobody knows why

**Read-only diagnosis. No server changes.**

### What is known

- 267 of 453 all-time failures are the route being unavailable — the single largest cause, bigger
  than everything else combined.
- One post-deploy failure row carries `subagent_fallback_reason: "launch_failed"`, and the status
  reports `proposer: {"effective": "structured", "subagent_config_enabled": true,
  "subagent_lifecycle_bound": false}`.
- The live host **does** have the route patch: `route_present: True`, and
  `tests/agent/test_plugin_invocation_route.py` exists in
  `/home/ubuntu/releases/hermes-agent-v2026.8.31-clean/`. So this is not the missing-patch case.
- `~/hermes-agent` at `0957277f2f` is a **dev checkout, not the running host** — it has no route
  patch, and checking it will mislead you. The live runtime is the `releases/…-clean` tree.

### Your job

1. Read the `route_unavailable` rows and classify them. How many are `launch_failed` on the subagent
   proposer, how many are the bound route genuinely absent, how many are something else? Group them
   the way `_handle_no_signal` and the proposer fallback actually distinguish them, not by string
   guessing.
2. Answer one question with evidence: **would disabling the subagent proposer remove these
   failures?** `subagent_lifecycle_bound: false` while `subagent_config_enabled: true` looks like a
   proposer that is configured but cannot bind, falling back on every pass and failing there.
3. If the answer is yes, the deliverable is a **recommendation with the numbers behind it**, plus
   whatever code change makes the fallback path survive an unbindable subagent. Do not change the
   server's config yourself.
4. If a distinct failure is currently indistinguishable in the journal from another, that is its own
   defect under AGENTS.md's silent-`no_op` rule — fix the journaling so they are told apart.

---

## Item 4 — The usefulness run measured the weakest signal available

### What was measured

The harness `hermes-corpus-run-task.md` hardcodes seven session ids and probes evidence with
`core.collect_evidence(session_id=sid)` — per session. Every `evidence.json` in the last run says:

```
top_sessions_seen: 1     (all 7 passes)
```

So every graded proposal came from *within-session* recurrence, which is the weaker half of the
signal gate by design. `sessions_seen` is the stronger half and was never exercised.

**And the tagged corpus cannot fix that.** Same measurement method as Item 1, on
`G:\Claude\cc-corpus.db`:

```
messages 38286   sessions 125   fingerprints 1818
span distribution: 1sess:1809  2sess:9
fingerprints in >=2 sessions: 9    max span: 2
```

Nine fingerprints reach two sessions, none reach three, and several of the nine are classifier false
positives of the kind Item 1 removes. **There is no cross-session signal in that corpus to measure.**
An earlier claim of mine — "80 fingerprints, one spanning 12 sessions" — was wrong; it came from a
crude probe that skipped `_is_error_content` and `active = 1`. Do not build on it.

`server-A.db` does have the signal (60 fingerprints ≥2 sessions, spans to 15).

### Your job

Run the harness per `hermes-corpus-run-task.md`, unchanged in its rules, with four changes:

1. **Corpus:** `server-A.db`, not `cc-corpus.db`. It is the only one with cross-session recurrence.
2. **Sessions:** these, already selected for a fingerprint that spans sessions:

   ```
   signal-01:20260728_104610_8bee8476:signal    fp e8fbc354229a  span 15   4 hits here
   signal-02:20260720_104017_5e572e24:signal    fp e041d640d2fe  span 14   2 hits here
   signal-03:20260702_061147_b3ab2626:signal    fp 4f76df7cefd7  span 11   3 hits here
   signal-04:20260823_234538_165cfa6b:signal    fp 2bd5f772cd40  span  8   4 hits here
   ```

   **Re-select after Item 1 lands.** `signal-02` is the `browser_navigate` page that Item 1 removes
   from evidence, so it should stop being a signal session — that is the fix working, not a bug.
   Replace it with the next genuine cross-session fingerprint. Controls: keep three sessions whose
   fingerprints repeat nowhere.
3. **Seed the throwaway journal** with a copy of the live journal's entries. In the last run the
   throwaway home started empty, so the duplicate guard was blind by construction and could not have
   fired no matter what was proposed. Without seeding, that guard stays unmeasured.
4. **Record `sessions_seen` from the real pass**, not only from the harness's own per-session probe.
   Right now the output cannot tell you whether cross-session evidence reached the model at all. Also
   read `result.json`/the journal *before* stdout when building the run table — `control-01/02` showed
   `-` last time although the journal proved both reached a decision.

Grade exactly as `hermes-corpus-run-task.md` says. Do not soften verdicts.

**One guard question is already answered, so do not report it as unknown.** Production closed it: on
2026-09-04 the shipped version rejected two proposals within four minutes —
`Prompt note action must match an approved behavioral policy`, and
`Prompt note is too large for its per-note rendered context budget (158 chars; max 120)` — then
applied a third as a memory entry. The size guard fires, with the number in the journal. What remains
unmeasured is the duplicate guard, which is what change 3 exists for.

---

## Item 5 — `0.14.0` names two different builds

Tag `v0.14.0` points at `a4edd83`. Deployed and current HEAD is `e18ca5d` — three commits later,
touching `core.py`, `patterns.py` and `install.sh`. Both call themselves `0.14.0`, so a field report
quoting the version does not identify the code.

Bump to `0.14.1` and tag the released SHA. Last item, after 1–3. Do not move an existing tag.

---

## What I verified, and what I did not

**Verified by execution:**

- Suite 1097 OK, 0 skipped, on `e18ca5d`. CI green for that SHA.
- Blocker 3 of the previous spec (numeric normalization): 0 defects over 9 cases, both directions —
  exit codes, ports, signals and HTTP statuses stay apart; ids, timeouts and incidental numbers still
  collapse.
- Blocker 2: `cross_session_unavailable` is journaled as `db_unavailable` / `query_error`, a genuinely
  quiet window records nothing, and `session_unknown` / `daily_limit_reached` are journaled without
  consuming budget. Both directions have tests.
- Blocker 1: the installer tests for undoing a created file, restoring the index, and keeping the
  recovery copy on a failed restore all pass, on Windows.
- The server runs exactly `e18ca5d`: 13 of 13 files match by git-normalized blob id. An earlier
  "0 of 12 match" reading of mine was CRLF in my own checkout, nothing more.
- Every corpus number in Items 1 and 4, via `core._is_error_content` and `patterns.fingerprint`.
- Every journal number in Items 2 and 3.

**Not verified:**

- **The Item 1 code in the working tree has never been run.** No test, no suite pass, no compile
  check. Treat it as a draft.
- Whether post-deploy production behaves better than pre-deploy. Post-deploy is 5 passes — 1 failure,
  2 rejected, 1 applied. 20% versus 59% is **not** a result at that sample size, and I am not
  claiming it.
- Whether cross-session evidence ever reached the proposal model in the last run. The harness only
  records its own per-session probe, so the output cannot answer it. Item 4 change 4 exists to make
  it answerable.
