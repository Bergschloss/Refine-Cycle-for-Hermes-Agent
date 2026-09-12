# Refine Cycle — research report

**Date:** 2026-09-12
**Program:** grounded lesson effectiveness
**Repository HEAD at publication:** `40acae09b71076ce40d53706b25ea3de93278440`

Every artifact cited here is committed under [`docs/evidence/`](evidence/).

---

## 0. Disclosure

We built the plugin, the harness, the probes, and the grader. We ran and scored all three
experiments and the release verification. There is no independent replication in this
document.

Every numerical claim is traceable to a committed artifact. A hostile, technically
competent reader should be able to recompute all of them.

---

## 1. What the experiments show

### 1.1 The scoped claim

On a single pre-specified invocation route, `openai-codex` / `gpt-5.6-luna-900k`, giving
the agent a short advisory lesson in memory increased the probability that it performed
the instructed remedial action when a matched failure recurred, relative to empty memory,
to a topical but non-directive sentence, and to a vocabulary-matched scramble that
destroys the advice while keeping the words.

Main run, 133 items:

| Arm | Passed | Rate |
|---|---|---|
| lesson | 66 / 133 | 49.6% |
| scramble | 30 / 133 | 22.6% |
| nothing | 28 / 133 | 21.1% |
| topic | 28 / 133 | 21.1% |

The pre-registered decider reports, for lesson versus nothing, a paired risk difference of
**+28.57 percentage points**, 95% CI as coded [0.151, 0.420], Holm-corrected p = 0.0000.
Under the standard paired variance formula the interval is [0.209, 0.362]; see §2.3.

Forty-one percent of items are structurally uninformative (§2.2). On the 79 items where
the experiment can discriminate: lesson 45/79 (57.0%), nothing 7/79 (8.9%), RD ≈ +0.48.

### 1.2 What the effect is, and is not

The effect is **instructed compliance on matched triggers, not learning**. Endpoints are
exact structural tool-call patterns: a trial passes if and only if the agent emits the
graded call under the specified failure condition. For nine of the fifteen lessons,
passing means emitting the escalation call the lesson already names in prose, and the
lesson arm enters each trial carrying that action in memory while control arms do not.

What we claim: on this route, for these probes and this grader, advice stored in memory is
enacted when its specific trigger fires.

What we do not show: acquisition of judgment, transfer to paraphrased triggers or novel
failures, improvement in judged response quality, or any behaviour outside the fixed
scaffolds.

### 1.3 Route scope

All 529 scored trials used one invocation route. Small unregistered pilots of the same
lessons on other routes produced opposite-sign effects at N=20 per arm: `+0.20` on
`opencode-go/gpt-5.6-luna` (14/20 vs 10/20) and `−0.20` on `opencode-go/mimo-v2.5` (6/20
vs 10/20). A treatment-by-route interaction cannot be detected from a single-route run.

With roughly 8–9 probes per lesson, per-lesson power is under 1% except for extreme
effects. The only per-lesson causal statement we pre-registered and executed is the A/B on
fingerprint `51ad58a9a362` (§4).

---

## 2. Run 1 — crossed four-arm experiment, N = 133 items

### 2.1 Target system

| | |
|---|---|
| Release | `hermes-agent-v2026.8.31-clean` |
| Commit | `29112bef099274229cadff79cdff7bf7b99c4b77` (tag `v0.21.0`, 2026-08-31) |
| `run-status.json` written | 2026-09-10 21:58 UTC |
| Route | `openai-codex` / `gpt-5.6-luna-900k`, reasoning effort medium |

A directory named `hermes-agent-2026.9.11` was created about twelve hours after this run as
part of the v1.3.3 release verification; an earlier internal dossier mis-attributed the run
to it. This report corrects that.

Arms, crossed within item: `nothing` (no lesson), `lesson` (grounded lesson text from the
plugin), `placebo_scramble` (same vocabulary, scrambled into non-advisory prose),
`placebo_topic` (topical sentence, no directive). Every item-by-arm cell uses a fresh agent
instance with tools, route and budgets held fixed.

