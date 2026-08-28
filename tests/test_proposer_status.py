"""Tests for the proposer-status fields — config mock must only stub the two
functions the code path reads; everything else must be the real config."""
import sys
import unittest
from unittest import mock

sys.path.insert(0, "/home/ubuntu/.hermes/plugins/refine")

import core
import config as real_config


class ProposerStatusTests(unittest.TestCase):
    def _status_with(self, enabled, lifecycle):
        with mock.patch.object(core, "_subagent_lifecycle", return_value=None if enabled is None else (object() if enabled else None)), \
             mock.patch.object(core.config, "proposer_subagent_enabled", return_value=bool(enabled and enabled is not None)):
            return core.refine_status()

    def test_subagent_when_config_enabled_and_lifecycle_bound(self):
        with mock.patch.object(core.config, "proposer_subagent_enabled", return_value=True), \
             mock.patch.object(core, "_subagent_lifecycle", return_value=object()):
            s = core.refine_status()
        self.assertEqual(s["proposer"]["effective"], "subagent")
        self.assertTrue(s["proposer"]["subagent_config_enabled"])
        self.assertTrue(s["proposer"]["subagent_lifecycle_bound"])

    def test_structured_when_config_disabled_even_if_bound(self):
        with mock.patch.object(core.config, "proposer_subagent_enabled", return_value=False), \
             mock.patch.object(core, "_subagent_lifecycle", return_value=object()):
            s = core.refine_status()
        self.assertEqual(s["proposer"]["effective"], "structured")
        self.assertFalse(s["proposer"]["subagent_config_enabled"])
        self.assertTrue(s["proposer"]["subagent_lifecycle_bound"])

    def test_structured_when_lifecycle_not_bound_even_if_enabled(self):
        with mock.patch.object(core.config, "proposer_subagent_enabled", return_value=True), \
             mock.patch.object(core, "_subagent_lifecycle", return_value=None):
            s = core.refine_status()
        self.assertEqual(s["proposer"]["effective"], "structured")
        self.assertFalse(s["proposer"]["subagent_lifecycle_bound"])


if __name__ == "__main__":
    unittest.main()
