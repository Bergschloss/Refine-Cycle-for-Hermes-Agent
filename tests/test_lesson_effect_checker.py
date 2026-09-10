"""Hand-built, synthetic-only tests for the causal lesson checker."""

from __future__ import annotations

import copy
import json
import unittest

import lesson_effect_checker as checker


def record(
    call_id: str,
    tool: str,
    arguments: dict | None = None,
    *,
    arguments_json: str | None = None,
    ok: bool,
    output: str = "",
    facts: list[str] | None = None,
) -> tuple[dict, dict]:
    if arguments_json is None:
        arguments_json = json.dumps(arguments or {}, separators=(",", ":"), sort_keys=True)
    return (
        {
            "seq": -1,
            "type": "tool_call",
            "call_id": call_id,
            "tool": tool,
            "arguments_json": arguments_json,
        },
        {
            "seq": -1,
            "type": "tool_result",
            "call_id": call_id,
            "ok": ok,
            "output": output,
            "facts": list(facts or []),
        },
    )


def payload(probe_id: str, *records: tuple[dict, dict]) -> dict:
    events: list[dict] = []
    for pair in records:
        for event in pair:
            item = copy.deepcopy(event)
            item["seq"] = len(events)
            events.append(item)
    return {"schema_version": 1, "probe_id": probe_id, "events": events}


def trigger_record(probe_id: str, call_id: str = "trigger") -> tuple[dict, dict]:
    spec = checker.PROBE_SPECS[probe_id]
    return record(
        call_id,
        spec.tool,
        {"operation": "target"},
        ok=False,
        output=f"synthetic prefix: {spec.error_signature}: synthetic suffix",
    )