Planned 130 items × 4 arms = 520 cells. As run: 133 items including 3 reserves after
misfires, 529 scored arm-trials (lesson 131, nothing 133, scramble 132, topic 133), 130
items fully crossed.

Grader: [`lesson_effect_checker.py`](../lesson_effect_checker.py), deterministic and keyed
only on tool calls, SHA-256 `d2834b94bf95a15a9dde118387ab348a64deeeabd4e1e2683d0b3f73d33ac7bc`,
matching the pinned `.sha256`. Decider: `analysis_decider.py`, paired McNemar, Holm across
three contrasts, meaningful-effect floor RD ≥ +0.05, SHA-256
`964fc365a27593cb254097f9df404036cbbf7a605c5c8906e277fd752afe1db9`.

Gate 0 passed: all four arms with ≥80 scored trials, ≥100 unique crossed items, misfire
rate 0.56% (3/532), provider failures 0.0%. Source: [`decider_133.txt`](evidence/decider_133.txt).

### 2.2 Ceiling, floor, and the 41% that carry no information

Full table: [`per_lesson.csv`](evidence/per_lesson.csv).

Ceiling in all arms: `08d70bc36553` (12 items, 12/12 everywhere) and `935cfb594cff` (9
items, 9/9 everywhere). Both have predicates conditional on three byte-identical retries,
which agents never produce because they vary the command; the condition never fires. These
are measurement artefacts.

Floor in all arms: `2b4ff368ae22` (8), `348929cb76c6` (8), `51ad58a9a362` (9),
`fb25ce8f2797` (8).

Informative: the remaining nine fingerprints, 79 items, with lesson-arm advantages from +1
to +7 passes over nothing.

Ceilings plus floors are 54 of 133, or 40.6%, which we round to 41%. They generate only
concordant pairs and cannot create the treatment effect; they dilute it. The pooled
headline is therefore conservative.

### 2.3 Contrasts, and a sign error in the frozen decider

| Contrast | n11, n10, n01, n00 | RD | CI as coded | CI covariance-corrected |
|---|---|---|---|---|
| C1 lesson vs nothing | 28, 38, 0, 67 | +28.57 pp | [0.151, 0.420] | [0.209, 0.362] |
| C2 lesson vs topic | 28, 38, 0, 67 | +28.57 pp | [0.151, 0.420] | [0.209, 0.362] |
| C3 lesson vs scramble | 28, 38, 2, 65 | +27.07 pp | [0.1367, 0.4047] | [0.190, 0.352] |

The decider implements the paired variance as:

```python
var = ((n11+n10)*(n01+n00) + (n11+n01)*(n10+n00)
       - 2*(n01*n10 - n11*n00)) / (N**3)
```

The standard formula subtracts twice the covariance term, `- 2*(n11*n00 - n10*n01)`. The
implementation flips the sign inside the parentheses and therefore adds the covariance
instead of subtracting it. Because the data have positive concordance, this inflates the
variance and widens the intervals. The bias runs against significance; point estimates are
unaffected. Arithmetic: [`ci_formula.md`](evidence/ci_formula.md).

We did not change the decider after seeing data. The printed intervals stand as the
decision quantities under the frozen contract; the corrected intervals are published as a
labelled sensitivity analysis. All verdicts are identical under either computation.

Two directional facts. In C1 and C2 there are zero adverse discordant pairs: 38 of 38
favour the lesson, which under a sign test has probability about 7 × 10⁻¹² under the null.
And the nothing and topic arms both pass 28/133, so topic naming without a directive buys
nothing over empty memory; the real mechanism gate is advice against vocabulary-preserving
non-advice.

### 2.4 Deviations from the pre-registration

**The grader panel was replaced.** The protocol
([`lesson_effect_protocol_v2.md`](evidence/lesson_effect_protocol_v2.md) §7.2)
pre-registered two human graders plus an out-of-family LLM grader, with κ calibration and
adjudication. We used only the deterministic checker and did not log this as a pre-data
amendment. It removes grader variance, ties endpoints strictly to tool-call structure, and
forfeits any claim about judged answer quality. It is the largest deviation.

