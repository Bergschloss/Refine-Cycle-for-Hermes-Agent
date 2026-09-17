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

- It makes no more than three changes per day, and reaches a model no more than 30 times a day.
- Every change is recorded. When it can be safely undone, it gives you one
  command to reverse it.
- It never rewrites Hermes's base instructions or deletes your skills.
- API keys and other credentials are removed before conversation evidence is
  sent to the model.
- If the evidence, model reply, or Hermes state is unclear, it stops instead of
  pretending that a lesson was applied.

![A Telegram notification reading "Refine Cycle — new lesson learned (memory 3222/4400)", followed by the review line naming the skill it created](assets/notification.gif)

## How it works

![How the Refine Cycle plugin works: a session ends, repeated failures are found across sessions, the gate opens only on recurrence, one edit is proposed, safety checks run, the edit is journaled then applied, and it is checked later — with three exits where the plugin stops, rejects, or rolls back](assets/refine-cycle.gif)

After a session, the plugin reads the errors in it and in earlier sessions and turns each one into a fingerprint, so the same failure with a different id, path or timestamp counts once. Nothing is applied until a failure repeats. Then the model is asked for one small edit. The edit passes size, injection and duplicate checks, goes into the journal, and only then lands. Later sessions show whether the failure stopped. The plugin exits early at three points: nothing repeats, a check rejects the edit, or the edit is rolled back.

Stage by stage: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## How it differs from Hermes's own learning

Hermes saves what is worth keeping from the conversation in front of it. Refine Cycle looks across many sessions for the failures that keep coming back, and afterwards reports whether its fix held: working, did not help, unused, or too early to tell. They run side by side. The full comparison is in [docs/USAGE.md](docs/USAGE.md).

## What it changes on your host

- Connects the plugin to the model your session already uses, so it never calls a model you did not pick.
- Raises Hermes's memory limit to at least 4,400 characters. A higher value you set yourself stays.
- Turns off Hermes's approval queue for memory and skill writes, because with it on no lesson ever lands. Your config is backed up first.

`python install.py --rollback` undoes all three. If you approve every memory write by hand and want to keep doing that, this plugin is not for you.

## What the testing shows

In a pre-registered test on 133 probes, the agent handled the repeated mistake correctly 66 times with the lesson and 28 times without it. Two placebo notes scored 28 and 30, so the gain comes from what the lesson says. The test ran on one model and one route. Method, raw data and limits: [docs/RESEARCH-REPORT-2026-09-12.md](docs/RESEARCH-REPORT-2026-09-12.md).

## Install

Tested on Hermes 0.19 through 0.21.3.

**1. Install the plugin.** Hermes may ask you to confirm its security scan.

```bash
hermes plugins install Bergschloss/Refine-Cycle-for-Hermes-Agent
```

**2. Connect it to Hermes.** Run this from the plugin folder: `~/.hermes/plugins/refine` on Linux and macOS, `%LOCALAPPDATA%\hermes\plugins\refine` on Windows.

```bash
python install.py
```

**3. Enable it and restart.**

```bash
hermes plugins enable refine
hermes gateway restart
```

**4. Check it.** Send `/refine status` in chat. If your Hermes already has its own `/refine`, the plugin answers to `/refine-cycle status`.

When a new version is out, or a Hermes update stops the plugin, Refine Cycle tells you in chat. Tap `/refine_update` or `/refine_fix` and it updates or repairs itself and restarts Hermes. Version support, troubleshooting and everything the installer changes: [docs/INSTALL.md](docs/INSTALL.md).

## Documentation

| | |
|---|---|
| [INSTALL.md](docs/INSTALL.md) | Version support, host route patch, memory budget, troubleshooting |
| [USAGE.md](docs/USAGE.md) | Commands, automatic refinement, prompt notes, auditing, model selection |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | Settings, defaults, known integration gaps |
| [ROLLBACK.md](docs/ROLLBACK.md) | Undoing an edit or a transaction |
| [SAFETY.md](docs/SAFETY.md) | What leaves the host, and what the plugin will not do |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | For maintainers: host couplings, pipeline, modules, tests, known gaps |
| [HOST-PATCH.md](docs/HOST-PATCH.md) | What the route patch adds, and how to rebase it |
| [RESEARCH-REPORT-2026-09-12.md](docs/RESEARCH-REPORT-2026-09-12.md) | The measurement programme, pre-registrations, raw data |

It adapts the `/refine` idea from [Prime Intellect's Prime Agent](https://www.primeintellect.ai/blog/prime-agent) to the Hermes plugin system.

## License

MIT © 2026 Taras Boiko
