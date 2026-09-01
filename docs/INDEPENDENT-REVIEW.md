# Independent Review — Refine Cycle Plugin

Reviewed against `main` at `ba5ad98` (776 tests passing). Verified against live production server (`ubuntu@92.5.18.124`, 824 journal entries, 14,395 messages in `state.db`) and local environment.

---

## Executive Summary

A previous round of audits claimed to fix six critical defects. An independent forensic evaluation demonstrates that **claimed fix P0/Q1 (`5e06bca`, timestamp validation) did not actually hold at the database boundary**, leaving the SQL `LIMIT` truncation starving real evidence on live production data. Furthermore, four additional high-consequence architectural and boundary defects were verified across error classification, audit attribution, active tool-call interception, and pattern normalization.

---

## H1 — Claimed Fix `5e06bca` Does Not Hold: SQL `LIMIT` Starvation by Future-Dated Timestamps

**What could be wrong.**
The timestamp validation fix (`5e06bca`) validates timestamps only in Python *after* SQLite executes `ORDER BY m.timestamp DESC LIMIT ?` without an upper bound in SQL, allowing corrupted future-dated rows (values up to $1.06 \times 10^{305}$) to consume the SQL `LIMIT` budget and silently displace valid, recent trajectory evidence.

**Why this codebase produces it.**
This is the classic "check that only ever tested one direction" and "filtering after the truncation bound" (the exact shape of historical defects 1, 2, and H2). The fix introduced `patterns.believable_ts` and `_row_ts` to reject invalid timestamps in Python loops, but left `WHERE m.timestamp >= ?` and `ORDER BY m.timestamp DESC LIMIT ?` unchanged in `core.py` (`collect_cross_session_patterns` at lines 1370–1381, and `collect_evidence` at lines 1187 & 1242). Because SQLite orders descending by timestamp before applying `LIMIT`, extreme positive numbers sort first and fill the buffer. Python then correctly drops them as unbelievable, leaving zero or truncated valid rows.

**Live measurement on production server (`state.db`, 14,395 messages, 288 future-dated active tool rows):**
- In `collect_cross_session_patterns` under `LIMIT 500`:
  - Current SQL query: fetched 500 rows, of which **288 were garbage rows (57.6%) displacing 288 valid rows**, leaving only 212 valid rows in Python.
  - Bounded query (`timestamp <= now + 300`): fetched 500 rows, of which **500 were valid rows (0 displaced)**.
- In `collect_evidence(limit=60)` on live session `20260825_042514_05409217` (1,162 messages):
  - SQLite returns **53 future-dated rows (timestamps up to $1.70 \times 10^{286}$) and only 7 real messages**. 88.3% of the window context is lost to ancient corrupted rows.

**The one probe.**
Build an SQLite database containing 10 rows with `timestamp = 1e305` and 5 rows with `timestamp = time.time() - 100`. Run `core.collect_cross_session_patterns(days=1, max_rows=10)`.

**Confirms it:**
`collect_cross_session_patterns` returns 0 patterns despite 5 valid failing rows existing in the horizon.
**Clears it:**
The 5 valid rows are returned despite the presence of 10 future-dated rows.

---

## H2 — Structured `exit_code: 0` and `success: true` Silently Suppress Error Tracebacks in Output Channels

**What could be wrong.**
`_structured_error_status` (`core.py:896–899`) immediately returns `False` for any dictionary payload containing `exit_code: 0`, `success: true`, or `ok: true`, completely suppressing textual error markers and tracebacks inside `output`, `stdout`, or `stderr` and rendering the downstream `_OUTPUT_CHANNELS` check dead code.

**Why this codebase produces it.**
An internal contradiction in `core.py`: comments at lines 912–915 and 928–931 explicitly declare that `exit_code: 0` and `_OUTPUT_CHANNELS` must not mask textual error markers (e.g. `execute_code` reporting `status: success` with a traceback in its output), but lines 896–899 return `False` unconditionally before the output channel inspection at line 916 is ever reached.