**A memory-kind lesson was included against the inclusion rule.** `348929cb76c6` is
excluded from the primary estimand by §2.1 of the protocol. It scored 0 in every arm, so it
contributes only concordant failures and cannot affect discordant counts, but the breach is
declared.

**The direction-opposite probe was dropped.** `b8787eb40263` prescribes retrying but names
no bound, and was dropped before the run as undecidable. `935cfb594cff` was tested instead
and is ceiling in every arm. No executed polarity test remains, and the lesson set is
skewed toward stop-and-escalate.

The primary claim is therefore about escalation-shaped lessons, not all grounded memories.

### 2.5 Misfires, replacement, and sensitivity

Three misfires in 532 trials (0.56%), two in the lesson arm and one in scramble, none in
the controls. Each misfiring item, not arm, was replaced whole from a reserve pool:
`probe_751789fa6c33_01`, `_06` and `probe_f0db28a1c990_06` out; `probe_08d70bc36553_10`,
`_11`, `_12` in. Details: [`harness_fix.md`](evidence/harness_fix.md).

An audit of [`pair_manifest.json`](evidence/pair_manifest.json) confirms 133 items each with
exactly four arms, zero hybrid items, identical slotless scaffold and config digests within
each item, and a single config digest across the run. Because the reserves come from a
ceiling lesson they add only concordant successes and slightly shrink the pooled RD.

Across three dataset definitions the verdict is SUPPORTED and the point estimate moves by
under two percentage points: N=133 as-run +28.57 pp, N=130 fully crossed +29.23 pp
([`decider_130.txt`](evidence/decider_130.txt)), N=127 core only +29.92 pp. The discordant
counts are **not** identical across definitions: at N=130 only C1 keeps 38/0, while C2 is
37/0 and C3 is 37/1. Primary analysis anchors on the inclusive N=133 set, which understates
the effect.

### 2.6 The comparability check aborted before it was amended

`run_crossed.py verify` aborted on the run:

```
HARNESS ABORT: the run's own digests do not satisfy the comparability assertions:
  items sharing domain tool 'mcp__jules__create_coding_task' rendered different
  scaffolds: ["42529581085e", "c83412705416"]
```

The check grouped by domain tool alone. For `mcp__jules__create_coding_task` different
lessons intentionally use different parameter schemas: `51ad58a9a362` and `e9d240f906dc`
declare `prompt` and `source`, while `c9def7291693` declares `source` only, because that
lesson is about missing required arguments. These are between-item differences. Within each
item all four arms share the same slotless scaffold digest and the same config digest.

The check was then amended to group by `(domain_tool, fingerprint)` and it passes under that
grouping. We report it in this order deliberately: the weight-bearing evidence for arm
comparability is the independent digest comparison, not the amended check. A check made to
pass after it failed, on a favourable run, is worth less than the abort plus the independent
measurement. Chronology: [`verify_story.md`](evidence/verify_story.md).

---

## 3. Run 2 — probe-reachability ceiling test, N = 24

Four lessons scored zero in every arm of Run 1. Either their probes cannot be passed at all,
or the lesson wording is dead. This run asks only the first question.

Pre-registered rule ([`DECISION_RULE_ceiling24.md`](evidence/DECISION_RULE_ceiling24.md),
committed 2026-09-11T12:35:19Z, SHA-256
`e560801f384d89391107cb13ef72533293b2056f0964d19190b2d8df319d2bad`): inject a lesson that
literally names the graded call, run 6 probes per fingerprint. A fingerprint clearing ≥80%
has a working probe and its wording becomes the suspect; below 80% the probe is broken and
the lesson is exonerated.

| Fingerprint | Scored | Passes | Rate | Verdict |
|---|---|---|---|---|
| `fb25ce8f2797` | 6 | 6 | 100% | probe works |
| `2b4ff368ae22` | 6 | 6 | 100% | probe works |
| `51ad58a9a362` | 6 | 6 | 100% | probe works |
| `348929cb76c6` | 6 | 0 | 0% | probe broken |

Three probes are mechanically sound. `348929cb76c6` fails even under explicit instruction:
the agent hallucinates activity data on error and trips the `invented_data` predicate every
time. For that fingerprint the probe is the defect and the lesson is exonerated.

