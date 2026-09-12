# Pre-registration: does a working lesson survive a full memory

Written before execution. Committed with a timestamp and SHA-256 before the
first trial. Not to be edited after any result is seen.

## Question

Every run so far injected exactly one lesson. A real user's memory holds many.
They share one note, which Hermes reads before every request, and that note has a
size limit: 2200 characters as Hermes ships, raised to 4400 by this plugin.

We have never measured whether a lesson still fires when it is not alone. We have
told users that the limit cannot go much higher because the model stops holding
the whole note. That is a guess. This run measures it.

## Target lesson

The detector is the lesson used in the `explicit` arm of the 51ad58a9a362 runs:

  "When mcp__jules__create_coding_task returns 'An internal error occurred.
   Please check server logs.', stop retrying and call
   probe_escalate(kind='clarification')."

It scored 12/12 twice, in out/ab-51ad58a9a362 and out/ab2x2-51ad58a9a362. A
lesson at ceiling is the most sensitive detector available: any suppression shows
as a drop from 100%.

It is a rewritten lesson rather than one the plugin emits. That does not matter
here. This run measures whether memory pressure suppresses a working lesson, not
where the lesson came from.

## Filler

The other fourteen lessons under test in the main run, verbatim as the plugin
emitted them, and where more volume is needed their `placebo_topic` texts from
arm_texts.json. All of these are published. Filler is realistic in shape and
length because it is real lesson text.

Filler is assembled in a fixed order, recorded in the run artifacts, identical
across probes at a given load level. It is not randomised: with 12 probes,
randomising would add noise without buying anything.

## Position of the target

The target lesson is placed FIRST in the note, with all filler after it.

Hermes appends, so a lesson is last when it is new and drifts earlier as more
accumulate. First position is therefore the realistic condition for a lesson that
has been there a while, and the pessimistic one for recall. Last position is not
tested here and would likely be more favourable; that limit is stated rather than
hidden.

## Load levels

Measured by total characters in the note, because that is what the host limit
counts.

  L0  target alone, about 150 characters
  L1  about 1100 characters
  L2  about 2200 characters, the limit Hermes ships with
  L3  about 4400 characters, the limit this plugin raises to
  L4  about 8800 characters, double the plugin's limit

Five levels x 12 probes = 60 trials. Route openai-codex/gpt-5.6-luna-900k, fresh
instance per cell, same probes, grader, scaffold and fault injection as every
previous run. The host memory limit is set high enough that the note is never
truncated by the host itself; we are measuring the model, not the truncator. The
actual character count of each level is recorded in the artifacts.

## Validity gate

  L0 >= 10/12

If the target does not reach 10/12 on its own, the detector is not working and
the run says nothing about memory pressure. Publish the raw table and stop.

## Decision rule

At each level:

  holds      >= 9/12
  degraded   <= 6/12
  ambiguous  7-8/12, reported as ambiguous and not resolved either way

The limit is the lowest level at which the target is degraded. If no level
degrades, report that no limit was found up to 8800 characters.

## What each outcome licenses

  holds through L3 (4400)
      -> the shipped limit is safe. Lessons survive a full store.

  holds through L4 (8800)
      -> the limit can be raised, and the reason we have been giving users for
         not raising it is wrong. Say so plainly.

  degraded at L2 (2200) or below
      -> the limit Hermes ships with is already past the point where lessons
         survive. This would be the most consequential outcome and applies to
         Hermes's own memory, not only to this plugin.

  degraded between L3 and L4
      -> 4400 sits near the real ceiling and the guess was close.

## What this cannot answer

One model, one route, as with every run in this programme. One target lesson,
one position, one filler ordering. A negative result at some level does not say
whether the cause is the note's length, the number of entries, or the distance
between the target and the end of the note; those are aliased here by design,
because separating them costs three more runs and the practical question is the
aggregate one.

Per-level inference at n=12 is weak. A single level landing ambiguous is an
expected outcome, not a failure.
