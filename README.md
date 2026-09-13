# Refine Cycle for Hermes Agent

![Refine Cycle — a self-improvement plugin for Hermes Agent](assets/banner.gif)

**Your agent keeps repeating the same mistake. This makes it stop.**

**Refine Cycle** looks across recent sessions, finds those repeating problems, and
saves one small lesson when the evidence is strong enough. Later, it checks
whether the same problem came back.

**Cross-session by design.** Hermes can learn from the conversation in front of
it, but some problems return across different sessions: the same failed command,
the same wrong assumption, the same workaround you have to explain twice.

Underneath: errors are fingerprinted into comparable shapes, recurrence is
counted **within and across sessions**, and every mutation is journaled before it
runs.

It adapts the `/refine` concept from
[Prime Intellect's Prime Agent](https://www.primeintellect.ai/blog/prime-agent)
(Continual Harness) to the Hermes plugin system.

**Measured:** Hermes on its own handles **8.9–21.1%** of its repeating mistakes
correctly → **49.6–57.0%** with the plugin.

[**Install on your Hermes host →**](#install)

![Refine Cycle finds a repeated problem, saves one focused lesson, and checks whether it helped](assets/what-it-does.gif)

## A simple three-step loop

1. **Notice what keeps going wrong.** One bad result may be noise. A problem seen
   in two sessions or five times is a pattern worth examining.
2. **Save the smallest useful lesson.** It can add a short memory,
   create or improve a reusable skill, or add a focused note for future turns.
3. **Check the result.** It watches later sessions and reports whether the lesson
   appears to be working, unused, unreliable, or too new to judge.

## You stay in control

- It makes no more than three changes per day.
- Every change is recorded. When it can be safely undone, it gives you one
  command to reverse it.
- It never rewrites Hermes's base instructions or deletes your skills.
- API keys and other credentials are removed before conversation evidence is
  sent to the model.
- If the evidence, model reply, or Hermes state is unclear, it stops instead of
  pretending that a lesson was applied.

![A Telegram notification reading "Refine Cycle — new lesson learned (memory 3222/4400)", followed by the review line naming the skill it created](assets/notification.gif)

## What it changes on your host

Three things, and they do not all happen at the same moment.

**The installer does two**, and `--plugin-only` declines both:

- Connects the plugin to the model already serving your session, so it never calls a model you did not choose. Without this, proposals fail closed.
- Raises the long-term memory limit to a floor of 4,400 characters. A floor: a higher value you set yourself is never lowered.

**Enabling the plugin does the third**, so `--plugin-only` does not opt you out of it. Hermes can queue every memory and skill write, the agent's own as much as this plugin's, until a person approves each one. With that queue on, lessons never land: no error, no output, writes piling up where nobody looks. So the plugin turns it off on load. One `write_approval: true` line inside the `memory:` or `skills:` block becomes `false`; comments, ordering and every other value are left alone, and your config is copied beside itself as `config.yaml.refine-bak` first. A config your administrator manages is detected and never touched.

All three are reversible with `python install.py --rollback`.

**If you actually use that approval queue** — you drain it, and you want to see every write before it lands — this plugin works against how you have set Hermes up, and you should not enable it. That is a good reason to pass.

## How this differs from Hermes's built-in self-improvement

Hermes ships its own background review: after a turn or a session it looks at the
current conversation and saves what is worth keeping — a useful tactic, a user
preference, a correction. It answers **"is there something here worth
remembering?"**

**Refine Cycle** answers a different question, over a different window, and then
checks its own work:

| | Hermes background review | **Refine Cycle** |
|---|---|---|
| **Trigger** | anything worth keeping | proposal signal at 2 repeats; application only at 2 sessions **or** 5 occurrences |
| **Window** | the current session | many sessions |
| **Evidence** | the conversation as written | errors normalized to invariant shapes and fingerprinted, so `HTTP 429 for /users/8821` and `HTTP 429 for /users/9134` count as one failure |
| **Threshold** | qualitative judgement | a cheap proposal gate followed by an application bar: distinct-session count **or** occurrence count |
| **After the edit** | — | grades it: `working`, `did not help`, `unused`, `churning`, or names honestly why no verdict exists yet (`too early`, `no recurrence window`, `unreliable`) |
| **Blast radius** | host policy | 3 edits/day, dedup window, cooldown, per-edit journal, per-edit rollback |

The two are complementary, not alternatives. Hermes captures fresh experience;
**Refine Cycle** hunts chronic failures and measures whether its own fixes held.

Both can write to the same skills and memory, so the plugin is built to notice
that: a skill patch is refused outright when the target changed after planning,
and `/refine audit` reports when an entry it created was modified by something
else, because an effectiveness verdict on a file someone else edited is not a
verdict worth trusting.

---


## Why

An agent that fixes the same problem every week is not learning. The hard part is not noticing a failure; it is knowing which failures are *chronic*, and knowing whether a fix worked.

Fingerprinting is what carries that. Raw error strings never repeat exactly, so volatile parts (ids, paths, ports, timestamps) have to collapse before "again" means anything, while genuinely different errors must stay apart. Those two requirements pull against each other, and every serious defect in this plugin so far has been one of them winning too hard. An edit is then treated as a hypothesis with a falsifiable `expected_outcome`, which is what makes a verdict afterwards possible at all.

The base system prompt is never touched. Only **agent-created** skills and memory entries are editable; built-in, pinned and hub-installed skills remain off-limits. Prompt notes live only in Refine Cycle's own store, never in host memory or a skill.

## What the testing shows

A pre-registered experiment ran 133 probes across four arms, every probe in all four. With the lesson in memory the agent did the graded thing 66 times; with memory empty, 28. Both placebos sat flat: a topical sentence naming the failure domain scored 28, and a scramble of the lesson's own vocabulary scored 30. The gain comes from what the lesson says rather than from the fact that something was written. Risk difference +28.6 points, and all 38 discordant pairs ran the same way.

Read it with its bounds. Nine of the fifteen lessons pass by making the escalation call their own text names, so what is measured is instructed compliance on a matched trigger, not learning. It ran on one route, and the same lessons produced an opposite-sign effect on a different route in an earlier pilot. Two lessons passed in every arm and four failed in every arm, so 41% of the probes could not discriminate at all.

The full report carries the frozen decider's output, the per-lesson table, both pre-registered decision rules with their hashes, and a sign error we found in our own analysis script: [`docs/RESEARCH-REPORT-2026-09-12.md`](docs/RESEARCH-REPORT-2026-09-12.md), with the artifacts in [`docs/evidence/`](docs/evidence/).

## Install

Requires a Hermes host the route patch applies to. Check before installing:

```bash
python install.py --status
```

Then follow [docs/INSTALL.md](docs/INSTALL.md), which covers version support, the host patch, the memory budget, and what to do when a Hermes update removes the patch.

## Documentation

| | |
|---|---|
| [INSTALL.md](docs/INSTALL.md) | Version support, host route patch, memory budget, troubleshooting |
| [USAGE.md](docs/USAGE.md) | Commands, automatic refinement, prompt notes, auditing, model selection |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | Settings, defaults, known integration gaps |
| [ROLLBACK.md](docs/ROLLBACK.md) | Undoing an edit or a transaction |
| [SAFETY.md](docs/SAFETY.md) | What leaves the host, and what the plugin will not do |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | For maintainers: host couplings, pipeline, modules, known gaps |
| [HOST-PATCH.md](docs/HOST-PATCH.md) | What the route patch adds, and how to rebase it |
| [RESEARCH-REPORT-2026-09-12.md](docs/RESEARCH-REPORT-2026-09-12.md) | The measurement programme, pre-registrations, raw data |

## Tests

```bash
python tests/run_tests.py
```

1,233 tests, standard library only, no network.

## License

MIT © 2026 Taras Boiko
