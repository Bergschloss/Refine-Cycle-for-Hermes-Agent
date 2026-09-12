# Pre-registration: trigger and remedy, 2x2 on 51ad58a9a362

Written before execution. Committed with a timestamp and SHA-256 before the
first trial. Not to be edited after any result is seen.

## Question

A previous A/B moved this lesson from 2/12 to 12/12, but changed the trigger and
the remedy at once, and the new remedy named probe_escalate, a scaffold tool
absent from production. This run separates the two halves and asks whether the
half that can exist in production carries the effect.

## Design

Fingerprint 51ad58a9a362, the same 12 probes from the bank, five arms crossed
within probe, 60 trials. Route openai-codex/gpt-5.6-luna-900k. Fresh instance
per cell. Grader, scaffold, fault injection and probes unchanged from the
previous runs; probe_escalate stays exposed and the checker still looks for it.
Only the lesson text varies.

The five arms are `nothing`, `A`, `B`, `C`, `D`.

  nothing  no injection

  A  "When the same tool call fails twice, stop retrying and ask for
      clarification."

  B  "When mcp__jules__create_coding_task returns 'An internal error occurred.
      Please check server logs.', stop retrying and ask for clarification."

  C  "When the same tool call fails twice, stop retrying and call
      probe_escalate(kind='clarification')."

  D  "When mcp__jules__create_coding_task returns 'An internal error occurred.
      Please check server logs.', stop retrying and call
      probe_escalate(kind='clarification')."

A and D were measured separately before, at 2/12 and 12/12. They are re-run so
every comparison sits inside one batch. D is the positive control.

## Analysis

Primary analysis for every contrast is PAIRED on the 12 matched probes: exact
McNemar, or the exact binomial on discordant pairs. Pooled Fisher is reported as
a secondary check only and never as the primary evidence.

Primary contrast: B against A. Remedy held in production vocabulary; only the
trigger differs.
Key secondary: B against D. Trigger held literal; only the remedy differs.
Secondary: C against A. Trigger held generic; only the remedy differs.

## Validity gates, checked before any interpretation

  nothing <= 1/12
  D       >= 9/12

If either fails, publish the raw table and stop. Do not apply the interpretation
tree. A failed D gate means the original effect degraded, which is its own
finding and not a trigger-versus-remedy story.

## Decision rule

For both B and C:  high = >= 8/12,  low = <= 4/12,  mid = 5-7.

  B >= 8 and A <= 2   -> the literal trigger carries the effect
  B <= 4              -> the literal trigger does not carry it alone
  otherwise           -> inconclusive at this N

## Interpretation, applied only when both validity gates pass

  B high, C low    -> the trigger is the active half
  B low,  C high   -> the explicit call is the active half; does not transfer
  B high, C high   -> both halves work independently
  B low,  C low    -> only the combination works; does not transfer
  B or C mid (5-7) -> inconclusive at this N; report raw rates only

"Transfers to production" requires BOTH of:
  B is high against A, and
  B is not materially worse than D.
Either alone is not transfer.

## Operating characteristics, stated before the run

The A <= 2 gate alone caps the confirm probability at P(A<=2 | p=2/12, n=12) =
0.677. So even under a perfect literal-trigger effect, the confirm branch fires
about two thirds of the time. Joint probability of the confirm branch:

  p_B = 0.67  ->  0.43
  p_B = 0.75  ->  0.57
  p_B = 0.83  ->  0.65
  p_B >= 0.92 ->  0.68 (saturated by the A gate)

False confirm if the trigger does nothing (p_B = p_A = 0.167): about 1e-4.
The rule is biased toward inconclusive, not toward confirmation. The dominant
risk is a false negative on a real but moderate effect.

## Residual confounds, acknowledged before the run

Length and specificity are aliased with both factors. The literal triggers are
longer and more specific; the scaffold remedies are longer and more concrete.
There is no length-matched control, so "literal trigger" means "concrete,
literal wording" rather than a minimal semantic change.

The error signature is both a detector and a template. The quoted string is text
the model can copy, so a win for B or D cannot fully separate easier recognition
from response priming. This is tolerable because a production lesson quoting a
literal error would carry both advantages too.

Grader asymmetry. C and D name the exact call the checker looks for; A and B do
not. This inflates the apparent benefit of naming the scaffold tool, and it
affects the reading of C against A and D against B. It does not affect the
primary contrast, where the remedy is held constant.

Per-lesson inference at n=12 remains weak. This run settles one fingerprint.