**Synthetic proof:**
- `{"success": true, "output": "Traceback (most recent call last):\nZeroDivisionError: division by zero"}` $\to$ `_is_error_content()` returns `False` (treated as success).
- `{"exit_code": 0, "stdout": "Error: compilation failed\nFatal error: cannot find module"}` $\to$ `_is_error_content()` returns `False`.
- `{"ok": true, "stderr": "Traceback (most recent call last):\nRuntimeError: crashed"}` $\to$ `_is_error_content()` returns `False`.

**The one probe.**
Pass `json.dumps({"success": True, "output": "Traceback (most recent call last):\n  File 'a.py', line 1\nZeroDivisionError: division by zero"})` to `core._is_error_content(payload, tool_name="execute_code")`.

**Confirms it:**
Returns `False` (failure classified as success).
**Clears it:**
Returns `True` (failure detected in output channel).

---

## H3 — Prompt Note Effectiveness Audit Has Zero Target Presence Verification

**What could be wrong.**
`ledger.audit()` verifies whether skills and memory entries still exist on disk (flagging them as `unreliable — externally removed` or `unreliable — no longer present as applied` when deleted or altered), but contains no baseline or disk-presence check for prompt notes (`kind == "prompt"`), permanently awarding a `"working"` verdict to prompt notes that have been deleted, cleared, or expired from `prompt_notes.json`.

**Why this codebase produces it.**
Commit `ba5ad98` introduced a branch in `ledger.py` allowing kinds with no host usage counter (`prompt` and `memory`) to reach the `"working"` verdict solely on quiet-gap recurrence evidence (`recurred is False and age_days >= horizon_days`). However, disk baseline verification was implemented only for skills (`snapshot_skill_baselines`) and memory (`snapshot_memory_baselines`), completely bypassing `prompt_notes.json`. On the live server, prompt notes constitute **19 of 24 applied edits (79.2%)** and 5 of 6 on the desktop install.

**Live measurement:**
An applied prompt note whose entry is deleted from `prompt_notes.json` continues to be evaluated by `ledger.audit()` as `verdict: "working"`, `externally_modified: False`.

**The one probe.**
Create a ledger entry with `kind="prompt"`, `outcome="applied"`, `age_days=20`, and `pattern_recurred=False`. Write an empty `{"notes": []}` to `prompt_notes.json`. Run `ledger.audit(current_patterns=[...])`.

**Confirms it:**
Row reports `verdict: "working"` and `externally_modified: False`.
**Clears it:**
Row reports `verdict: "unreliable — target state unavailable"` or `"unreliable — no longer present as applied"`.

---

## H4 — Session-Scoped Prompt Notes Leak Active Tool Blocking Rules Across All Gateway Sessions

**What could be wrong.**
`__init__._update_block_rules(notes)` parses all prompt notes in `prompt_notes.json` into a global `_BLOCK_RULES` list without filtering by `scope` or `session_id`, causing session-scoped prompt notes created for Session A to actively intercept and block tool calls in Session B and all other concurrent sessions.

**Why this codebase produces it.**
In `__init__._on_pre_llm_call`, session filtering was implemented for prompt injection (`selected` list filters `scope == "session" and note["session_id"] == session_id`), but the raw, unfiltered `notes` list is passed to `_update_block_rules(notes)`. Furthermore, `_on_pre_tool_call` executes every rule in `_BLOCK_RULES` without checking `session_id`. Consequently, tool advice scoped to a single ephemeral turn or task becomes a host-wide tool execution block.

**Live synthetic proof:**
Given a note `{"id": "note1", "scope": "session", "session_id": "session-A", "content": "When calling curl, use wget instead of curl."}`, calling `_on_pre_tool_call(tool_name="terminal", args={"command": "curl http://example.com"}, session_id="session-B")` returns `{"action": "block", "message": "use wget instead of curl"}`.

**The one probe.**
Call `__init__._update_block_rules([{"id": "1", "scope": "session", "session_id": "session-A", "content": "When calling curl, use wget instead of curl."}])`, then call `__init__._on_pre_tool_call(tool_name="terminal", args={"command": "curl http://example.com"}, session_id="session-B")`.

