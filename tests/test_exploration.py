"""Regression tests for Issue-authorized method exploration."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.competition_automation.exploration import (
    ExplorationError,
    bottleneck_analysis,
    evaluate_exploration,
)


class ExplorationTests(unittest.TestCase):
    """Ensure exploration remains finite, attributable, and model-independent."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "issue.json").write_text('{"number": 7}\n', encoding="utf-8")
        self.issue_hash = hashlib.sha256(
            (self.root / "issue.json").read_bytes()
        ).hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _plan(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source_issue": {
                "number": 7,
                "snapshot": {"path": "issue.json", "sha256": self.issue_hash},
            },
            "capabilities": ["ml", "rl"],
            "objective": {
                "metric": "mrr",
                "direction": "max",
                "mechanism": ["retrieval"],
            },
            "authorized_axes": [
                {
                    "id": "retrieval",
                    "kind": "mechanism",
                    "budget": 2,
                    "families": ["a"],
                    "status": "untested",
                    "objective_link": "candidate coverage",
                },
                {
                    "id": "cosmetic",
                    "kind": "cosmetic",
                    "budget": 1,
                    "families": [],
                    "status": "exhausted",
                },
            ],
            "families": [
                {"id": "a", "axis": "retrieval", "budget": 1, "observed_attempts": 0}
            ],
            "restrictions": [],
            "rl": None,
            "bottleneck": {"candidate_recall": 0.2, "conditional_rank_quality": 0.8},
        }

    def test_uncovered_and_exhausted_axes_are_reported(self) -> None:
        result = evaluate_exploration(self._plan(), self.root)
        self.assertIn("retrieval", result["uncovered_axes"])
        self.assertIn("cosmetic", result["exhausted_axes"])
        self.assertIsNone(result["bottleneck"]["dominant"])
        self.assertIsNone(result["bottleneck"]["overall_metric"])

    def test_rl_requires_state_action_reward_evaluation_budget_and_baseline(
        self,
    ) -> None:
        plan = self._plan()
        plan["rl"] = {
            "state": "sample",
            "action": "select",
            "reward": "metric",
            "evaluation": "heldout",
            "budget": 2,
            "cheap_baseline": "greedy",
        }
        self.assertTrue(evaluate_exploration(plan, self.root)["rl"]["eligible"])
        plan["rl"].pop("cheap_baseline")  # type: ignore[union-attr]
        self.assertFalse(evaluate_exploration(plan, self.root)["rl"]["eligible"])

    def test_expired_restriction_requires_review_without_extension(self) -> None:
        plan = self._plan()
        plan["restrictions"] = [
            {
                "owner": "user",
                "reason": "no third family",
                "review_at": "2026-01-01T00:00:00Z",
            }
        ]
        result = evaluate_exploration(
            plan, self.root, now=datetime(2026, 10, 15, tzinfo=timezone.utc)
        )
        self.assertEqual(result["status"], "review_needed")
        self.assertTrue(result["restrictions"]["review_needed"])
        self.assertFalse(result["restrictions"]["automatic_extension"])

    def test_issue_hash_mismatch_fails_closed(self) -> None:
        plan = self._plan()
        plan["source_issue"]["snapshot"]["sha256"] = "0" * 64  # type: ignore[index]
        with self.assertRaisesRegex(ExplorationError, "hash mismatch"):
            evaluate_exploration(plan, self.root)

    def test_issue_number_mismatch_fails_closed(self) -> None:
        plan = self._plan()
        (self.root / "issue.json").write_text('{"number": 8}\n', encoding="utf-8")
        plan["source_issue"]["snapshot"]["sha256"] = hashlib.sha256(
            (self.root / "issue.json").read_bytes()
        ).hexdigest()  # type: ignore[index]
        with self.assertRaisesRegex(ExplorationError, "number mismatch"):
            evaluate_exploration(plan, self.root)

    def test_axis_must_link_mechanism_to_objective(self) -> None:
        plan = self._plan()
        plan["authorized_axes"][0].pop("objective_link")  # type: ignore[index]
        plan["objective"]["mechanism"] = ["other-mechanism"]  # type: ignore[index]
        with self.assertRaisesRegex(ExplorationError, "objective_link"):
            evaluate_exploration(plan, self.root)

    def test_bottleneck_does_not_infer_official_metric(self) -> None:
        result = bottleneck_analysis(0.9, 0.9)
        self.assertIsNone(result["overall_metric"])
        self.assertEqual(result["overall_metric_inference"], "not_performed")

    def test_family_budget_and_observed_attempts_are_bounded(self) -> None:
        plan = self._plan()
        plan["families"][0]["budget"] = 3  # type: ignore[index]
        with self.assertRaisesRegex(ExplorationError, "exceeds axis budget"):
            evaluate_exploration(plan, self.root)
        plan = self._plan()
        plan["families"][0]["observed_attempts"] = 2  # type: ignore[index]
        with self.assertRaisesRegex(ExplorationError, "exceed family budget"):
            evaluate_exploration(plan, self.root)

    def test_naive_review_time_is_rejected(self) -> None:
        plan = self._plan()
        plan["restrictions"] = [
            {"owner": "user", "reason": "wait", "review_at": "2026-01-01T00:00:00"}
        ]
        with self.assertRaisesRegex(ExplorationError, "timezone"):
            evaluate_exploration(
                plan, self.root, now=datetime(2026, 10, 15, tzinfo=timezone.utc)
            )

    def test_unknown_plan_keys_are_rejected(self) -> None:
        plan = self._plan()
        plan["extra"] = True
        with self.assertRaisesRegex(ExplorationError, "unknown keys"):
            evaluate_exploration(plan, self.root)


if __name__ == "__main__":
    unittest.main()
