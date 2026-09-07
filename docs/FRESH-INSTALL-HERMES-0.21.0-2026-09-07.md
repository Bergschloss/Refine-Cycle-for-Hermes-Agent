# Refine Cycle fresh-install verification on Hermes 0.21.0

**Date:** 2026-09-07
**Platform:** Windows
**Result:** **FAIL for an ordinary clean install; FAIL for full runtime usability.**

## Executive verdict

The README installation sequence does **not** currently produce a working Refine Cycle installation on the updated local Hermes 0.21.0.

There are two independent blockers:

1. **Hermes 0.21.0 blocks the public GitHub repository during installation.** The new plugin security scanner assigns a `DANGEROUS` verdict with 179 findings. `--force` is deliberately unable to override that verdict.
2. **After using Hermes's supported scanner opt-out in an isolated profile, the plugin installs, enables, imports, and registers, but its invocation-route patch is incompatible with Hermes 0.21.0.** Refine therefore cannot call the active session's LLM and proposal-producing runs fail closed with `llm_invocation_unavailable`.

The plugin's own suite is green and Hermes Doctor accepts its runtime registrations. Those facts establish that the plugin code is internally healthy; they do not overcome either integration blocker.

| Question | Result |
|---|---|
| Does the documented public command install with default Hermes security settings? | **No** |
| Does failed install leave a half-installed plugin? | **No** |
| Can a trusted operator install it after disabling install scanning? | **Yes** |
| Does Hermes 0.21.0 import and register it? | **Yes** |
| Does the Sep-2026 compatibility scanner find removed imports? | **No** |
| Does the complete plugin test suite pass? | **Yes: 1,193 tests, 11 skipped** |
| Can Refine create new proposals on stock Hermes 0.21.0? | **No** |
| Is the documented installation ready for ordinary users? | **No** |

## Environment under test

Local Hermes reported:

```text
Hermes Agent v0.21.0 (2026.8.31) · upstream 693641aa
Install directory: C:\Users\relig\AppData\Local\hermes\hermes-agent
Install method: git
Python: 3.11.15
OpenAI SDK: 2.24.0
```

Exact host checkout:

```text
693641aa8b4359c602283bdbbc14041e03bc47bc
```

The checkout was clean before the test and remained clean afterwards.

The public plugin clone used for validation was:

```text
Repository: https://github.com/Bergschloss/Refine-Cycle-for-Hermes-Agent.git
Commit:     fdee1e3377d4498f53640752cd5900f77392fe36
Tag:        v1.0.0
Manifest:   refine 1.0.0
```

## Isolation and privacy controls

The test did not install into or restart the real Hermes profile. Every mutating Hermes command ran with:

```powershell
$env:HERMES_HOME = 'G:\Kiro\Refine-Cycle\scratch\fresh-install-v0210\home'
$env:LOCALAPPDATA = 'G:\Kiro\Refine-Cycle\scratch\fresh-install-v0210\localappdata'
$env:PYTHONIOENCODING = 'utf-8'
```

The disposable profile started without `state.db`. After install, enable, Doctor, compatibility checks, and the test suite:

```text
Disposable plugin manifest: present
Disposable state.db:         absent
Disposable refine journal:   absent
.install-* staging residue:  0
```

No real session database was copied, opened, or supplied to the plugin. No real trajectory content was sent to a model or external service. The only required network operation was Hermes cloning the public GitHub repository.

A live foreground gateway was deliberately not started. `gateway run` can inherit provider/platform secrets from the process and project environment, and plugin Python runs in-process rather than in a security sandbox. Hermes Doctor is the supported isolated command that performs real discovery, import, and registration while blocking outbound socket connections.

## Test 1 — documented clean installation

Command, made non-interactive without changing install semantics:

```powershell
hermes plugins install Bergschloss/Refine-Cycle-for-Hermes-Agent --no-enable
```

Result:

```text
INSTALL_RC=1
Decision: BLOCKED — Blocked (community source + dangerous verdict, 179 findings).
--force does not override a dangerous verdict.
```

Hermes did not create the final plugin directory. `hermes plugins list --user --json` returned an empty list, `state.db` remained absent, and the temporary `.install-*` clone was cleaned up.

### Scanner findings

The scanner reported critical/high findings across documentation, test fixtures, and installer code. Representative categories included:

- prompt-injection strings used by sanitization tests;
- synthetic API keys, tokens, and private-key fixtures used by redaction tests;
- system password-file paths and exfiltration phrases used by safety tests;
- subprocess execution in the installer and tests;
- `sudo`/service restart commands in Linux documentation;
- large GIF and test-suite files.