One arm, no control. This is verification of reachability, not an effect estimate.

---

## 4. Run 3 — A/B wording experiment on `51ad58a9a362`, N = 36

Pre-registered rule ([`DECISION_RULE_ab.md`](evidence/DECISION_RULE_ab.md), committed
2026-09-11T12:59:05Z, SHA-256
`bab1c6dd795a016d5b849784f4e50b92b5080f36afb3eae94a6b91c452f11feb`): three arms × 12 probes.
Confirmed if the rewritten lesson reaches ≥8/12 while the original stays ≤2/12; dead if the
rewritten lesson is ≤4/12; otherwise inconclusive.

Original: *"When the same tool call fails twice, stop retrying and ask for clarification."*

Rewritten: *"When mcp__jules__create_coding_task returns 'An internal error occurred. Please
check server logs.', stop retrying and call probe_escalate(kind='clarification')."*

| Arm | Scored | Passes | Rate |
|---|---|---|---|
| nothing | 12 | 0 | 0.0% |
| lesson_original | 12 | 2 | 16.7% |
| lesson_rewritten | 12 | 12 | 100.0% |

The confirm branch fired (12 ≥ 8 and 2 ≤ 2). Fisher exact, rewritten against original:
p = 6.73 × 10⁻⁵ two-tailed.

The permitted conclusion, capped by the pre-registration: for fingerprint `51ad58a9a362`,
changing the trigger from a generic recurrence phrase to the literal error signature moved
the lesson from 2 successful escalations to 12 of 12. One fingerprint, twelve trials per
arm.

### 4.1 Does this license changing the proposer?

**No.** It supports a writing heuristic, not a code change. Four reasons.

**Scope is deliberately narrow.** The A/B was pre-registered as a single-fingerprint test
with an explicitly narrow conclusion.

**The manipulation was not single-variable.** The trigger became literal and the remedy
changed from "ask for clarification" to "call `probe_escalate(kind='clarification')`".
`probe_escalate` is a scaffold tool used only by the evaluation harness; it does not exist
in production. The experiment cannot separate "the literal trigger made the condition
detectable" from "the explicit graded call made the action executable". At most both
together work, under the scaffold.

**No production-valid remedy was tested.** We never ran a version pairing a concrete
trigger with a remedy phrased in the agent's real action vocabulary.

**The fix we had proposed is not shippable in this codebase.** An independent compatibility
audit found the earlier recommendation — tighten rule 6 of `REFINE_SYSTEM_PROMPT` and
enforce it with a hard validator in `_finalize_edit` / `_finalize_edits` — wrong on both
legs. The initial proposer is a subagent launched with `core._PROPOSER_GOAL` and never sees
`REFINE_SYSTEM_PROMPT`, so a rule-6 edit governs only the structured path. And a blanket
validator demanding quoted observables would reject the majority of existing applied entries
(19 of 21 applied prompt notes, 38 of 47 applied entries across kinds, under a permissive
scan), silently break the tool-rule parser that extracts a tool name from conditions shaped
"When calling X", collide with a condition grammar that forbids commas inside a roughly
104-character budget, and exclude failure signals that carry no text at all.

What is licensed is a measurement program, not enforcement: a pre-registered multi-fingerprint
experiment varying trigger literalness and remedy concreteness factorially, with remedies in
production vocabulary, any gate run in shadow first, and false rejections measured before
anything enforces.

That programme was then run. Sections 4.2 and 4.3 report it.

### 4.2 Run 4 — the confound removed, N = 60

Section 4.1 rejected the A/B on two grounds: the rewrite changed the trigger and the
remedy together, and the new remedy named a scaffold tool absent from production. Run 4
separated them on the same fingerprint and the same 12 probes, varying only the lesson
text across four cells plus an empty baseline. Pre-registration and full report:
[`docs/evidence/run4-2x2/`](evidence/run4-2x2/), hash recorded before the first trial.

| | remedy in production wording | remedy naming the scaffold call |
|---|---|---|
| generic trigger | 1/12 | 1/12 |
| literal trigger | **12/12** | 12/12 |

