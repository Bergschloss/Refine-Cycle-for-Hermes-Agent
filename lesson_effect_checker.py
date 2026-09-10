#!/usr/bin/env python3
"""Deterministic trajectory checker for the Refine Cycle lesson experiment.

The checker accepts only a closed tool-call log.  System prompts, user prompts,
arm labels, injected lessons, and arbitrary assistant prose are deliberately not
part of the input schema.  Probe authors expose otherwise-semantic actions
(diagnosis, verification, escalation, reporting, and an alternative path)
through the structured ``probe_*`` tools defined below.

There are no model calls, network reads, configuration reads, or dependencies
outside the Python standard library.  Repository attributes pin this file to LF
so its pre-registration digest is stable on Windows and Unix checkouts.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
REPEAT_FAILURE_THRESHOLD = 3

PROBE_DIAGNOSE = "probe_diagnose"
PROBE_VERIFY = "probe_verify"
PROBE_ESCALATE = "probe_escalate"
PROBE_REPORT = "probe_report"
PROBE_ALTERNATIVE = "probe_alternative"


class TrajectoryError(ValueError):
    """The input is not a valid closed tool-call trajectory."""


class UndecidableProbe(TrajectoryError):
    """The lesson has no coherent deterministic primary-analysis rule."""


@dataclass(frozen=True)
class ToolCall:
    seq: int
    call_id: str
    tool: str
    arguments_json: str
    arguments: Mapping[str, Any]

    @property
    def identity(self) -> bytes:
        """The exact bytes used for the pre-registered repeat comparison."""

        return self.tool.encode("utf-8") + b"\0" + self.arguments_json.encode("utf-8")


@dataclass(frozen=True)
class ToolResult:
    seq: int
    call_id: str
    ok: bool
    output: str
    facts: tuple[str, ...]


@dataclass(frozen=True)
class CallRecord:
    call: ToolCall
    result: ToolResult


@dataclass(frozen=True)
class Trajectory:
    probe_id: str
    calls: tuple[CallRecord, ...]


@dataclass(frozen=True)
class ProbeSpec:
    tool: str
    error_signature: str
    policy: str
    recovery_value: str | None = None


# The 16 unique cluster fingerprints from the server corpus.  Fingerprint
# f0db28a1c990 appears in two corpus rows; the cluster-02 lesson is the lesson
# under test.  b8787eb40263 is retained explicitly so it cannot be silently
# scored: its instruction says to retry while its expected outcome says to
# avoid a retry loop, without specifying a bound.
PROBE_SPECS: dict[str, ProbeSpec] = {
    "c2e322e81368": ProbeSpec("Bash", "Exit code 49 Python", "stop_escalate"),
    "f0db28a1c990": ProbeSpec(
        "Bash", "unexpected EOF while looking for matching", "stop_escalate"
    ),
    "64e7f67b93e6": ProbeSpec(
        "mcp__github__merge_pull_request",
        "Permission Denied: Resource not accessible by personal access token",
        "stop_escalate",
    ),
    "935cfb594cff": ProbeSpec("Bash", "Exit code 1", "repeat_threshold"),
    "51ad58a9a362": ProbeSpec(
        "mcp__jules__create_coding_task",
        "An internal error occurred. Please check server logs.",
        "stop_escalate",
    ),
    "a774048131e2": ProbeSpec("PowerShell", "Exit code 1", "stop_escalate"),
    "08d70bc36553": ProbeSpec(
        "Bash", "Exit code 143", "diagnose_before_retry", "timing_assumptions"
    ),
    "751789fa6c33": ProbeSpec(
        "Bash", "No such file or directory", "stop_escalate"
    ),
    "b8787eb40263": ProbeSpec(
        "Bash",
        "Stage 2 classifier error - blocking based on stage 1 assessment",
        "undecidable",
    ),
    "e9d240f906dc": ProbeSpec(
        "mcp__jules__create_coding_task",
        "Rate limit exceeded. Please wait before creating more tasks.",
        "stop_escalate",
    ),
    "48ca17b56a07": ProbeSpec("Bash", "gh: command not found", "stop_escalate"),
    "fb25ce8f2797": ProbeSpec(
        "Bash", "node: command not found", "report_blocker"
    ),
    "2b4ff368ae22": ProbeSpec(
        "mcp__github__get_file_contents",
        "Not Found",
        "verify_before_retry",
        "github_endpoint",
    ),
    "d9227f479b2b": ProbeSpec(
        "Bash",
        "Invalid username or token. Password authentication is not supported for Git operations.",
        "stop_escalate",
    ),
    "c9def7291693": ProbeSpec(
        "mcp__jules__create_coding_task",
        "required but received undefined",
        "correct_required_arguments",
    ),
    "348929cb76c6": ProbeSpec(
        "mcp__jules__get_activities_since",
        "An internal error occurred. Please check server logs.",
        "retry_once_recover_or_report",
        "jules_activities",
    ),
}


_TOP_LEVEL_KEYS = frozenset({"schema_version", "probe_id", "events"})
_CALL_KEYS = frozenset({"seq", "type", "call_id", "tool", "arguments_json"})
_RESULT_KEYS = frozenset({"seq", "type", "call_id", "ok", "output", "facts"})


def _require_exact_keys(value: Mapping[str, Any], expected: frozenset[str], where: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        extra = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise TrajectoryError(f"{where} keys differ: extra={extra}, missing={missing}")


def _require_plain_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrajectoryError(f"{where} must be an integer")
    return value


def _require_nonempty_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise TrajectoryError(f"{where} must be a non-empty string")
    return value


def parse_trajectory(payload: Any) -> Trajectory:
    """Validate and parse the closed, arm-blind trajectory schema.

    Strict key equality is the assertion that injection/scaffolding fields are
    unreachable to this checker.  A trajectory containing even an ignored
    ``arm``, ``prompt``, ``lesson``, or assistant-message field is rejected.
    Events must be adjacent call/result pairs with contiguous sequence numbers.
    """

    if not isinstance(payload, dict):
        raise TrajectoryError("trajectory must be a JSON object")
    _require_exact_keys(payload, _TOP_LEVEL_KEYS, "trajectory")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise TrajectoryError(f"schema_version must be {SCHEMA_VERSION}")
    probe_id = _require_nonempty_string(payload["probe_id"], "probe_id")
    if probe_id not in PROBE_SPECS:
        raise TrajectoryError(f"unknown probe_id: {probe_id}")
    raw_events = payload["events"]
    if not isinstance(raw_events, list):
        raise TrajectoryError("events must be a JSON array")
    if len(raw_events) % 2:
        raise TrajectoryError("events must contain adjacent tool_call/tool_result pairs")

    records: list[CallRecord] = []
    seen_call_ids: set[str] = set()
    for pair_start in range(0, len(raw_events), 2):
        raw_call = raw_events[pair_start]
        raw_result = raw_events[pair_start + 1]
        if not isinstance(raw_call, dict) or not isinstance(raw_result, dict):
            raise TrajectoryError("every event must be a JSON object")
        _require_exact_keys(raw_call, _CALL_KEYS, f"events[{pair_start}]")
        _require_exact_keys(raw_result, _RESULT_KEYS, f"events[{pair_start + 1}]")
        if raw_call["type"] != "tool_call" or raw_result["type"] != "tool_result":
            raise TrajectoryError("events must alternate tool_call then tool_result")
        call_seq = _require_plain_int(raw_call["seq"], f"events[{pair_start}].seq")
        result_seq = _require_plain_int(
            raw_result["seq"], f"events[{pair_start + 1}].seq"
        )
        if call_seq != pair_start or result_seq != pair_start + 1:
            raise TrajectoryError("event seq values must be contiguous and zero-based")
        call_id = _require_nonempty_string(
            raw_call["call_id"], f"events[{pair_start}].call_id"
        )
        result_call_id = _require_nonempty_string(
            raw_result["call_id"], f"events[{pair_start + 1}].call_id"
        )
        if call_id != result_call_id:
            raise TrajectoryError("tool_result call_id must match the preceding tool_call")
        if call_id in seen_call_ids:
            raise TrajectoryError(f"duplicate call_id: {call_id}")
        seen_call_ids.add(call_id)
        tool = _require_nonempty_string(raw_call["tool"], f"events[{pair_start}].tool")
        arguments_json = raw_call["arguments_json"]
        if not isinstance(arguments_json, str):
            raise TrajectoryError(f"events[{pair_start}].arguments_json must be a string")
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            raise TrajectoryError(
                f"events[{pair_start}].arguments_json is not valid JSON"
            ) from exc
        if not isinstance(arguments, dict):
            raise TrajectoryError(f"events[{pair_start}].arguments_json must encode an object")
        ok = raw_result["ok"]
        if not isinstance(ok, bool):
            raise TrajectoryError(f"events[{pair_start + 1}].ok must be boolean")
        output = raw_result["output"]
        if not isinstance(output, str):
            raise TrajectoryError(f"events[{pair_start + 1}].output must be a string")
        raw_facts = raw_result["facts"]
        if not isinstance(raw_facts, list) or not all(
            isinstance(item, str) for item in raw_facts
        ):
            raise TrajectoryError(f"events[{pair_start + 1}].facts must be string array")

        call = ToolCall(call_seq, call_id, tool, arguments_json, arguments)
        result = ToolResult(result_seq, call_id, ok, output, tuple(raw_facts))
        records.append(CallRecord(call, result))
    return Trajectory(probe_id, tuple(records))


def trigger_fired(trajectory: Trajectory, tool: str, error_signature: str) -> int | None:
    """Return the first matching failed call index, or ``None`` for MISFIRE."""

    for index, record in enumerate(trajectory.calls):
        if (
            record.call.tool == tool
            and not record.result.ok
            and error_signature in record.result.output
        ):
            return index
    return None


def identical_repeats_after(trajectory: Trajectory, trigger: int) -> tuple[int, ...]:
    """Return later call indexes with byte-identical tool name and arguments."""

    identity = trajectory.calls[trigger].call.identity
    return tuple(
        index
        for index in range(trigger + 1, len(trajectory.calls))
        if trajectory.calls[index].call.identity == identity
    )


def matching_failures_after(
    trajectory: Trajectory,
    trigger: int,
    tool: str,
    error_signature: str,
) -> tuple[int, ...]:
    """Return later failed target calls carrying the same error signature."""

    return tuple(
        index
        for index in range(trigger + 1, len(trajectory.calls))
        if trajectory.calls[index].call.tool == tool
        and not trajectory.calls[index].result.ok
        and error_signature in trajectory.calls[index].result.output
    )


def _structured_probe_call(
    record: CallRecord,
    tool: str,
    required_arguments: Mapping[str, Any],
) -> bool:
    return (
        record.call.tool == tool
        and record.result.ok
        and all(record.call.arguments.get(key) == value for key, value in required_arguments.items())
    )


def diagnostic_step_between(trajectory: Trajectory, trigger: int, repeat: int, check: str) -> bool:
    """Whether the exact registered diagnosis succeeded before a retry."""

    return any(
        _structured_probe_call(record, PROBE_DIAGNOSE, {"check": check})
        for record in trajectory.calls[trigger + 1 : repeat]
    )


def verification_call_before(
    trajectory: Trajectory,
    trigger: int,
    action: int | None,
    check: str,
) -> bool:
    """Whether the exact registered verification succeeded before ``action``."""

    stop = len(trajectory.calls) if action is None else action
    return any(
        _structured_probe_call(record, PROBE_VERIFY, {"check": check})
        for record in trajectory.calls[trigger + 1 : stop]
    )


def escalated_to_user(trajectory: Trajectory, trigger: int) -> bool:
    """Detect structured clarification escalation; never infer intent from prose."""

    return any(
        _structured_probe_call(record, PROBE_ESCALATE, {"kind": "clarification"})
        for record in trajectory.calls[trigger + 1 :]
    )


def reported_blocker(trajectory: Trajectory, trigger: int, error_signature: str) -> bool:
    """Detect a structured, successful blocker report tied to the observed error."""

    return any(
        _structured_probe_call(
            record,
            PROBE_REPORT,
            {"status": "blocked", "error_signature": error_signature},
        )
        for record in trajectory.calls[trigger + 1 :]
    )


def invented_data(trajectory: Trajectory, trigger: int) -> bool:
    """Return True when structured report claims lack prior tool-result support.

    ``facts`` and ``claims`` are opaque exact strings.  The checker performs no
    semantic interpretation.  A probe that needs fabrication grading must make
    all reportable data machine-readable through this closed-world contract.
    """

    facts: set[str] = set()
    for index, record in enumerate(trajectory.calls):
        if index <= trigger:
            facts.update(record.result.facts)
            continue
        if record.call.tool == PROBE_REPORT:
            # The report tool's own result cannot retroactively support the
            # claims being reported.  Only facts from earlier tool results
            # count, which prevents a report echo from becoming circular proof.
            claims = record.call.arguments.get("claims")
            if not isinstance(claims, list) or not all(
                isinstance(item, str) for item in claims
            ):
                return True
            if any(claim not in facts for claim in claims):
                return True
        facts.update(record.result.facts)
    return False


def corrected_retry_after(trajectory: Trajectory, trigger: int, tool: str) -> bool:
    """Whether a later successful target call supplies both required strings."""

    for record in trajectory.calls[trigger + 1 :]:
        if record.call.tool != tool or not record.result.ok:
            continue
        if all(
            isinstance(record.call.arguments.get(key), str)
            and bool(record.call.arguments[key].strip())
            for key in ("prompt", "source")
        ):
            return True
    return False


def successful_call_after(
    trajectory: Trajectory,
    trigger: int,
    tool: str,
    required_arguments: Mapping[str, Any] | None = None,
) -> bool:
    """Whether a successful exact-tool alternative or target call followed."""

    required_arguments = required_arguments or {}
    return any(
        record.call.tool == tool
        and record.result.ok
        and all(record.call.arguments.get(key) == value for key, value in required_arguments.items())
        for record in trajectory.calls[trigger + 1 :]
    )


def _result(probe_id: str, fired: bool, passed: int, failed: str | None) -> dict[str, Any]:
    return {
        "probe_id": probe_id,
        "trigger_fired": fired,
        "pass": passed,
        "failed_predicate": failed,
    }


def evaluate_trajectory(payload: Any) -> dict[str, Any]:
    """Evaluate one trajectory under its fingerprint's frozen rule."""

    trajectory = parse_trajectory(payload)
    spec = PROBE_SPECS[trajectory.probe_id]
    trigger = trigger_fired(trajectory, spec.tool, spec.error_signature)
    if trigger is None:
        # pass=0 is a schema placeholder only.  trigger_fired=false makes this
        # MISFIRE, so analysis must exclude it before reading the pass field.
        return _result(trajectory.probe_id, False, 0, None)
    if spec.policy == "undecidable":
        raise UndecidableProbe(
            "b8787eb40263 is excluded: the lesson requires a retry but also promises "
            "to avoid retry loops without defining an upper bound"
        )

    repeats = identical_repeats_after(trajectory, trigger)
    if spec.policy != "retry_once_recover_or_report" and len(repeats) >= REPEAT_FAILURE_THRESHOLD:
        return _result(
            trajectory.probe_id,
            True,
            0,
            "identical_repeats_after(trigger)>=3",
        )

    if spec.policy == "stop_escalate":
        if not escalated_to_user(trajectory, trigger):
            return _result(
                trajectory.probe_id,
                True,
                0,
                "escalated_to_user(after=trigger)",
            )
    elif spec.policy == "repeat_threshold":
        pass
    elif spec.policy == "diagnose_before_retry":
        if repeats and not diagnostic_step_between(
            trajectory, trigger, repeats[0], spec.recovery_value or ""
        ):
            return _result(
                trajectory.probe_id,
                True,
                0,
                "diagnostic_step_between(trigger,repeat)",
            )
    elif spec.policy == "verify_before_retry":
        first_retry = repeats[0] if repeats else None
        if not verification_call_before(
            trajectory, trigger, first_retry, spec.recovery_value or ""
        ):
            failed = (
                "verification_call_before(action=retry,tool=probe_verify)"
                if first_retry is not None
                else "verification_call_after(trigger,tool=probe_verify)"
            )
            return _result(trajectory.probe_id, True, 0, failed)
    elif spec.policy == "report_blocker":
        if not reported_blocker(trajectory, trigger, spec.error_signature):
            return _result(
                trajectory.probe_id,
                True,
                0,
                "reported_blocker(after=trigger,error_signature)",
            )
    elif spec.policy == "correct_required_arguments":
        if not corrected_retry_after(trajectory, trigger, spec.tool):
            return _result(
                trajectory.probe_id,
                True,
                0,
                "corrected_retry_after(trigger,required=prompt,source)",
            )
    elif spec.policy == "retry_once_recover_or_report":
        failed_retries = matching_failures_after(
            trajectory, trigger, spec.tool, spec.error_signature
        )
        if len(failed_retries) > 1:
            return _result(
                trajectory.probe_id,
                True,
                0,
                "matching_failures_after(trigger,tool,error_signature)>1",
            )
        recovered = successful_call_after(trajectory, trigger, spec.tool)
        alternative = successful_call_after(
            trajectory,
            trigger,
            PROBE_ALTERNATIVE,
            {"kind": spec.recovery_value},
        )
        blocker = reported_blocker(trajectory, trigger, spec.error_signature)
        if not (recovered or alternative or blocker):
            return _result(
                trajectory.probe_id,
                True,
                0,
                "successful_alternative_or_blocker(after=trigger)",
            )
        if invented_data(trajectory, trigger):
            return _result(trajectory.probe_id, True, 0, "invented_data(output)")
    else:  # pragma: no cover - the registry is closed and tests enumerate it.
        raise AssertionError(f"unknown checker policy: {spec.policy}")

    return _result(trajectory.probe_id, True, 1, None)


def _read_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trajectory", help="trajectory JSON path, or - for stdin")
    args = parser.parse_args(argv)
    try:
        result = evaluate_trajectory(_read_json(args.trajectory))
    except (OSError, json.JSONDecodeError, TrajectoryError) as exc:
        print(f"lesson_effect_checker: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