Many listed strings are visibly defensive test fixtures or documentation rather than executed malicious behavior. However, this run did **not** manually adjudicate all 179 findings, so it would be incorrect to label every finding a false positive. The user-visible fact is simpler: the current public repository is not installable under Hermes 0.21.0's default policy.

`--force` is not a workaround. Hermes permits it for a `CAUTION` result, but never for `DANGEROUS`.

## Test 2 — supported scanner opt-out in the disposable profile

Hermes 0.21.0 supports this configuration:

```yaml
plugins:
  scan_on_install: false
```

The value must be the YAML boolean `false`, not the string `"false"`.

After adding it only to the disposable profile, these commands succeeded:

```powershell
hermes plugins install Bergschloss/Refine-Cycle-for-Hermes-Agent --no-enable
hermes plugins enable refine --no-allow-tool-override
hermes plugins list --user --json
hermes plugins show refine
```

Observed result:

```text
INSTALL_RC=0
ENABLE_RC=0
Status: enabled
Source: git
Version: 1.0.0
Tool override: not granted
```

This proves that the repository can be cloned into the correct Windows `HERMES_HOME`, its manifest can be parsed, and Hermes can persist the enabled state. It is a diagnostic result, not a recommended end-user installation path: disabling the scanner skips all install scanning for that profile.

## Test 3 — real Hermes runtime discovery and registration

Command:

```powershell
hermes plugins doctor refine --ci
```

Result:

```text
DOCTOR_RC=0
Plugin Doctor: ...\plugins\refine
manifest: refine 1.0.0 (standalone)
OK: runtime discovery, manifest parsing, import, and registration passed
registrations: 1 tool(s), 7 hook(s)
```

This is stronger than `plugins list`: Doctor uses Hermes's real `PluginManager`, imports the plugin, calls its registration path in a temporary profile, blocks outbound sockets, and restores global runtime state afterwards.

Registered surface:

- tool: `refine_run`;
- hooks: `pre_llm_call`, `pre_tool_call`, `post_llm_call`, `on_session_end`, `on_session_reset`, `subagent_start`, and `subagent_stop`.

## Test 4 — Hermes Sep-2026 decomposition compatibility

Command:

```powershell
hermes plugins compat G:\Kiro\Refine-Cycle\scratch\fresh-install-v0210\home\plugins\refine --json
```

Result:

```json
{
  "removal_date": "2026-09-14",
  "in_effect": false,
  "plugins": {}
}
```

Exit code was 0. The current plugin does not import any path flagged by Hermes's Sep-2026 compatibility manifest.

## Test 5 — complete installed-plugin suite

The suite was run from the public clone using Hermes's own virtualenv Python:

```powershell
C:\Users\relig\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -m tests.run_tests
```

Result:

```text
Ran 1193 tests in 96.990s
OK (skipped=11)
SUITE_RC=0
```

All 11 skips were the expected Windows skips for `install.sh` tests requiring a working Bash path. The Python installer, Windows path resolution, plugin import coverage, redaction, aggregation, journal, rollback, concurrency, proposal, and usefulness tests ran successfully.

## Test 6 — invocation-route compatibility with Hermes 0.21.0

Read-only command:

```powershell
python install.py --status `
  --hermes-src C:\Users\relig\AppData\Local\hermes\hermes-agent
```

Result:

```text
PATCH_STATUS_RC=0
State: incompatible
no bundled route patch applies to base 693641aa8b
tried:
  invocation-route-v2026.8.31.patch
  invocation-route-v2026.8.16.patch