**Confirms it:**
Returns `{"action": "block", ...}` in Session B.
**Clears it:**
Returns `None` in Session B and blocks only in Session A.

---

## H5 — Blanket URL/Integer Normalization Collapses Independent HTTP Error Domains and CLI Flags

**What could be wrong.**
`patterns.normalize_error` normalizes all URLs to `URL` and all integers to `N`, collapsing distinct HTTP error codes and API endpoints (e.g. `401 Unauthorized` on `/login`, `404 Not Found` on `/users`, and `500 Server Error` on `/status`) into the single pattern `get url returned n`; similarly, Windows CLI switches (e.g. `/help`, `/verbose`, `/force`) are matched by `_POSIX_PATH` and collapsed to `unknown option path`.

**Why this codebase produces it.**
Regex order and token greediness: `https?://\S+` consumes the entire URL (including hostname and endpoint path) before numeric status codes are normalized to `N`. Furthermore, `_POSIX_PATH` (`(?<!\w)/(?:[\w.()-]+/)*[\w.()-]+`) treats any slash-prefixed word as a POSIX path, ignoring Windows CLI switch syntax.

**Measured collisions:**
1. `GET https://api.github.com/repos/foo returned 404` $\to$ `get url returned n` (fingerprint `5d09852ce7da`)
2. `GET https://api.github.com/login returned 401` $\to$ `get url returned n` (fingerprint `5d09852ce7da`)
3. `GET https://api.github.com/status returned 500` $\to$ `get url returned n` (fingerprint `5d09852ce7da`)
4. `Unknown option /help` $\to$ `unknown option path` (fingerprint `9ae9594f8e65`)
5. `Unknown option /force` $\to$ `unknown option path` (fingerprint `9ae9594f8e65`)

**The one probe.**
Compute `patterns.fingerprint("api", "GET https://api.github.com/login returned 401")` and `patterns.fingerprint("api", "GET https://api.github.com/repos/foo returned 404")`.

**Confirms it:**
Both return the identical 12-hex fingerprint `5d09852ce7da`.
**Clears it:**
Return distinct fingerprints preserving the HTTP failure status or endpoint domain.

---

## Verification Matrix

| Hypothesis | Severity | Status | Verification Method |
|---|---|---|---|
| **H1: SQL LIMIT timestamp starvation** | **CRITICAL** | **VERIFIED (Live Server)** | Live query against `/home/ubuntu/.hermes/state.db` (288/500 rows displaced under LIMIT 500; 53/60 rows in session `20260825_042514_05409217`). |
| **H2: Structured error masking on exit 0 / success true** | **CRITICAL** | **VERIFIED (Code & Probe)** | Python unit probe on `core._is_error_content` against structured payloads containing tracebacks in `output`/`stderr`. |
| **H3: Prompt note audit baseline blind spot** | **HIGH** | **VERIFIED (Live & Probe)** | Probe against `ledger.audit()` with empty `prompt_notes.json` yielding `working`. Server data confirms 19/24 applied edits are prompt notes. |
| **H4: Cross-session block rule pollution** | **HIGH** | **VERIFIED (Code & Probe)** | Python execution of `__init__._update_block_rules` and `_on_pre_tool_call` across mismatched session IDs. |
| **H5: HTTP/CLI error normalization collapse** | **MEDIUM** | **VERIFIED (Code & Probe)** | Pure evaluation of `patterns.normalize_error` on distinct HTTP statuses and Windows CLI switches yielding identical fingerprints. |

---

## What Was Not Verified

- **Live LLM proposal generation against real providers:** Proposer calls were not triggered against live remote endpoints during this pass (all evaluations used deterministic stdlib probes and read-only database queries in compliance with the read-only mandate).
- **Physical power-loss crash during multi-process write contention:** Tested deterministically by process signal termination in the existing suite; raw OS kernel panic / filesystem journal replay was not tested.
