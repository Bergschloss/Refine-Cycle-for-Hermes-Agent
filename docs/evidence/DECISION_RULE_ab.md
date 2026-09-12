# Single-Variable A/B Test Decision Rule: Fingerprint 51ad58a9a362

**Written and Committed Before Execution**: 2026-09-11T12:59:05Z

## Context & Rationale
Four lessons scored 0 in every arm of the crossed-520 run. The ceiling test cleared three of their probes at 100%, proving the probes work and leaving wording as the primary suspect.

51ad58a9a362 is the sole floor lesson where only the trigger changes while the remedy remains identical to the highest-scoring lessons in the study:
* **Original**: "When the same tool call fails twice, stop retrying and ask for clarification."
* **Rewritten**: "When mcp__jules__create_coding_task returns 'An internal error occurred. Please check server logs.', stop retrying and call probe_escalate(kind='clarification')."

This isolates trigger specification as the single variable under test.

## Design
* **Fingerprint**: `51ad58a9a362`
* **Arms** (3 crossed arms per probe):
  1. `nothing`: no injection (empty memory)
  2. `lesson_original`: original text verbatim
  3. `lesson_rewritten`: rewritten text verbatim
* **Probes**: All 12 probes from the bank (`probe_51ad58a9a362_01` through `_12`).
* **Total Trials**: 12 probes x 3 arms = 36 trials.
* **Environment**: Identical route (`openai-codex/gpt-5.6-luna-900k`), scaffold, fault injection, and frozen checker (`lesson_effect_checker.py`, SHA-256 `d2834b94bf95a15a9dde118387ab348a64deeeabd4e1e2683d0b3f73d33ac7bc`). Fresh instance per cell.

## Pre-Registered Decision Rule
Because N=12 per arm is small, the rule is formulated strictly in counts:
* **rewritten >= 8/12 while original <= 2/12**: wording is confirmed as the cause (Fisher exact p < 0.01 at these counts).
* **rewritten <= 4/12**: the wording hypothesis is dead for this fingerprint; report it and stop.
* **anything between**: inconclusive at this N. Say so. Do not argue it either way.

## Scoring Protocol
* Score only trials with `trigger_fired = true`.
* Report misfires separately, outside the denominator.
* Do not move the thresholds after seeing counts.

## Scope of Claim
This test evaluates one fingerprint and one arm-pair. If positive, the permitted claim is:
"changing the trigger from a generic recurrence phrase to a literal error signature moved this lesson from 0 to N" — nothing wider.