The patch needs rebasing onto this host's version.
```

The command returned 0 because `--status` successfully reported state; `State: incompatible` is the compatibility result.

### Why this blocks the actual feature

Refine accepts an LLM facade only when Hermes marks it `invocation_bound`. This is intentional: trajectory evidence must use the exact active session route and must not silently fall back to another provider, profile, or model.

Stock Hermes 0.21.0 has `PluginContext.llm` and `PluginLlm`, but it does not provide the route-bound contract expected by Refine:

- no `PluginLlm.invocation_bound`;
- no `PluginLlm.bind_invocation()`;
- no `PluginLlmInvocationError` route contract;
- no `plugin_invocation_scope` around plugin commands/turns;
- no exact-client, single-attempt, no-fallback plugin call path.

The closest Hermes runtime context is not equivalent: it can fall back to ambient configuration and the normal auxiliary path can retry or fail over. Refine correctly refuses to use it.

Therefore `_session_llm()` returns `None`, and proposal-producing operations stop before trajectory evidence is sent to any model:

```text
llm_invocation_unavailable
```

Expected working/non-working surfaces on this host:

| Surface | Expected state |
|---|---|
| plugin discovery/import/registration | Works |
| status | Works |
| audit | Works |
| rollback of an existing Refine journal entry | Works |
| new manual proposal | Blocked |
| dry-run proposal | Blocked |
| explicit-session proposal | Blocked |
| `refine_run` proposal | Blocked |
| automatic proposal | Blocked |

### Why the old patch cannot simply be forced

Hermes 0.21.0 decomposed and moved the relevant call paths. The old patch targets no longer match the current architecture: command dispatch moved, gateway inbound handling moved out of the former monolith, turn execution moved into a facade, and the auxiliary LLM retry/fallback flow was redesigned. This is semantic drift across the route, not a harmless line-offset conflict.

No patch was applied during this verification. Forcing the old patch or using a three-way merge would risk a partially working route that silently violates Refine's privacy and single-route guarantees.

## Integrity checks

After all checks:

```text
Hermes checkout HEAD: 693641aa8b4359c602283bdbbc14041e03bc47bc
Hermes git status:     clean
Hermes git diff:       empty
```

No gateway restart, host patch, dependency installation, real-profile plugin enable, or real database operation was performed.

## Required fixes

### P0 — make the public installation pass Hermes security scanning

The ordinary command must work with `plugins.scan_on_install: true`.

Recommended direction:

1. Publish a production-only plugin artifact/tree containing runtime files, manifest, route patches, and only the documentation required at runtime; keep adversarial fixtures and the large development suite in the source repository/CI artifact.
2. Alternatively, coordinate with Hermes on a scanner-aware package format or narrowly scoped ignore mechanism. Do not instruct ordinary users to disable scanning globally.
3. Add a release check that runs the real Hermes 0.21 installer with scanning enabled against the exact public artifact.

Rewording a few fixtures is unlikely to be sufficient: the current scan has 179 findings across several intentional safety-test categories.

### P0 — add an invocation-route capability for Hermes 0.21.0

Either upstream the route-bound capability into Hermes or ship a new patch built against host commit `693641aa8b` and its decomposed topology. The implementation must preserve the existing invariants:

- exact active provider/model/client;
- immutable invocation binding;
- no ambient-config fallback;
- no provider/model override;
- one physical request per attempt;
- no hidden same-provider retry or provider fallback;
- fail closed when the route is incomplete;
- task/thread context propagation for plugin handlers;
- backup, classification, verification, and rollback for every touched host file.

The installer currently assumes one shared eight-file patch topology. Because Hermes 0.21 moved some targets, patch metadata should become patch-specific before adding the new base; otherwise classification and rollback can misreport partial state.

### P1 — publish an explicit support matrix

Until both P0 items land, the README should state:

- Hermes 0.20.1/0.20.2 are the last verified bases;
- Hermes 0.21.0 blocks the repository under default scanning;
- scanner opt-out only proves physical install and should not be presented as the normal path;
- the bundled route patch is incompatible with stock 0.21.0;
- a green Doctor/test suite does not mean proposal runs are available.

### P1 — add release-gate integration checks

A release should fail unless all of these pass on the declared Hermes version:

```text
plugins install with default scanner enabled
plugins enable
plugins doctor --ci
plugins compat
install.py --status reports stock or patched, never incompatible
invocation-bound proposal smoke test in an isolated host fixture
full plugin suite on Windows and Linux
```

## Operator guidance

Do **not** deploy this version to ordinary Hermes 0.21.0 users as fully working.

For development only, a trusted operator can temporarily set `plugins.scan_on_install: false`, install the known commit, and immediately restore scanning. That still does not enable proposal runs on stock Hermes 0.21.0, so it is useful only for Doctor/status/audit testing until the route capability is rebased.

Do not apply either old bundled patch with force. Do not restart the real gateway merely to test this state: the missing route is already proven by read-only host classification and source-level capability inspection.

## Final assessment

**Release compatibility with Hermes 0.21.0: BLOCKED.**

The plugin itself is internally consistent: all 1,193 tests pass, Hermes imports it, all declared registrations are valid, and the new decomposition scanner finds no removed imports. The release nevertheless fails the two checks that matter to a new user: default installation is rejected, and the core proposal path has no compatible invocation route. Both blockers must be fixed before the README's three-command installation can be called supported on Hermes 0.21.0.
