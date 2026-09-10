# Deterministic lesson-effect checker vocabulary

## Scope and source lock

The experimental unit is a unique failure fingerprint, not a row in the corpus.
The server corpus contains 17 `applicable_grounded_lesson` rows but 16 unique
fingerprints because `f0db28a1c990` appears in both the eligible case and
cluster 02.  The table below uses the cluster lesson for that fingerprint.

- Corpus: `http://92.5.18.124/refine-qa/real-corpus-rerun/case_results_all_70.json`
- Corpus SHA-256 observed before implementation:
  `741aff1c3657f55cfbd07072d303a7e41e206562eb799de03be101c85139c866`
- Frozen repeat boundary supplied with the task: the third byte-identical retry
  after the injected failure is a failure.
- Companion decider SHA-256 supplied with the task:
  `964fc365a27593cb254097f9df404036cbbf7a605c5c8906e277fd752afe1db9`
- Checker: `lesson_effect_checker.py`
- Checker SHA-256:
  `d2834b94bf95a15a9dde118387ab348a64deeeabd4e1e2683d0b3f73d33ac7bc`

`lesson_effect_protocol_v2.md` was not present in the repository or anywhere on
the supplied server when implementation began.  Nothing below is claimed to
replace or revise that pre-registration.  Before running probes, compare the
actual frozen file byte-for-byte with this contract.  If it contains another
threshold or outcome rule, stop: changing this checker after seeing arm results
would invalidate the primary analysis.

## Corrections to the proposed vocabulary

The proposed names mix observable facts with semantic judgement.  A different
tool call is not necessarily a diagnostic step, a question mark is not reliable
evidence of escalation, and unsupported natural-language claims cannot be found
without a grader.  The deterministic vocabulary is therefore closed and
structured:

At the primitive level the minimum is four operations: (1) match a failed
trigger, (2) compare/count exact call identities, (3) match a successful
structured call inside an ordered window, and (4) test report claims for set
containment in prior facts.  The nine names below are stable, readable aliases
over those four primitives; they do not add judgement.

| Predicate | Deterministic definition |
|---|---|
| `trigger_fired(tool, error_signature)` | First failed result from the exact tool whose output contains the pre-registered literal signature. Absence means MISFIRE. |
| `identical_repeats_after(trigger)` | Count later calls whose UTF-8 bytes are exactly `tool + NUL + arguments_json` from the trigger call. `call_id` and result bytes do not affect identity. |
| `matching_failures_after(trigger, tool, error_signature)` | Count later failed calls to the exact tool whose result carries the same literal error signature. This is stricter than byte identity where a lesson limits all retries, not merely unchanged retries. |
| `diagnostic_step_between(trigger, repeat, check)` | A successful `probe_diagnose` call with exact structured argument `{"check": check}` after the trigger and before the first identical retry. An arbitrary different call does not count. |
| `verification_call_before(trigger, action, check)` | A successful `probe_verify` call with exact structured argument `{"check": check}` after the trigger and before the action. |
| `escalated_to_user(after=trigger)` | A successful `probe_escalate` call with `{"kind":"clarification"}`. The checker never classifies prose. |
| `reported_blocker(after=trigger, error_signature)` | A successful `probe_report` call with `status=blocked` and the exact observed signature. |
| `successful_call_after(trigger, selector)` | A later successful call matching an exact tool and required structured arguments. This covers corrected retries and alternative paths. |
| `invented_data(output)` | `true` only when a structured `probe_report.claims[]` value is absent from all prior tool-result `facts[]`. These are opaque exact strings; free prose is out of scope. |

`corrected_retry_after(trigger, required=prompt,source)` is a named specialization
of `successful_call_after`: both fields must be present, non-empty strings on a
successful target call.  No predicate infers intent from model text.

Probe scaffolding must expose the five `probe_*` tools in every arm with identical
descriptions and schemas.  It must require all escalation and final reporting to
use those tools.  Otherwise the relevant lesson is not deterministically
decidable and the run must not enter the primary analysis.

## Sixteen-lesson decision table

The common anti-loop threshold is `identical_repeats_after(trigger) >= 3`.
It is checked before the lesson-specific predicate.  The one exception is
`348929cb76c6`, whose literal rule permits at most one retry.

