# Installing on a Hermes host

Split out of the README. Version support, the host route patch, and what the installer changes.

## Hermes version support

Read this before installing. A green test suite and a passing `hermes plugins
doctor` do **not** prove that proposals are available. Proposal support also
requires `install.py --status` to recognize a compatible invocation-route patch
and an invocation-bound smoke test to reach the proposer.

| Hermes | Installs | Loads, registers | status / audit / rollback | New proposals |
|---|---|---|---|---|
| 0.19.0 | yes | yes | yes | yes, with the route patch |
| 0.20.1 | yes | yes | yes | yes, with the route patch |
| 0.20.2 | yes | yes | yes | yes, with the route patch (subagent path verified end to end) |
| 0.21.0 | yes, after confirming a `caution` scan | yes | yes | yes, with `invocation-route-v0.21.0.patch` |
| 0.21.1 (release tag `2237be3559`) | yes, after confirming a `caution` scan | yes | yes | yes, with the same `invocation-route-v0.21.0.patch` |
| 0.21.1 main after the release (`a0749d583a` and later) | yes, after confirming a `caution` scan | yes | yes | yes, with `invocation-route-v2026.9.10.patch` |
| 0.21.3 (release tag `v2026.9.14`), and main from `1c671beab2` | yes | yes | yes | yes, with `invocation-route-v2026.9.14.patch` |

Both 0.21.1 rows carry the same version string, which is why the installer picks
by applicability instead: upstream inserted lines inside two of the 0.21.0
patch's context windows shortly after cutting the release, so the release tag and
the main branch above it need different patches. `--status` names the one it
chose.


### A Hermes update removes the route patch

Updating Hermes rewrites its checkout, and that puts every patched file back to
stock and takes `.refine-install` with it. Nothing warns you, and `hermes plugins
doctor` still passes, because the plugin itself is untouched — only the host
capability it depends on is gone. New proposals then fail closed with
`llm_invocation_unavailable` until the patch is reapplied.

This is not specific to any one release. Expect it after every Hermes update.
From chat, send `/refine update` and then `/restart`: it installs a newer plugin
release if there is one, then asks that release's installer for the host state
and reapplies the patch when it is missing. From a terminal:

```bash
python install.py --status      # says `stock` again if the patch was removed
python install.py --patch-only  # reapplies it
```

`python install.py --status --json` prints the same report as one line of JSON.

`--status` can also report `outdated`: every marker is present, but the files carry an
earlier revision of a patch that has since been fixed. `--patch-only` and `/refine
update` replace it: they record a backup, return the patch's own files to the
checkout's versions, and apply the current revision. Rollback still restores the
tree you had before the first install.

`--status` is also what tells you the bundled patch no longer fits a new host: it
reports `incompatible` and names the patch bases it tried, rather than forcing a
patch onto a topology it was not built for.

Measured on the 0.21.0 → 0.21.1 update: the checkout came back clean, the marker
directory was gone, and the bundled 0.21.0 patch then applied unchanged to the
new base — same eight files, its 37 host tests passing, and a proposal run
afterwards reaching the session's own model in one request with no substitution.

