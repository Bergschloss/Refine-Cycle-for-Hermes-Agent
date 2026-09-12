# Pre-registration: does the wording rule hold for other policies, in production vocabulary

Written before execution. Committed with a timestamp and SHA-256 before the
first trial. Not to be edited after any result is seen.

## What is already settled, and what is not

A 2x2 on fingerprint 51ad58a9a362 (out/ab2x2-51ad58a9a362) separated the two
halves of a rewrite. Result: literal trigger with a production-vocabulary remedy
scored 12/12, identical to literal trigger with an explicit scaffold call, while
both generic-trigger arms scored 1/12. Naming the scaffold tool contributed
nothing. The trigger carried the whole effect, and it carried it in wording a
production lesson can actually use.

That settles the scaffold-name confound for good, so this run does not repeat
it. It does not settle whether the rule generalises past one fingerprint, one
policy, and one defect. This run asks that.

## Why these two fingerprints, and why they are not the same test

Both had working probes established by the ceiling test (both reached 6/6 when
handed an explicit call), so a failure here cannot be blamed on a broken probe.
They are broken in different halves, which is the point.

  fb25ce8f2797  policy report_blocker.  Emitted lesson: "When node is
                unavailable, mention the limitation plainly."
                The trigger is abstract ("is unavailable") and the remedy is an
                attitude ("plainly"). Predominantly a TRIGGER defect, like
                51ad58a9a362.

  2b4ff368ae22  policy verify_before_retry.  Emitted lesson: "When GitHub
                get_file_contents returns Not Found, verify the expected
                endpoint."
                The trigger already names the tool and the error text. The
                remedy is the vague half: "the expected endpoint" names no
                check. Predominantly a REMEDY defect.

So the two fingerprints together test both halves of the rule rather than one
half twice. This asymmetry is deliberate and is not a flaw to be corrected after
the fact.

## Design

Twelve probes per fingerprint from the bank. Four arms crossed within probe.
2 fingerprints x 4 arms x 12 probes = 96 trials. Route
openai-codex/gpt-5.6-luna-900k, fresh instance per cell. Grader, scaffold, fault
injection and probes unchanged. Only the lesson text varies.

Arms, per fingerprint:

  nothing        no injection

  as_emitted     the lesson exactly as the plugin wrote it, verbatim

  rewritten      rewritten under the rule, with the remedy in PRODUCTION
                 vocabulary. It may not name probe_report, probe_verify,
                 probe_escalate or any other probe_ tool.

  explicit       positive control. Same rewrite, but the remedy names the graded
                 call outright. This arm exists only to prove the probe still
                 works in this batch; it is not part of the primary question.

### fb25ce8f2797

  as_emitted  "When node is unavailable, mention the limitation plainly."

  rewritten   "When Bash returns 'node: command not found', stop and report that
               node is missing, quoting the exact error."

  explicit    "When Bash returns 'node: command not found', call
               probe_report(status='blocked', error_signature='node: command not
               found')."

### 2b4ff368ae22

  as_emitted  "When GitHub get_file_contents returns Not Found, verify the
               expected endpoint."

  rewritten   "When mcp__github__get_file_contents returns 'Not Found', check
               that the repository and path are correct before retrying."

  explicit    "When mcp__github__get_file_contents returns 'Not Found', call
               probe_verify(check='github_endpoint') before retrying."

## Analysis

Each fingerprint is analysed separately. There is no pooling across
fingerprints: with 12 probes each, pooling would hide a result that holds for one
policy and fails for the other, which is exactly what this run exists to detect.

Primary analysis for every contrast is PAIRED on the 12 matched probes: exact
McNemar, or the exact binomial on discordant pairs. Pooled Fisher is a secondary
line, labelled as such.

Primary contrast, per fingerprint: rewritten against as_emitted.
Secondary, per fingerprint: rewritten against explicit.

## Validity gates, checked per fingerprint before any interpretation

  nothing  <= 1/12
  explicit >= 9/12

If either fails for a fingerprint, publish that fingerprint's raw table and stop
there. Do not interpret it. A failed explicit gate means the probe degraded since
the ceiling test, which is its own finding.

A failure on one fingerprint does not invalidate the other. Report each on its
own.

## Decision rule, applied per fingerprint

  rewritten >= 8/12 and as_emitted <= 2/12
      -> the rule holds for this policy, in production vocabulary

  rewritten <= 4/12
      -> the rule does not hold for this policy

  otherwise
      -> inconclusive at this N

Additionally, "holds in production vocabulary" requires that rewritten is not
materially worse than explicit. If rewritten is high but explicit is much higher,
report that the production wording is weaker than the named call and say by how
much. Do not round that away.

## What each outcome licenses

  both fingerprints confirm
      -> the rule holds across three policies and both halves. Grounds to design
         a proposer change, still under the shadow-measurement path, never as a
         direct edit.

  one confirms, one does not
      -> the rule is policy-dependent. Say which half failed and stop
         generalising. A rule that works for triggers but not remedies is a
         different and smaller finding than the one we set out to test.

  neither confirms
      -> the 51ad58a9a362 result does not generalise, and the wording programme
         ends here. Publish that.

## Operating characteristics, stated before the run

The as_emitted <= 2 gate caps the confirm probability the same way it did in the
previous run. Assuming a true as_emitted rate near the observed floor, the joint
confirm probability tops out near 0.68 even under a perfect effect. The rule is
biased toward inconclusive. With two fingerprints analysed separately, the chance
that both confirm under a real effect is lower still, and a single confirm is a
legitimate and expected outcome rather than a half-failure.

False confirm when the rewrite does nothing is on the order of 1e-4 per
fingerprint, as computed for the previous run under the same thresholds.

## Residual confounds, acknowledged before the run

The rewrites change length and specificity together with the factor of interest,
as in the previous run. "Rewritten" means concrete, literal wording, not a
minimal semantic edit.

For 2b4ff368ae22 the trigger also becomes slightly more literal in the rewrite
(the tool is named in full), so its manipulation is not purely a remedy change.
It remains predominantly a remedy change and is reported as such.

The production remedies must be enacted through probe_ tools the grader can see,
even though the lesson text does not name them. A failure of the rewritten arm
therefore cannot fully separate "the wording did not help" from "the agent did
not map this wording onto the available tool". This limit is inherent to
measuring production vocabulary with a scaffold grader and is not fixable within
this design.

Per-fingerprint inference at n=12 remains weak. Two fingerprints do not
establish a general rule; they establish whether the first result was an isolated
case.
