"""Tests in both directions for the fallback-parser fix (repo test file)."""
import sys
import unittest

sys.path.insert(0, "/home/ubuntu/.hermes/plugins/refine")

import __init__ as P


class FallbackRuleParserTests(unittest.TestCase):
    # ACCEPT: explicit reroute and param notes still produce structured rules
    def test_reroute_note_produces_block_rule(self):
        rule = P._parse_prompt_note_rule(
            "When the build fails with a stale cache, use ccache instead of make."
        )
        self.assertIsNotNone(rule)
        self.assertIn(rule["type"], ("block_binary", "block_tool"))
        self.assertEqual(rule["target"], "make")
        self.assertIn("ccache", rule["action"])

    def test_param_note_produces_require_fields(self):
        rule = P._parse_prompt_note_rule(
            "When calling the maps tool, always include both 'origin' and "
            "'destination' fields."
        )
        self.assertIsNotNone(rule)
        self.assertEqual(rule["type"], "require_fields")
        self.assertEqual(rule["fields"], ["origin", "destination"])

    # REFUSE: condition prose must never synthesize a block target
    def test_prose_note_yields_no_rule(self):
        self.assertIsNone(
            P._parse_prompt_note_rule(
                "When a command is not recognized, check the error before acting."
            )
        )

    def test_exit_code_note_cannot_block_code_cli(self):
        # The real regression: 'exit code 127' prose synthesized
        # block_binary target='code', blocking the VS Code CLI.
        self.assertIsNone(
            P._parse_prompt_note_rule(
                "When terminal returns exit code 127, use a different approach instead."
            )
        )
        P._BLOCK_RULES = []
        res = P._on_pre_tool_call(
            tool_name="terminal", args={"command": "code --install-extension foo"},
            session_id="sid-test",
        )
        self.assertIsNone(res)

    def test_update_block_rules_skips_prose_notes(self):
        P._update_block_rules([
            {"content": "When the LLM is unavailable, mention the limitation plainly."},
            {"content": "When refine_run reports a session does not exist, ask for clarification."},
        ])
        self.assertEqual(P._BLOCK_RULES, [])


if __name__ == "__main__":
    unittest.main()
