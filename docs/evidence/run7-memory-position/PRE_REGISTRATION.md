# Pre-registration: the same load test, with the lesson buried in the middle

Written before execution. Committed with a timestamp and SHA-256 before the
first trial. Not to be edited after any result is seen.

## Why this run exists, and what it corrects

out/step3-memory-load placed the target lesson FIRST in the note and found no
degradation at any load level up to 8811 characters: 12/12 at every level.

That run's pre-registration called first position "the pessimistic condition for
recall". That was wrong, and the error is ours. First and last are the two
strongest positions in a long context; what gets lost is the middle. The previous
run therefore measured the most favourable position while describing it as the
worst, and its result cannot support a claim about a lesson buried among others.

This run repeats it with the target in the middle. It is the measurement the
previous one should have been.

## What changes, and what does not

Changed: the target lesson's position in the note. Everything else is identical
to out/step3-memory-load, deliberately, so the two runs differ in one variable.

Unchanged: the target lesson, the 12 probes, the filler texts, the filler order,
the five load levels, the host memory limit that prevents truncation, the route,
the grader, the scaffold and the fault injection.

## Target lesson

  "When mcp__jules__create_coding_task returns 'An internal error occurred.
   Please check server logs.', stop retrying and call
   probe_escalate(kind='clarification')."

The same detector, at ceiling in three prior runs.

## Position

The target is placed in the MIDDLE of the note: as close as the filler allows to
an equal number of characters before and after it. The exact character offset
before and after is recorded per level in the artifacts.

At L0 the note is the target alone, so position is undefined and L0 is identical
to the previous run by construction. It is included as the validity gate and as
the anchor for the two runs.

## Load levels

Same five levels, same filler, same order, measured in characters of the
rendered note.

  L0  target alone, about 150 characters
  L1  about 1100
  L2  about 2200, the limit Hermes ships with
  L3  about 4400, the limit this plugin raises to
  L4  about 8800, double the plugin's limit

Five levels x 12 probes = 60 trials.

## Validity gate

  L0 >= 10/12

If the detector fails alone, the run says nothing. Publish the raw table and
stop.

## Decision rule

At each level:

  holds      >= 9/12
  degraded   <= 6/12
  ambiguous  7-8/12, reported as ambiguous and not resolved either way

The limit is the lowest level at which the target is degraded. If no level
degrades, report that no limit was found up to 8800 characters in middle
position.

## What each outcome licenses

  holds at every level, as in the first-position run
      -> position does not matter at these sizes, and the two runs together
         support raising the limit. The reason we have been giving users for
         keeping it at 4400 is wrong and should be corrected publicly.

  degrades at some level where first position held
      -> position matters, the first-position run overstated what it measured,
         and the limit stays where it is. Report the level at which middle
         position broke and treat 4400 as justified by measurement rather than
         by guess.

  degrades at every level above L0
      -> a buried lesson does not survive any realistic amount of company. This
         would be the most consequential outcome for the product, because a
         user's memory fills up and every lesson eventually stops being first.

## What this cannot answer

One model, one route, one target lesson, one filler ordering, as with every run
in this programme. The target names its graded call outright, which is the
strongest form a lesson can take; a vaguer lesson may degrade earlier and this
run does not measure that.

"Middle" is one position among many. Between first and middle there is a
gradient this run does not sample.

Per-level inference at n=12 is weak. A single level landing ambiguous is an
expected outcome, not a failure.