Baseline 0/12. Both validity gates passed. The literal trigger carried the whole effect:
paired, 11 discordant pairs one way and none the other, p = 0.00098. Naming the scaffold
tool contributed nothing, and the two literal-trigger arms were identical with zero
discordant pairs between them.

So the objection in 4.1 was wrong in our favour. The effect did not depend on the scaffold
name, and it appeared in wording a production lesson can actually use. Both pre-registered
conditions for transfer were met, for this one fingerprint.

### 4.3 Run 5 — the hypothesis that raised, and its rejection, N = 96

Run 4 raised a specific and testable claim: **a lesson fails when its trigger is abstract,
and rewriting it with a literal trigger and a production-vocabulary remedy makes it work.**
If true, the four lessons that scored zero in Run 1 were fixable by rewriting, and the
plugin's proposer could be changed to emit that shape.

Run 5 tested it on the two other floor lessons whose probes the ceiling test had already
cleared. They are broken in different halves, which is why they were chosen: `fb25ce8f2797`
has an abstract trigger, `2b4ff368ae22` has an abstract remedy. Four arms each, 12 probes
each, analysed separately with no pooling. Pre-registration and report:
[`docs/evidence/run5-wording-rule/`](evidence/run5-wording-rule/).

| fingerprint | as emitted | rewritten, production wording | explicit call |
|---|---|---|---|
| `fb25ce8f2797` | 2/12 | **0/12** | 12/12 |
| `2b4ff368ae22` | 0/12 | **0/12** | 12/12 |

Baseline 0/12 for both. Both validity gates passed for both. The rewrites did nothing at
all. Only the arm naming the exact graded call worked, and it worked completely.

The pre-registered branch fired: *"neither confirms -> the 51ad58a9a362 result does not
generalise, and the wording programme ends here. Publish that."*

### 4.4 What the three runs leave standing

The rewriting hypothesis is rejected. We cannot show that rewording a dead lesson revives
it, and the proposer change is off the table. Not because its implementation is dangerous,
which it also is, but because there is no evidence it would help.

One post-hoc observation, offered as a hypothesis and not as a finding. The single case
where production wording worked is the one where the lesson's own words matched the tool's
argument value: "ask for clarification" against `probe_escalate(kind='clarification')`.
The two that failed did not: "report that node is missing" against
`probe_report(status='blocked', error_signature=...)`, and "check that the repository and
path are correct" against `probe_verify(check='github_endpoint')`. A literal trigger is
necessary, since the generic-trigger arm in Run 4 scored 1/12, and Run 5 shows it is not
sufficient. What may actually carry the effect is whether the lesson names the action in
the vocabulary the tool itself uses. Testing that would need a harness whose tools exist in
production, which ours does not have.

None of this touches Run 1. The 133-probe result stands unchanged: the lessons the plugin
writes today change behaviour, and both placebos sit flat.

### 4.5 Runs 6 and 7 — does a lesson survive a full memory

Every run above injected exactly one lesson. A real store holds many, sharing one note
that the host reads before each request, and that note has a size limit: 2200 characters
as Hermes ships, raised to 4400 by this plugin. Whether a lesson still fires when it is not
alone had never been measured, and we had been telling users the limit could not rise
because the model stops holding the whole note. That was a guess.

The detector is the lesson that scored 12/12 in Runs 3 and 4, which is the most sensitive
choice available: at ceiling, any suppression shows as a drop. Filler is the other fourteen
lessons under test and their topic placebos, all published. Five load levels by measured
note length, 12 probes each. Pre-registrations and reports:
[`docs/evidence/run6-memory-load/`](evidence/run6-memory-load/) and
[`docs/evidence/run7-memory-position/`](evidence/run7-memory-position/).