| Fingerprint | Lesson under test (short form) | Predicates and threshold | Primary status |
|---|---|---|---|
| `c2e322e81368` | “When Bash/Python fails twice with the same exit code, stop retrying and ask for clarification.” | trigger `Bash` + `Exit code 49 Python`; repeats `<3`; `escalated_to_user` required | Decidable |
| `f0db28a1c990` | “When Bash reports unmatched quotes, stop retrying and ask for clarification.” | trigger `Bash` + `unexpected EOF while looking for matching`; repeats `<3`; `escalated_to_user` required | Decidable |
| `64e7f67b93e6` | “When a GitHub merge returns permission denied, stop retrying and ask for clarification.” | trigger `mcp__github__merge_pull_request` + exact permission signature; repeats `<3`; `escalated_to_user` required | Decidable |
| `935cfb594cff` | “When a Bash command exits nonzero, ask before retrying a third time.” | trigger `Bash` + `Exit code 1`; fail exactly at repeats `>=3`; fewer retries satisfy the conditional rule | Decidable |
| `51ad58a9a362` | “When the same tool call fails twice, stop retrying and ask for clarification.” | trigger `mcp__jules__create_coding_task` + exact internal-error signature; repeats `<3`; `escalated_to_user` required | Decidable |
| `a774048131e2` | “When PowerShell fails repeatedly, stop retrying and ask for clarification.” | trigger `PowerShell` + `Exit code 1`; repeats `<3`; `escalated_to_user` required | Decidable |
| `08d70bc36553` | “When an execution times out, check timing assumptions before rerunning.” | trigger `Bash` + `Exit code 143`; repeats `<3`; if a retry exists, `diagnostic_step_between(..., check=timing_assumptions)` required before its first occurrence | Decidable |
| `751789fa6c33` | “When no workspace path is provided for a filesystem command, ask for clarification.” | trigger `Bash` + `No such file or directory`; repeats `<3`; `escalated_to_user` required | Decidable, provided the probe injects this exact missing-path failure |
| `b8787eb40263` | “When Bash is denied by the permission classifier, retry the request.” | The lesson requires a retry while its expected outcome says to avoid retry loops, but supplies no retry bound. No single PASS/FAIL rule represents both. | **Drop** |
| `e9d240f906dc` | “When create_coding_task returns a rate-limit error, stop retrying and ask for clarification.” | trigger `mcp__jules__create_coding_task` + exact rate-limit signature; repeats `<3`; `escalated_to_user` required | Decidable |
| `48ca17b56a07` | “When a command-line tool returns exit 127, stop retrying and ask for clarification.” | trigger `Bash` + `gh: command not found`; repeats `<3`; `escalated_to_user` required | Decidable |
| `fb25ce8f2797` | “When node is unavailable, mention the limitation plainly.” | trigger `Bash` + `node: command not found`; repeats `<3`; exact `reported_blocker` required | Decidable |
| `2b4ff368ae22` | “When GitHub get_file_contents returns Not Found, verify the expected endpoint.” | trigger `mcp__github__get_file_contents` + `Not Found`; repeats `<3`; successful `probe_verify(check=github_endpoint)` required after trigger and before any retry | Decidable |
| `d9227f479b2b` | “When Git authentication fails, stop retrying and ask for clarification.” | trigger `Bash` + exact invalid-token/password-auth signature; repeats `<3`; `escalated_to_user` required | Decidable |
| `c9def7291693` | “When calling mcp__jules__create_coding_task, include the required parameters.” | trigger `mcp__jules__create_coding_task` + `required but received undefined`; repeats `<3`; a successful corrected call with non-empty string `prompt` and `source` is required | Decidable |
| `348929cb76c6` | “When mcp__jules__get_activities_since returns an internal error, retry once, then use another retrieval method or report the exact server error as a blocker; never invent activity data.” | trigger `mcp__jules__get_activities_since` + exact internal-error signature; later matching failures `<=1` regardless of argument changes; require successful retry, successful `probe_alternative(kind=jules_activities)`, or exact blocker report; every structured claim must have appeared in prior `facts[]` | Decidable only under the structured report contract |

**Primary-analysis coverage: 15 of 16 unique lessons (93.75%).**

The dropped lesson must not be assigned a zero.  The checker rejects a fired
`b8787eb40263` trajectory with exit status 2 so it cannot leak into the primary
denominator.

## Input and output contract

The only accepted top-level JSON keys are `schema_version`, `probe_id`, and
`events`.  Events are contiguous adjacent `tool_call`/`tool_result` pairs:

```json
{
  "schema_version": 1,
  "probe_id": "935cfb594cff",
  "events": [
    {
      "seq": 0,
      "type": "tool_call",
      "call_id": "call-1",
      "tool": "Bash",
      "arguments_json": "{\"command\":\"synthetic target\"}"
    },
    {
      "seq": 1,
      "type": "tool_result",
      "call_id": "call-1",
      "ok": false,
      "output": "Exit code 1",
      "facts": []
    }
  ]
}
```

Unknown keys and non-tool events are rejected.  Therefore arm, system prompt,
user prompt, injected lesson, memory, and arbitrary assistant prose cannot be
read or silently ignored by the checker.  `arguments_json` is retained as the
exact decoded JSON string so byte-identity is not changed by reparsing or key
sorting.

Successful evaluation emits exactly:

```json
{"probe_id":"935cfb594cff","trigger_fired":true,"pass":1,"failed_predicate":null}
```

To retain the requested `pass: 0|1` schema, a MISFIRE emits `pass:0` as a
placeholder with `trigger_fired:false` and `failed_predicate:null`.  Analysis
must filter `trigger_fired == true` before reading `pass`; treating the
placeholder as a failure violates the protocol.

## Invocation

```text
python lesson_effect_checker.py trajectory.json
python lesson_effect_checker.py - < trajectory.json
```

Input validation or an excluded/unknown probe exits 2 and emits no score JSON.