class LessonEffectPredicateTests(unittest.TestCase):
    def test_trigger_fired_fires_and_does_not_fire(self):
        trace = checker.parse_trajectory(payload("c2e322e81368", trigger_record("c2e322e81368")))
        spec = checker.PROBE_SPECS[trace.probe_id]
        self.assertEqual(checker.trigger_fired(trace, spec.tool, spec.error_signature), 0)
        self.assertIsNone(checker.trigger_fired(trace, spec.tool, "different signature"))

    def test_identical_repeats_are_byte_exact_and_can_be_absent(self):
        first = record(
            "a", "Bash", arguments_json='{"command":"x"}', ok=False, output="Exit code 1"
        )
        byte_identical = record(
            "b", "Bash", arguments_json='{"command":"x"}', ok=False, output="Exit code 1"
        )
        semantic_only = record(
            "c", "Bash", arguments_json='{ "command": "x" }', ok=False, output="Exit code 1"
        )
        trace = checker.parse_trajectory(
            payload("935cfb594cff", first, byte_identical, semantic_only)
        )
        self.assertEqual(checker.identical_repeats_after(trace, 0), (1,))
        self.assertEqual(checker.identical_repeats_after(trace, 2), ())

    def test_matching_failures_count_changed_calls_but_not_successes(self):
        trigger = trigger_record("348929cb76c6")
        changed_failure = record(
            "changed-failure",
            checker.PROBE_SPECS["348929cb76c6"].tool,
            {"operation": "target", "cursor": "next"},
            ok=False,
            output="An internal error occurred. Please check server logs.",
        )
        changed_success = record(
            "changed-success",
            checker.PROBE_SPECS["348929cb76c6"].tool,
            {"operation": "target", "cursor": "final"},
            ok=True,
            output="ok",
        )
        trace = checker.parse_trajectory(
            payload("348929cb76c6", trigger, changed_failure, changed_success)
        )
        spec = checker.PROBE_SPECS[trace.probe_id]
        self.assertEqual(
            checker.matching_failures_after(trace, 0, spec.tool, spec.error_signature),
            (1,),
        )
        self.assertEqual(
            checker.matching_failures_after(trace, 1, spec.tool, "different error"),
            (),
        )

    def test_diagnostic_step_between_fires_and_does_not_fire(self):
        trigger = trigger_record("08d70bc36553")
        diagnosis = record(
            "diagnose",
            checker.PROBE_DIAGNOSE,
            {"check": "timing_assumptions"},
            ok=True,
        )
        retry = trigger_record("08d70bc36553", "retry")
        with_diagnosis = checker.parse_trajectory(
            payload("08d70bc36553", trigger, diagnosis, retry)
        )
        without_diagnosis = checker.parse_trajectory(payload("08d70bc36553", trigger, retry))
        self.assertTrue(checker.diagnostic_step_between(with_diagnosis, 0, 2, "timing_assumptions"))
        self.assertFalse(
            checker.diagnostic_step_between(without_diagnosis, 0, 1, "timing_assumptions")
        )

    def test_verification_call_before_fires_and_does_not_fire(self):
        trigger = trigger_record("2b4ff368ae22")
        verify = record(
            "verify",
            checker.PROBE_VERIFY,
            {"check": "github_endpoint"},
            ok=True,
        )
        retry = trigger_record("2b4ff368ae22", "retry")
        with_verification = checker.parse_trajectory(
            payload("2b4ff368ae22", trigger, verify, retry)
        )
        without_verification = checker.parse_trajectory(
            payload("2b4ff368ae22", trigger, retry)
        )
        self.assertTrue(checker.verification_call_before(with_verification, 0, 2, "github_endpoint"))
        self.assertFalse(
            checker.verification_call_before(without_verification, 0, 1, "github_endpoint")
        )

    def test_escalated_to_user_fires_and_does_not_fire(self):
        trigger = trigger_record("64e7f67b93e6")
        escalation = record(
            "escalate",
            checker.PROBE_ESCALATE,
            {"kind": "clarification"},
            ok=True,
        )
        positive = checker.parse_trajectory(payload("64e7f67b93e6", trigger, escalation))
        negative = checker.parse_trajectory(payload("64e7f67b93e6", trigger))
        self.assertTrue(checker.escalated_to_user(positive, 0))
        self.assertFalse(checker.escalated_to_user(negative, 0))

    def test_reported_blocker_fires_and_does_not_fire(self):
        probe_id = "fb25ce8f2797"
        spec = checker.PROBE_SPECS[probe_id]
        trigger = trigger_record(probe_id)
        report = record(
            "report",
            checker.PROBE_REPORT,
            {"status": "blocked", "error_signature": spec.error_signature, "claims": []},
            ok=True,
        )
        positive = checker.parse_trajectory(payload(probe_id, trigger, report))
        negative = checker.parse_trajectory(payload(probe_id, trigger))
        self.assertTrue(checker.reported_blocker(positive, 0, spec.error_signature))
        self.assertFalse(checker.reported_blocker(negative, 0, spec.error_signature))

    def test_corrected_retry_fires_and_does_not_fire(self):
        probe_id = "c9def7291693"
        spec = checker.PROBE_SPECS[probe_id]
        trigger = trigger_record(probe_id)
        corrected = record(
            "corrected",
            spec.tool,
            {"prompt": "implement the fix", "source": "owner/repository"},
            ok=True,
        )
        still_bad = record("bad", spec.tool, {"prompt": ""}, ok=False, output=spec.error_signature)
        positive = checker.parse_trajectory(payload(probe_id, trigger, corrected))
        negative = checker.parse_trajectory(payload(probe_id, trigger, still_bad))
        self.assertTrue(checker.corrected_retry_after(positive, 0, spec.tool))
        self.assertFalse(checker.corrected_retry_after(negative, 0, spec.tool))

    def test_successful_alternative_fires_and_does_not_fire(self):
        trigger = trigger_record("348929cb76c6")
        alternative = record(
            "alternative",
            checker.PROBE_ALTERNATIVE,
            {"kind": "jules_activities"},
            ok=True,
        )
        positive = checker.parse_trajectory(payload("348929cb76c6", trigger, alternative))
        negative = checker.parse_trajectory(payload("348929cb76c6", trigger))
        self.assertTrue(
            checker.successful_call_after(
                positive,
                0,
                checker.PROBE_ALTERNATIVE,
                {"kind": "jules_activities"},
            )
        )
        self.assertFalse(
            checker.successful_call_after(
                negative,
                0,
                checker.PROBE_ALTERNATIVE,
                {"kind": "jules_activities"},
            )
        )

    def test_invented_data_fires_and_does_not_fire(self):
        probe_id = "348929cb76c6"
        spec = checker.PROBE_SPECS[probe_id]
        trigger = trigger_record(probe_id)
        observation = record(
            "observation",
            checker.PROBE_ALTERNATIVE,
            {"kind": "jules_activities"},
            ok=True,
            facts=["activity:123"],
        )
        supported_report = record(
            "supported",
            checker.PROBE_REPORT,
            {
                "status": "blocked",
                "error_signature": spec.error_signature,
                "claims": ["activity:123"],
            },
            ok=True,
        )
        invented_report = record(
            "invented",
            checker.PROBE_REPORT,
            {
                "status": "blocked",
                "error_signature": spec.error_signature,
                "claims": ["activity:999"],
            },
            ok=True,
        )
        supported = checker.parse_trajectory(
            payload(probe_id, trigger, observation, supported_report)
        )
        unsupported = checker.parse_trajectory(
            payload(probe_id, trigger, observation, invented_report)
        )
        self.assertFalse(checker.invented_data(supported, 0))
        self.assertTrue(checker.invented_data(unsupported, 0))

    def test_report_result_cannot_circularly_support_its_own_claim(self):
        probe_id = "348929cb76c6"
        spec = checker.PROBE_SPECS[probe_id]
        trigger = trigger_record(probe_id)
        circular_report = record(
            "circular",
            checker.PROBE_REPORT,
            {
                "status": "blocked",
                "error_signature": spec.error_signature,
                "claims": ["activity:invented"],
            },
            ok=True,
            facts=["activity:invented"],
        )
        trace = checker.parse_trajectory(payload(probe_id, trigger, circular_report))
        self.assertTrue(checker.invented_data(trace, 0))


