"""Regression tests for strict framework-neutral multi-fidelity accounting."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.competition_automation.fidelity import (
    FidelityError,
    create_checkpoint_manifest,
    evaluate_fidelity,
    verify_checkpoint_manifest,
)
from tools.competition_automation.fingerprint import canonical_json_digest


class FidelityTests(unittest.TestCase):
    """Ensure partial computation cannot masquerade as negative evidence."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name, text in (("model.bin", "model"), ("native.bin", "native")):
            (self.root / name).write_text(text, encoding="utf-8")
        self._write_metric("baseline.json", "short", 20, 0.5)
        self._write_metric("candidate.json", "full", 100, 0.6)
        self.computation_payload = {
            "code": {"commit": "a"},
            "data": {"version": "b"},
            "split": {"version": "c"},
            "config": {"epochs": 1},
        }
        self.lineage = {
            **{
                f"{key}_sha256": canonical_json_digest(value)
                for key, value in self.computation_payload.items()
            },
            "objective": {"metric": "score", "direction": "max", "min_delta": 0.01},
        }
        self.manifest = create_checkpoint_manifest(
            self.root,
            "checkpoint.json",
            [
                {"component": "model", "path": "model.bin"},
                {"component": "native_state", "path": "native.bin"},
            ],
            capability="ml",
            required_components=["model", "native_state"],
            lineage=self.lineage,
            resume_mode="weights_only",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _snapshot(self, path: str, value: float) -> dict[str, object]:
        stage_name, budget = ("short", 20) if path == "baseline.json" else ("full", 100)
        return {
            "metric": "score",
            "value": value,
            "cohort_id": "c1",
            "status": "completed",
            "stage_name": stage_name,
            "budget": budget,
            "path": path,
            "sha256": hashlib.sha256((self.root / path).read_bytes()).hexdigest(),
        }

    def _write_metric(
        self, path: str, stage_name: str, budget: int, value: float
    ) -> None:
        (self.root / path).write_text(
            json.dumps(
                {
                    "metric": "score",
                    "value": value,
                    "cohort_id": "c1",
                    "status": "completed",
                    "stage_name": stage_name,
                    "budget": budget,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _plan(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "capability": "ml",
            "cohort_id": "c1",
            "unit": "steps",
            "total_budget": 100,
            "stages": [
                {
                    "name": "short",
                    "budget": 20,
                    "status": "completed",
                    "cohort_id": "c1",
                    "metric_snapshot": self._snapshot("baseline.json", 0.5),
                },
                {
                    "name": "full",
                    "budget": 100,
                    "status": "completed",
                    "cohort_id": "c1",
                    "metric_snapshot": self._snapshot("candidate.json", 0.6),
                },
            ],
            "observed": {
                "speed": None,
                "speed_unit": "steps_per_second",
                "completed": 0,
            },
            "computation_payload": self.computation_payload,
            "lineage": self.lineage,
            "objective": {"metric": "score", "direction": "max", "min_delta": 0.01},
            "checkpoint": {
                "resume_mode": "weights_only",
                "manifest_path": self.manifest["manifest_path"],
                "manifest_sha256": self.manifest["manifest_sha256"],
                "required_components": ["model", "native_state"],
            },
            "approved_issue_axes": ["representation"],
            "partial_artifacts": [],
        }

    def test_budget_exhausted_is_inconclusive(self) -> None:
        plan = self._plan()
        plan["stages"][1]["status"] = "budget_exhausted"  # type: ignore[index]
        plan["stages"][1].pop("metric_snapshot")  # type: ignore[index]
        result = evaluate_fidelity(plan, self.root)
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["hypothesis_result"], "inconclusive")

    def test_start_partial_resume_and_metric_promotion_flow(self) -> None:
        plan = self._plan()
        plan["checkpoint"] = None
        plan["stages"][0]["status"] = "running"  # type: ignore[index]
        plan["stages"][0].pop("metric_snapshot")  # type: ignore[index]
        plan["stages"][1]["status"] = "planned"  # type: ignore[index]
        plan["stages"][1].pop("metric_snapshot")  # type: ignore[index]
        self.assertEqual(
            evaluate_fidelity(plan, self.root)["checkpoint"]["status"], "not_requested"
        )
        plan["stages"][0]["status"] = "budget_exhausted"  # type: ignore[index]
        self.assertEqual(evaluate_fidelity(plan, self.root)["status"], "inconclusive")
        plan["stages"][0]["status"] = "completed"  # type: ignore[index]
        plan["stages"][0]["metric_snapshot"] = self._snapshot("baseline.json", 0.5)  # type: ignore[index]
        plan["stages"][1]["status"] = "completed"  # type: ignore[index]
        plan["stages"][1]["metric_snapshot"] = self._snapshot("candidate.json", 0.6)  # type: ignore[index]
        plan["checkpoint"] = {
            "resume_mode": "weights_only",
            "manifest_path": self.manifest["manifest_path"],
            "manifest_sha256": self.manifest["manifest_sha256"],
            "required_components": ["model", "native_state"],
        }
        self.assertEqual(evaluate_fidelity(plan, self.root)["status"], "promote")
        self.assertEqual(
            evaluate_fidelity(plan, self.root)["hypothesis_result"], "not_evaluated"
        )
        self._write_metric("candidate.json", "full", 100, 0.501)
        plan["stages"][1]["metric_snapshot"] = self._snapshot("candidate.json", 0.501)  # type: ignore[index]
        self.assertEqual(evaluate_fidelity(plan, self.root)["status"], "prune")

    def test_new_run_checkpoint_object_is_allowed(self) -> None:
        plan = self._plan()
        plan["checkpoint"] = {"resume_mode": "new_run"}
        self.assertEqual(
            evaluate_fidelity(plan, self.root)["checkpoint"]["status"], "not_requested"
        )

    def test_new_run_cannot_claim_existing_progress(self) -> None:
        plan = self._plan()
        plan["checkpoint"] = None
        plan["observed"]["completed"] = 1  # type: ignore[index]
        with self.assertRaisesRegex(FidelityError, "new_run"):
            evaluate_fidelity(plan, self.root)

    def test_changed_checkpoint_artifact_is_blocked(self) -> None:
        plan = self._plan()
        (self.root / "model.bin").write_text("changed", encoding="utf-8")
        result = evaluate_fidelity(plan, self.root)
        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "checkpoint component hash mismatch: model", result["checkpoint"]["errors"]
        )

    def test_lineage_split_mismatch_is_blocked(self) -> None:
        plan = self._plan()
        plan["lineage"]["split_sha256"] = "e" * 64  # type: ignore[index]
        with self.assertRaisesRegex(FidelityError, "does not match"):
            evaluate_fidelity(plan, self.root)

    def test_status_label_cannot_bypass_metric_evidence(self) -> None:
        plan = self._plan()
        plan["stages"][1]["status"] = "failed"  # type: ignore[index]
        plan["stages"][1]["metric_snapshot"] = self._snapshot("candidate.json", 100.0)  # type: ignore[index]
        with self.assertRaisesRegex(FidelityError, "not completed"):
            evaluate_fidelity(plan, self.root)

    def test_metric_file_content_is_checked_and_rung_artifact_cannot_be_reused(
        self,
    ) -> None:
        plan = self._plan()
        plan["stages"][1]["metric_snapshot"] = self._snapshot("baseline.json", 0.6)  # type: ignore[index]
        with self.assertRaisesRegex(FidelityError, "does not match"):
            evaluate_fidelity(plan, self.root)

    def test_completed_stage_cannot_skip_unfinished_rung(self) -> None:
        plan = self._plan()
        plan["stages"].insert(
            1, {"name": "middle", "budget": 50, "status": "planned", "cohort_id": "c1"}
        )  # type: ignore[union-attr]
        with self.assertRaisesRegex(FidelityError, "unfinished earlier stage"):
            evaluate_fidelity(plan, self.root)

    def test_infeasible_runtime_blocks_even_with_good_scores(self) -> None:
        plan = self._plan()
        plan["observed"].update({"speed": 1, "available_seconds": 1})  # type: ignore[union-attr]
        result = evaluate_fidelity(plan, self.root)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["feasibility"]["status"], "infeasible")

    def test_next_rung_can_be_allowed_when_final_horizon_does_not_fit(self) -> None:
        plan = self._plan()
        plan["observed"].update({"speed": 1, "available_seconds": 50})  # type: ignore[union-attr]
        result = evaluate_fidelity(plan, self.root)
        self.assertEqual(result["feasibility"]["full_budget_status"], "infeasible")
        self.assertTrue(result["feasibility"]["next_stage_feasible"])
        self.assertEqual(result["feasibility"]["recommended_action"], "run_next_rung")
        self.assertEqual(result["status"], "promote")

    def test_partial_artifact_hash_is_required(self) -> None:
        plan = self._plan()
        (self.root / "features.bin").write_text("features", encoding="utf-8")
        plan["partial_artifacts"] = [
            {
                "reuse_class": "features",
                "issue_axis": "representation",
                "path": "features.bin",
                "sha256": "0" * 64,
            }
        ]
        result = evaluate_fidelity(plan, self.root)
        self.assertEqual(result["status"], "blocked")

    def test_checkpoint_manifest_binds_actual_hashes(self) -> None:
        self.assertEqual(
            verify_checkpoint_manifest(self.root, self.manifest["manifest_path"])[
                "status"
            ],
            "PASS",
        )
        (self.root / "model.bin").write_text("changed", encoding="utf-8")
        self.assertEqual(
            verify_checkpoint_manifest(self.root, self.manifest["manifest_path"])[
                "status"
            ],
            "FAIL",
        )

    def test_manifest_capability_mismatch_is_blocked(self) -> None:
        manifest_path = self.root / self.manifest["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["capability"] = "dl"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        plan = self._plan()
        plan["checkpoint"]["manifest_sha256"] = hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()  # type: ignore[index]
        result = evaluate_fidelity(plan, self.root)
        self.assertIn(
            "checkpoint manifest schema or resume mode mismatch",
            result["checkpoint"]["errors"],
        )

    def test_duplicate_manifest_components_are_blocked(self) -> None:
        manifest_path = self.root / self.manifest["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["components"].append(dict(manifest["components"][0]))
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        plan = self._plan()
        plan["checkpoint"]["manifest_sha256"] = hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()  # type: ignore[index]
        result = evaluate_fidelity(plan, self.root)
        self.assertIn(
            "duplicate checkpoint component: model", result["checkpoint"]["errors"]
        )

    def test_strict_unknown_and_duplicate_stages_are_rejected(self) -> None:
        plan = self._plan()
        plan["extra"] = True
        with self.assertRaisesRegex(FidelityError, "unknown keys"):
            evaluate_fidelity(plan, self.root)
        plan = self._plan()
        plan["stages"][1]["name"] = "short"  # type: ignore[index]
        with self.assertRaisesRegex(FidelityError, "duplicate stage"):
            evaluate_fidelity(plan, self.root)


if __name__ == "__main__":
    unittest.main()
