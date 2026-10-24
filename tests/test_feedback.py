"""Tests for task-configured comparison of official feedback."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tools.competition_automation.feedback import FeedbackError, compare_official_feedback
from tools.competition_automation.fingerprint import fingerprint_path


class FeedbackTests(unittest.TestCase):
    """Check both fixed and changing evaluation populations."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("before.json", "after.json", "first.zip", "second.zip"):
            (self.root / name).write_text(name + "\n", encoding="utf-8")
        self.plan = {
            "schema_version": 2,
            "direction": "max",
            "requirements": {
                "minimum_observations": 50,
                "max_abs_recent_drift": 1.0,
                "max_cohort_distance": 0.1,
            },
            "before": self._observation("before.json", "1", {"high": 64, "low": 24}),
            "after": self._observation("after.json", "2", {"high": 64, "low": 24}),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _observation(
        self, path: str, submission_id: str, cohorts: dict[str, int]
    ) -> dict[str, object]:
        """Create one source-hash-bound normalized observation."""
        artifact = "first.zip" if submission_id == "1" else "second.zip"
        return {
            "observation_id": submission_id,
            "evaluation_id": "same-official-evaluation-version",
            "score": 2000.0 if submission_id == "1" else 2100.0,
            "observed_at": "2026-09-30T00:00:00Z",
            "n_observations": sum(cohorts.values()),
            "cohort_counts": cohorts,
            "recent_drift_per_observation": 0.5,
            "artifact": {
                "path": artifact,
                "sha256": fingerprint_path(self.root / artifact)["sha256"],
            },
            "source": {"path": path, "sha256": fingerprint_path(self.root / path)["sha256"]},
        }

    def test_comparable_observations_report_improvement(self) -> None:
        """A score delta is interpretable only after all comparison gates pass."""
        result = compare_official_feedback(self.plan, self.root)
        self.assertEqual(result["status"], "COMPARABLE")
        self.assertTrue(result["after_is_better"])
        self.assertFalse(result["same_artifact"])

    def test_fixed_cohort_does_not_require_dynamic_rating_fields(self) -> None:
        """Ordinary fixed-split tasks can compare official scores without game counts."""
        plan = deepcopy(self.plan)
        plan["requirements"] = {
            "minimum_observations": None,
            "max_abs_recent_drift": None,
            "max_cohort_distance": None,
        }
        for name in ("before", "after"):
            plan[name]["n_observations"] = None
            plan[name]["cohort_counts"] = None
            plan[name]["recent_drift_per_observation"] = None
        result = compare_official_feedback(plan, self.root)
        self.assertEqual(result["status"], "COMPARABLE")
        self.assertIsNone(result["cohort_distance"])

    def test_different_evaluation_versions_are_incomparable(self) -> None:
        """Scores from changed tasks or public sets cannot be subtracted."""
        plan = deepcopy(self.plan)
        plan["after"]["evaluation_id"] = "new-official-evaluation-version"
        result = compare_official_feedback(plan, self.root)
        self.assertIn("evaluation_mismatch", result["reasons"])
        self.assertIsNone(result["score_delta"])

    def test_lower_is_better_metric_is_not_ranking_specific(self) -> None:
        """The comparator also supports loss metrics with a fixed evaluation."""
        plan = deepcopy(self.plan)
        plan["direction"] = "min"
        plan["after"]["score"] = 1900.0
        self.assertTrue(compare_official_feedback(plan, self.root)["after_is_better"])

    def test_missing_optional_evidence_blocks_when_gate_enabled(self) -> None:
        """An enabled check fails closed rather than silently disabling itself."""
        plan = deepcopy(self.plan)
        plan["after"]["cohort_counts"] = None
        self.assertIn(
            "cohort_evidence_missing",
            compare_official_feedback(plan, self.root)["reasons"],
        )

    def test_mismatched_and_immature_scores_have_no_delta(self) -> None:
        """The Kaggriculture-style reupload gap is not causal evidence."""
        plan = deepcopy(self.plan)
        plan["after"] = self._observation("after.json", "2", {"low": 20})
        plan["after"]["recent_drift_per_observation"] = 57.5
        result = compare_official_feedback(plan, self.root)
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(result["score_delta"])
        self.assertIn("cohort_mismatch", result["reasons"])
        self.assertIn("after_not_mature", result["reasons"])

    def test_mutated_source_is_rejected(self) -> None:
        """A stale snapshot digest cannot support the comparison."""
        (self.root / "before.json").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(FeedbackError, "hash mismatch"):
            compare_official_feedback(self.plan, self.root)


if __name__ == "__main__":
    unittest.main()