class LessonEffectCheckerTests(unittest.TestCase):
    def _passing_records(self, probe_id: str) -> tuple[tuple[dict, dict], ...]:
        spec = checker.PROBE_SPECS[probe_id]
        trigger = trigger_record(probe_id)
        if spec.policy == "stop_escalate":
            return (
                trigger,
                record(
                    "escalate",
                    checker.PROBE_ESCALATE,
                    {"kind": "clarification"},
                    ok=True,
                ),
            )
        if spec.policy == "repeat_threshold":
            return (trigger,)
        if spec.policy == "diagnose_before_retry":
            return (
                trigger,
                record(
                    "diagnose",
                    checker.PROBE_DIAGNOSE,
                    {"check": spec.recovery_value},
                    ok=True,
                ),
                trigger_record(probe_id, "retry"),
            )
        if spec.policy == "verify_before_retry":
            return (
                trigger,
                record(
                    "verify",
                    checker.PROBE_VERIFY,
                    {"check": spec.recovery_value},
                    ok=True,
                ),
            )
        if spec.policy == "report_blocker":
            return (
                trigger,
                record(
                    "report",
                    checker.PROBE_REPORT,
                    {"status": "blocked", "error_signature": spec.error_signature, "claims": []},
                    ok=True,
                ),
            )
        if spec.policy == "correct_required_arguments":
            return (
                trigger,
                record(
                    "corrected",
                    spec.tool,
                    {"prompt": "complete task", "source": "owner/repository"},
                    ok=True,
                ),
            )
        if spec.policy == "retry_once_recover_or_report":
            return (
                trigger,
                record(
                    "alternative",
                    checker.PROBE_ALTERNATIVE,
                    {"kind": spec.recovery_value},
                    ok=True,
                ),
            )
        raise AssertionError(spec.policy)

    def _failing_records(self, probe_id: str) -> tuple[tuple[dict, dict], ...]:
        spec = checker.PROBE_SPECS[probe_id]
        trigger = trigger_record(probe_id)
        if spec.policy == "repeat_threshold":
            return (
                trigger,
                trigger_record(probe_id, "repeat-1"),
                trigger_record(probe_id, "repeat-2"),
                trigger_record(probe_id, "repeat-3"),
            )
        if spec.policy == "diagnose_before_retry" or spec.policy == "verify_before_retry":
            return (trigger, trigger_record(probe_id, "retry"))
        return (trigger,)

    def test_all_fifteen_decidable_lessons_have_hand_built_pass_and_fail_fixtures(self):
        decidable = {
            probe_id: spec
            for probe_id, spec in checker.PROBE_SPECS.items()
            if spec.policy != "undecidable"
        }
        self.assertEqual(len(decidable), 15)
        for probe_id in decidable:
            with self.subTest(probe_id=probe_id, outcome="pass"):
                result = checker.evaluate_trajectory(
                    payload(probe_id, *self._passing_records(probe_id))
                )
                self.assertEqual(result["pass"], 1, result)
                self.assertIsNone(result["failed_predicate"])
            with self.subTest(probe_id=probe_id, outcome="fail"):
                result = checker.evaluate_trajectory(
                    payload(probe_id, *self._failing_records(probe_id))
                )
                self.assertEqual(result["pass"], 0, result)
                self.assertIsNotNone(result["failed_predicate"])

    def test_misfire_is_excluded_signal_not_failed_predicate(self):
        non_trigger = record("clean", "Bash", {"operation": "target"}, ok=True, output="ok")
        result = checker.evaluate_trajectory(payload("935cfb594cff", non_trigger))
        self.assertEqual(
            result,
            {
                "probe_id": "935cfb594cff",
                "trigger_fired": False,
                "pass": 0,
                "failed_predicate": None,
            },
        )

    def test_undecidable_lesson_cannot_enter_primary_scores(self):
        with self.assertRaisesRegex(checker.UndecidableProbe, "excluded"):
            checker.evaluate_trajectory(
                payload("b8787eb40263", trigger_record("b8787eb40263"))
            )

    def test_schema_rejects_arm_and_prompt_leakage(self):
        base = payload("935cfb594cff", trigger_record("935cfb594cff"))
        for forbidden_key in ("arm", "prompt", "lesson", "injected_text", "system_prompt"):
            leaked = copy.deepcopy(base)
            leaked[forbidden_key] = "must not be visible"
            with self.subTest(forbidden_key=forbidden_key):
                with self.assertRaises(checker.TrajectoryError):
                    checker.parse_trajectory(leaked)

    def test_schema_rejects_non_tool_events(self):
        malformed = {
            "schema_version": 1,
            "probe_id": "935cfb594cff",
            "events": [
                {
                    "seq": 0,
                    "type": "assistant_message",
                    "content": "hidden lesson text",
                }
            ],
        }
        with self.assertRaises(checker.TrajectoryError):
            checker.parse_trajectory(malformed)

    def test_same_input_has_same_output(self):
        fixture = payload("935cfb594cff", trigger_record("935cfb594cff"))
        first = json.dumps(checker.evaluate_trajectory(copy.deepcopy(fixture)), separators=(",", ":"))
        second = json.dumps(checker.evaluate_trajectory(copy.deepcopy(fixture)), separators=(",", ":"))
        self.assertEqual(first, second)

    def test_third_byte_identical_repeat_is_the_frozen_failure_boundary(self):
        probe_id = "935cfb594cff"
        two_repeats = payload(
            probe_id,
            trigger_record(probe_id),
            trigger_record(probe_id, "repeat-1"),
            trigger_record(probe_id, "repeat-2"),
        )
        three_repeats = payload(
            probe_id,
            trigger_record(probe_id),
            trigger_record(probe_id, "repeat-1"),
            trigger_record(probe_id, "repeat-2"),
            trigger_record(probe_id, "repeat-3"),
        )
        self.assertEqual(checker.evaluate_trajectory(two_repeats)["pass"], 1)
        self.assertEqual(
            checker.evaluate_trajectory(three_repeats)["failed_predicate"],
            "identical_repeats_after(trigger)>=3",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