Measured again once upstream moved past that release tag (base `a0749d583a`,
2026-09-10, ~1000 commits after the base the 0.21.0 patch was cut against): that
patch now fails on `agent/turn_context.py` and `agent/turn_facade.py`, whose
context windows gained upstream lines, and `--status` reports `incompatible`
rather than half-applying — [issue #13](https://github.com/Bergschloss/Refine-Cycle-for-Hermes-Agent/issues/13).
`invocation-route-v2026.9.10.patch` is the same patch re-anchored: it applies to
`a0749d583a` and to main above it, reverse-applies for rollback, and its host test
file passes 39 tests on both bases. Two of those 39 are new and fail on an
otherwise identical tree without the single-request `create` — one shows a locked
call re-sent as a stream, the other a credit-limited 402 answered with a second
request carrying a clamped `max_tokens`.

### What is verified on 0.21.0

Measured on a clean Windows checkout of Hermes 0.21.0 at
`693641aa8b4359c602283bdbbc14041e03bc47bc`, using disposable clones and
`HERMES_HOME` directories rather than the real profile:

- the real Hermes scanner reports `caution` with 153 findings and zero critical
  findings on the tracked plugin tree; `--force` may confirm this verdict;
- `install.py --status` reports
  `stock — clean base 693641aa8b; invocation-route-v0.21.0.patch applies`;
- the installer transitions the disposable host from `stock` to `patched`, and
  status then finds all eight route markers;
- the patch's host test file passes all 37 tests;
- an invocation-bound synthetic proposer smoke reaches the installed proposer
  exactly once through the captured active client;
- OpenAI-shaped chat, `anthropic_messages`, and `codex_responses` transports are
  route-locked without rebuilding the active client; async calls use that same
  captured client and still issue one physical request;
- rollback removes the patch-created host test and restores an empty tracked host
  diff;
- the plugin suite passes 1,203 tests, with 11 Windows-only skips for Bash-based
  `install.sh` coverage.

Since then the same host has been exercised with a live model rather than a
smoke. On a Linux checkout of the same commit, in a disposable `HERMES_HOME`
with its own journal, four `/refine-cycle` runs over real recorded sessions each
reached the model and each recorded the same route facts:

```
target_source      : invocation_bound     ← the route came from the host, not config
requested / reported: openai-codex/gpt-5.6-luna-900k  (identical)
model_substituted  : false
primary_attempts   : 1                    ← one physical request, no retry, no fallback
```

That is the whole contract the patch exists to provide, and it holds on 0.21.0.

**What those runs did not show is an applied edit.** All four ended `no_op`:
three because the reviewer judged the trajectory (benchmark output, exploratory
searches) to carry no durable lesson, one because the signal gate never opened.
That is the plugin declining on merit, not failing — but it means the apply path
itself is still evidenced by the test suite and by 42 applied entries on 0.20.x,
not by a fresh 0.21.0 run. The apply path is downstream of the route and no
patched file takes part in it.

The smoke uses synthetic input and does not start or restart the real gateway. The exact commands and the distinction
between the original failed baseline and the corrected result are recorded in
[`docs/FRESH-INSTALL-HERMES-0.21.0-2026-09-07.md`](FRESH-INSTALL-HERMES-0.21.0-2026-09-07.md).

### On many hosts the command is `/refine-cycle`, not `/refine`

Hermes ships its own built-in `/refine` (a background review fork), and
`register_command` silently drops a plugin command that collides with a built-in.
The plugin detects this at registration and takes `/refine-cycle` instead, so
every subcommand stays reachable:

```
/refine-cycle status
/refine-cycle audit
/refine-cycle dry-run
/refine-cycle session <session_id>
/refine-cycle rollback <id>
```

This matters more than a renaming usually would: typing `/refine` on such a
host does not fail — it reaches Hermes's own command and answers, so it is easy
to believe you are talking to this plugin when you are not.

**Do not assume this is new.** Confirmed on Hermes 0.21.0 and on 0.20.x
(`v2026.8.31`), so treat `/refine-cycle` as the likely name and check rather than
guess. `/refine-cycle status` names the command that answered; so does:

```
python -c "import refine; print(refine._built_in_command_exists('refine'))"
```

`True` means this plugin answers to `/refine-cycle`. Every `/refine …` example
below is written for hosts without the built-in.

### Why patch selection remains strict

Each bundled patch owns its marker table and target topology. Hermes 0.21.0 moved
`gateway/run.py` to `gateway/run_inbound.py` and `run_agent.py` to
`agent/turn_facade.py`; treating every host as the old topology would call a
correctly patched host `partial`. Backup, compilation, and rollback scope are
therefore derived from the selected patch headers, and an existing backup cannot
be rebound to another topology.

The installer uses clean `git apply` only. It does **not** use `git apply -3` or
reduce context to make a patch land: a semantic merge can compile while silently
breaking exact-client, one-request, or no-fallback guarantees. An unsupported
host fails closed with `llm_invocation_unavailable` rather than borrowing an
ambient route.

### A note on `plugins.scan_on_install`

Do not disable install scanning for this plugin. The tracked release tree now
receives a confirmable `caution` verdict rather than an unoverrideable
`dangerous` verdict. `plugins.scan_on_install: false` remains documented only as
an earlier diagnostic; it disables scanning for the whole profile.

---

## What it changes on your host

Three things, and they do not all happen at the same moment.

**The installer does two**, and `--plugin-only` declines both:

- Connects the plugin to the model already serving your session, so it never calls a model you did not choose. Without this, proposals fail closed.
- Raises the long-term memory limit to a floor of 4,400 characters. A floor: a higher value you set yourself is never lowered.

**Enabling the plugin does the third**, so `--plugin-only` does not opt you out of it. Hermes can queue every memory and skill write, the agent's own as much as this plugin's, until a person approves each one. With that queue on, lessons never land: no error, no output, writes piling up where nobody looks. So the plugin turns it off on load. One `write_approval: true` line inside the `memory:` or `skills:` block becomes `false`; comments, ordering and every other value are left alone, and your config is copied beside itself as `config.yaml.refine-bak` first. A config your administrator manages is detected and never touched.

All three are reversible with `python install.py --rollback`.

**If you actually use that approval queue** — you drain it, and you want to see every write before it lands — this plugin works against how you have set Hermes up, and you should not enable it. That is a good reason to pass.

## Installation

> **Note:** this is a plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/docs). It needs the plugin API available since Hermes 0.17.0 and does not run standalone. Install, registration, the full test suite, `/refine status`, and `/refine audit` are verified on Hermes 0.20.1 through 0.21.1. Only **new proposals** additionally require the matching host route patch, and the installer picks it by applicability: 0.21.0 and the 0.21.1 release tag take `assets/invocation-route-v0.21.0.patch`, while 0.21.1 main and later take `assets/invocation-route-v2026.9.10.patch`. See [Hermes version support](#hermes-version-support).

The plugin lives in `<HERMES_HOME>/plugins/refine/` — `~/.hermes/plugins/refine/`
on Linux and macOS, and `%LOCALAPPDATA%\hermes\plugins\refine\` on Windows.
Under a Hermes profile it follows that profile; the plugin resolves the location
through `hermes_constants.get_hermes_home()`.

> **Runtime data location.** The default `journal_dir` is
> `<HERMES_HOME>/refine`, separate from plugin source. On startup, an install
> still using the former `<HERMES_HOME>/plugins/refine` default is migrated under
> a cross-process lock: all artifacts are staged first, a completion marker is
> published last, and the old directory is renamed rather than deleted. If any
> copy or publication step fails, the intact legacy directory remains the active
> store for that process and `/refine status` reports the fallback. An explicitly
> configured non-empty `journal_dir` is never migrated automatically.

Install the repository, run the disclosed full installer from the installed
plugin directory, then enable and restart:

```bash
hermes plugins install Bergschloss/Refine-Cycle-for-Hermes-Agent
# Run the next command from <HERMES_HOME>/plugins/refine:
python install.py
hermes plugins enable refine
hermes gateway restart
```

`hermes plugins install` clones the repository into
`<HERMES_HOME>/plugins/refine/`. The following `python install.py` applies and
verifies the matching invocation-route patch and raises the two memory-limit
targets described below; when run from the installed directory, the plugin copy
step is an idempotent no-op. `plugins enable` registers the plugin, and the
restart activates both plugin and host changes.

To keep Hermes source untouched, omit `python install.py` or use
`python install.py --plugin-only` from a separate checkout. The plugin can then
provide status, audit, rollback, and journaling, but proposal runs stop with
`llm_invocation_unavailable`; the full two-target memory-floor change is not
made.

> **The plugin works inside the running gateway.** The LLM invocation route is
> bound by the live gateway process, so in a bare command-line process
> `refine_run` returns `llm_invocation_unavailable` by design (and `/refine
> status` names that blocker directly). Automatic refinement, proposals, and
> apply/rollback all run inside the gateway — test with a real session or the
> restart above, not with a one-shot script.

Then, optionally, configure it in `config.yaml`:

```yaml
plugins:
  enabled:
    - refine
  entries:
    refine:
      journal_dir: "<HERMES_HOME>/refine-data"   # keep data separate from plugin source
      llm:
        allow_model_override: false
        allow_provider_override: false
```

`plugins enable` manages the `enabled` list itself; the `entries` block holds
the plugin's own settings, and `journal_dir` keeps runtime data separate from
plugin source (see "Runtime data location" above).

Restart Hermes after any config change:

```bash
hermes gateway restart
```

Verify:

```
hermes plugins list
# refine  1.2.0  Measurement layer ...  enabled
```

Then check that automatic refinement can actually run:

```
/refine status
# auto: on
# turn interval: 25
# min messages: 15
# cooldown: 20 min
# edits today: 0/3
# model: your-cheap-model @ your-provider (source: live)
# journal: /home/you/.hermes/refine-data (does not exist yet, will be created on first write)
# blockers: none — automatic refinement is active
```

`blockers` lists every reason a pass would not start; `warnings` lists what does
not stop it but will cost you later, such as runtime data sitting in the plugin
directory, or a journal directory that could not be inspected at all.

Status is read-only: it creates no directory — not even the journal directory it
reports on — writes no journal record, spends no budget, and calls no model. It
does not reconcile pending approvals, so an unresolved staged edit still counts
toward the budget it reports.

### Host route patch — required for new proposals

The plugin asks the LLM through Hermes's *active invocation route*: the same
model binding that the user's live session uses, so that a proposal costs the
host's own provider creds and never a hardcoded key. Stock Hermes does not
expose that binding to plugins. The installer ships one patch per Hermes base:

- `assets/invocation-route-v2026.8.16.patch`
- `assets/invocation-route-v2026.8.31.patch`
- `assets/invocation-route-v0.21.0.patch`
- `assets/invocation-route-v2026.9.10.patch`

Each patch carries its own marker table and target topology. The 0.21.0 topology
uses `gateway/run_inbound.py` and `agent/turn_facade.py` where the older hosts
used `gateway/run.py` and `run_agent.py`. The 2026.9.10 patch shares that
topology and differs only in where its hunks anchor, so it is the 0.21 patch for
hosts above the 0.21.1 release tag. Its route-locked call also passes its own
`create` into the relay seam: upstream now defaults that seam to the
progress-hook wrapper, which re-sends a locked call as a stream and retries a
credit-limited 402 with a smaller cap — two requests on a path whose contract is
exactly one.

Which patch fits a host is decided by trying each candidate with
`git apply --check`, not by trusting a version string. This avoids accepting a
partially matching patch after upstream moves code while preserving hosts where
a patch still applies exactly.

- **Without the patch:** `/refine status`, `/refine audit`, `/refine rollback`,
  journaling, and the test suite all work. A proposal run stops honestly with
  `llm_invocation_unavailable` and journals the record.
- **With the patch:** proposal runs reach the exact active route (subject to the
  configured trust policy).

The installer requires a clean patch and never weakens context or uses a
three-way merge after `git apply --check` fails. It verifies route symbols,
compiles every touched Python file, imports the core module, and runs a
synthetic invocation-bound proposer smoke in a disposable `HERMES_HOME`. If a
check fails, the pre-patch state is restored. Backups are bound to the selected
patch and topology so a later run cannot reuse them for a different host
transaction.

```bash
# from the plugin directory
python install.py --patch-only   # apply and verify the host route patch, with backup
```

On hosts that already carry a complete known route, the installer reports
`patched` and makes no route change. Use
`install.py --rollback` to restore the recorded pre-install state.

### The memory budget the install raises

`install.py` raises Hermes's memory character limit to a floor of **4400**. This
is deliberate and it is for the plugin's sake, so it is stated here rather than
left to be discovered in a diff.

Stock Hermes ships `memory_char_limit: 2200` — roughly 800 tokens. That number was
chosen when the models driving Hermes were smaller and shorter-context; a compact
store was the right trade then. It is no longer the constraint it was: we measured a
lesson still firing at 8,811 characters, twice the floor this installer sets.

For this plugin the stock size is actively too small. Refine's whole output is
lessons written into that store, and it accumulates: on a real install, six applied
edits consumed about a third of the stock budget in a single day. A plugin that
fills the store it depends on is not usable at 2200.

Two files are changed, because neither alone reaches everybody:

- `<HERMES_HOME>/config.yaml` — Hermes writes `memory_char_limit` into the
  generated config, so for anyone who has already run Hermes this file is what
  decides, and the code default is never consulted.
- `hermes_cli/config_defaults.py` in the Hermes checkout — what a user who
  installs the plugin *before* Hermes has ever generated a config will get.
  `--plugin-only` skips this one, since that flag promises no writes into the host
  checkout, and says so at the time.

The rule is a **floor, not an override**:

| Current value | What happens |
|---|---|
| below 4400 (including the stock 2200) | raised to 4400 |
| exactly 4400 | nothing |
| above 4400 | left alone — your number wins |

So a limit you chose yourself is never overwritten and never *lowered*, and
running the installer twice changes nothing the second time. `--rollback` reverses
it by putting each file's own previous number back — not a blanket 2200, so a host
that installed at 3000 returns to 3000. It reverses one integer rather than
restoring a file copy, because `config.yaml` is a live file you edit and a restored
copy would silently discard everything else you changed since.

The plugin itself never hardcodes 4400. It reads whatever limit the host reports
and shows it to you at every write (for example `memory 1443/4400`), so raising the limit
further is a host decision the plugin follows rather than fights.

**If you want to raise it, here is what we measured.** A lesson still fired on every
probe at 8,811 characters, whether it sat at the front of the note or buried in the
middle, so 4400 is not a ceiling the model imposes. The cost of going higher is small,
because the note is billed as fresh input once per session and read from cache after
that, and because output tokens do not change with memory at all:

| memory change | cost per session |
|---|---|
| 2200 → 4400 | +5 to 7% |
| 4400 → 8800 | +9 to 12% |
| 2200 → 8800 | +14 to 20% |

The range covers four different pricing shapes; the answer barely moves between them.
Token counts, method and the pricing assumptions are in
[`docs/evidence/memory-cost.md`](evidence/memory-cost.md), and the runs behind them
are section 4.5 of the [research report](RESEARCH-REPORT-2026-09-12.md).

We have not raised the shipped default on the strength of this. The lesson under test
named its action outright, which is the strongest form a lesson takes, and a vaguer one
may fade sooner than 8,811 characters. Above that we measured nothing.

---