| note length | Run 6, lesson first | Run 7, lesson in the middle |
|---|---|---|
| 161 | 12/12 | 11/11 |
| 1,119 | 12/12 | 11/11 |
| 2,246 (Hermes default) | 12/12 | 10/11 |
| 4,432 (this plugin's floor) | 12/12 | 11/11 |
| 8,811 | 12/12 | 10/11 |

No level degraded in either run. The host limit was set high enough that nothing was
truncated, verified per cell, so this measures the model rather than the truncator.

Run 6's pre-registration called first position "the pessimistic condition for recall". That
was wrong and the error is ours: first and last are the strong positions, and the middle is
where things get lost. Run 6 therefore measured the most favourable position while
describing it as the worst, which is why Run 7 exists and why it changes exactly one
variable.

Run 7 lost one probe at every level to a provider rate limit, excluded from the
denominators per its rule. That probe passed at every level of Run 6, so its absence works
against the result rather than for it. Its two failures, at 2,246 and 8,811 characters with
a clean level between them, do not form a gradient.

What this licenses, per the pre-registered outcome: the reason we had been giving for
keeping the limit at 4400 is wrong, and we say so here. What it does not license is
raising the default. The detector names its graded call outright, which is the strongest
form a lesson can take; a vaguer one may degrade earlier and was not measured. Nothing
above 8,811 characters was tested, and the plugin's limit is a floor, so an operator who
sets a higher number keeps it.

---

## 5. Release v1.3.3 — verification, not an experiment

Verified on two hosts running Hermes upstream `cbd03e6e` with
`assets/invocation-route-v2026.9.10.patch` applied to its eight target files plus one
regression test.

**Windows, clean install from tag `v1.3.3`.** On a repeating failure the plugin detected the
pattern, wrote a lesson, applied it, and rolled it back. The journal records
`prepared → applied → rollback_prepared → rolled_back`. The file hashed before the apply and
after the rollback was `prompt_notes.json`, because the lesson was a prompt note: identical
at 547 bytes, `60ad5418d3e7`. The rollback removed exactly what was appended.

**Server.** On a repeating failure the plugin refused to write, because three existing
entries already covered the rule, which it named. This exercises the deduplication path live.

**Both hosts.** `target_source = invocation_bound`, requested model equals reported model,
`model_substituted = false`, `primary_attempts = 1`, `grounded = true`.

Plugin suite: 1,224 cases, all passing. Hermes plugin scanner: caution, zero critical.

These checks show the code paths used in the experiments behave correctly on real hosts.
They add no causal evidence.

---

## 6. Corrections to our own earlier analysis

Two sections of our internal analysis were written before Runs 2 and 3 and are superseded.

The wording hypothesis was previously filed as post-hoc pattern reading. After the ceiling
test and the A/B it is confirmed for one fingerprint, under this harness and this remedy,
and remains too narrow to justify changing the proposer.

The proposed fix is withdrawn, for the reasons in §4.1.

Numerical corrections we made during preparation rather than leaving for a reader to find:
an earlier draft said "11 positive lessons" where the correct breakdown is 9 positive, 2
ceiling, 4 floor; an earlier draft reported a topic-arm discrepancy of 28 against 29 that
does not exist in the artifacts and came from a mis-copied cell in our own table; an earlier
draft claimed identical discordant counts across all three dataset definitions when only C1
keeps 38/0 at N=130; and an earlier draft named the rolled-back store as `MEMORY.md` when it
was `prompt_notes.json`.

---

## 7. What a hostile reader should hold us to

> On one route, for fifteen grounded lessons drawn from the Hermes failure corpus and for
> the probes and deterministic checker in this bundle, injecting those lessons into the
> agent's memory before tasks roughly doubles to triples the chance that the agent performs
> the instructed remedial action when its specific failure recurs, compared to empty memory
> or two vocabulary-matched placebos. The mechanism is instruction-following on matched
> triggers, not learning, and the claim is route-scoped.

We were conservative where the tooling accidentally was: a frozen decider whose error widens
intervals, and inclusive treatment of uninformative items. We were explicit where it was
not: the grader panel replacement, the lesson-set skew, the comparability-check abort, the
A/B's scaffold-tool confound, the non-shippability of the proposed fix, and the corrected
host, sensitivity counts and store filename.

Open questions, in the order they matter:

1. Does the effect persist, shrink, or flip sign on other routes and models?
2. Do concrete triggers still help when the remedy is phrased in the agent's real action
   vocabulary rather than a probe tool?
3. If concrete conditions are enforced, how many useful lessons are blocked and how many
   regressions introduced, measured under a frozen contract?

Those define the next experiments. They are out of scope here.
