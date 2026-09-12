# Probe-Reachability Ceiling Test: Decision Rule

**Written and Committed Before Execution**: 2026-09-11T12:35:19Z

## Purpose
Four lessons scored 0 in every arm of the crossed-520 run:
* 2b4ff368ae22: 0/8
* 348929cb76c6: 0/8
* 51ad58a9a362: 0/9
* fb25ce8f2797: 0/8

Two explanations:
(a) The lesson is dead — too vague for the agent to act on.
(b) The probe is broken — its graded action cannot be produced at all, no matter what the agent is told.

This test injects, as memory text, a lesson that literally names the graded call across 6 probes per floor fingerprint (24 trials total).

## Pre-Registered Decision Rule

* **A fingerprint clears at >= 80% pass**: its probe works; the original lesson's wording is the remaining suspect.
* **A fingerprint lands below 80%**: its probe cannot elicit its own graded action even when handed the answer; that lesson is exonerated and the probe is what needs fixing.

## Scoring Protocol
* Score only trials with `trigger_fired = true`, as the protocol requires.
* Report misfires separately and do not count them in the denominator.
* Do not adjust the threshold after seeing results. If a fingerprint lands near 80%, report the number and say it is near the line — do not round it to the side that suits us.
* This run has one arm and no control; it cannot measure an effect. It answers only: can these four probes be passed at all.
