"""Tests for distribution-aware final selection and retrospective controls."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from tools.competition_automation.fingerprint import fingerprint_path
from tools.competition_automation.retrospective import RetrospectiveError, evaluate_retrospective
from tools.competition_automation.selection import SelectionError, validate_selection_plan


def _sha(character: str) -> str:
    """Return a stable SHA-shaped test value."""
    return character * 64


def _plan() -> dict[str, object]:
    """Return a valid two-family selection plan."""
    return {
        "schema_version": 1,
        "frozen_at": "2026-08-27T00:00:00Z",
        "metric": {"name": "log_loss", "direction": "min"},
        "profiles": [
            {
                "name": "session_group",
                "role": "diagnostic",
                "distribution_signature": _sha("1"),
                "group_key": "session_id",
            },
            {
                "name": "objective_cold",
                "role": "selection",
                "distribution_signature": _sha("2"),
                "group_key": "learning_objective_id",
            },
            {
                "name": "worst_fold",
                "role": "stress",
                "distribution_signature": _sha("3"),
                "group_key": "learning_objective_id",
            },
        ],
        "policy": {
            "selection_profile": "objective_cold",
            "required_stress_profiles": ["worst_fold"],
            "equivalence_margin": 0.001,
            "replacement_requires_interval": True,
            "max_hedge_regret": 0.003,
            "max_hedge_correlation": 0.9,
            "max_stress_regret": {"worst_fold": 0.003},
            "minimum_distinct_families": 2,
            "max_official_feedback_rounds_per_family": 1,
        },
        "champion_before": "v004",
        "champion_after": "v004",
        "preexisting_submissions": ["v004"],
        "selected": ["v004", "v008"],
        "candidates": [
            {
                "record_id": "v004",
                "family": "sparse",
                "artifact_sha256": _sha("a"),
                "complexity_rank": 1,
                "metric_values": {
                    "session_group": 0.5357,
                    "objective_cold": 0.5948,
                    "worst_fold": 0.6173,
                },
                "interval_vs_champion": {
                    "lower": 0.0,
                    "upper": 0.0,
                    "confidence": 0.95,
                    "group_key": "learning_objective_id",
                },
                "correlation_with_champion": 1.0,
                "official_feedback_round": 0,
            },
            {
                "record_id": "v008",
                "family": "semantic-hedge",
                "artifact_sha256": _sha("b"),
                "complexity_rank": 2,
                "metric_values": {
                    "session_group": 0.536,
                    "objective_cold": 0.5952,
                    "worst_fold": 0.616,
                },
                "interval_vs_champion": {
                    "lower": -0.0011,
                    "upper": 0.00184,
                    "confidence": 0.95,
                    "group_key": "learning_objective_id",
                },
                "correlation_with_champion": 0.82,
                "official_feedback_round": 1,
            },
        ],
    }


def _write_v2_plan(root: Path, matched_cohort: bool = False) -> dict[str, object]:
    """Write evidence files and return a valid schema-v2 selection plan."""
    plan = _plan()
    plan["schema_version"] = 2
    plan["policy"]["minimum_distinct_lineages"] = 2
    plan["policy"]["max_official_feedback_rounds_per_lineage"] = 1
    if matched_cohort:
        plan["policy"]["require_matched_scored_cohort"] = True
    for profile in plan["profiles"]:
        profile_path = Path("evidence") / f"profile-{profile['name']}.json"
        profile_absolute = root / profile_path
        profile_absolute.parent.mkdir(parents=True, exist_ok=True)
        profile_absolute.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": profile["name"],
                    "role": profile["role"],
                    "group_key": profile["group_key"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        profile["distribution_signature"] = fingerprint_path(profile_absolute)["sha256"]
        profile["manifest"] = {
            "path": profile_path.as_posix(),
            "sha256": profile["distribution_signature"],
        }
        if matched_cohort and profile["role"] in {"selection", "stress"}:
            cohort_path = Path("evidence") / f"cohort-{profile['name']}.jsonl"
            cohort_absolute = root / cohort_path
            cohort_absolute.write_text(
                "".join(
                    json.dumps({"row_key": str(row), "reference": row}) + "\n"
                    for row in range(100)
                ),
                encoding="utf-8",
            )
            profile["scored_cohort"] = {
                "path": cohort_path.as_posix(),
                "sha256": fingerprint_path(cohort_absolute)["sha256"],
            }
    for index, candidate in enumerate(plan["candidates"]):
        candidate["lineage_signature"] = _sha(str(index + 4))
        candidate["interval_vs_champion"]["champion_record_id"] = "v004"
        candidate["correlation_champion_record_id"] = "v004"
        result_path = Path("evidence") / f"result-{candidate['record_id']}.json"
        eligibility_path = Path("evidence") / f"eligibility-{candidate['record_id']}.json"
        profile_results = {
            name: {
                "metric_value": value,
                "distribution_signature": next(
                    profile["distribution_signature"]
                    for profile in plan["profiles"]
                    if profile["name"] == name
                ),
                "group_key": next(
                    profile["group_key"]
                    for profile in plan["profiles"]
                    if profile["name"] == name
                ),
                "n_observations": 100,
            }
            for name, value in candidate["metric_values"].items()
        }
        if matched_cohort:
            for name, result in profile_results.items():
                profile = next(profile for profile in plan["profiles"] if profile["name"] == name)
                if "scored_cohort" in profile:
                    result["scored_cohort_sha256"] = profile["scored_cohort"]["sha256"]
        result = {
            "schema_version": 1,
            "record_id": candidate["record_id"],
            "artifact_sha256": candidate["artifact_sha256"],
            "family": candidate["family"],
            "lineage_signature": candidate["lineage_signature"],
            "complexity_rank": candidate["complexity_rank"],
            "profile_results": profile_results,
            "interval_vs_champion": candidate["interval_vs_champion"],
            "correlation_with_champion": candidate["correlation_with_champion"],
            "correlation_champion_record_id": candidate[
                "correlation_champion_record_id"
            ],
        }
        result_absolute = root / result_path
        result_absolute.parent.mkdir(parents=True, exist_ok=True)
        result_absolute.write_text(json.dumps(result) + "\n", encoding="utf-8")
        eligibility = {
            "schema_version": 1,
            "record_id": candidate["record_id"],
            "artifact_sha256": candidate["artifact_sha256"],
            "status": "verified",
            "rules_snapshot_sha256": _sha("9"),
            "checks": ["availability timing", "license", "submission contract"],
        }
        eligibility_absolute = root / eligibility_path
        eligibility_absolute.write_text(json.dumps(eligibility) + "\n", encoding="utf-8")
        candidate["result_evidence"] = {
            "path": result_path.as_posix(),
            "sha256": fingerprint_path(result_absolute)["sha256"],
        }
        candidate["eligibility"] = {
            "status": "verified",
            "evidence": {
                "path": eligibility_path.as_posix(),
                "sha256": fingerprint_path(eligibility_absolute)["sha256"],
            },
        }
    return plan


class SelectionTests(unittest.TestCase):
    """Ensure champion replacement and hedge diversity are evidence-gated."""

    def test_valid_equivalent_hedge_keeps_simple_champion(self) -> None:
        result = validate_selection_plan(_plan())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["champion_after"], "v004")
        self.assertEqual(result["distinct_families"], 2)

    def test_selection_cli_emits_machine_readable_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "selection.json"
            path.write_text(json.dumps(_plan()) + "\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["selection-check", str(path)])
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "PASS")

    def test_diagnostic_profile_cannot_drive_selection(self) -> None:
        plan = _plan()
        plan["policy"]["selection_profile"] = "session_group"
        with self.assertRaises(SelectionError):
            validate_selection_plan(plan)

    def test_equivalent_complex_challenger_cannot_replace_champion(self) -> None:
        plan = _plan()
        plan["champion_after"] = "v008"
        plan["selected"] = ["v008", "v004"]
        with self.assertRaisesRegex(SelectionError, "champion replacement"):
            validate_selection_plan(plan)

    def test_highly_correlated_or_regressive_hedge_is_rejected(self) -> None:
        plan = _plan()
        plan["candidates"][1]["correlation_with_champion"] = 0.95
        with self.assertRaisesRegex(SelectionError, "measured diversity"):
            validate_selection_plan(plan)
        plan = _plan()
        plan["candidates"][1]["metric_values"]["objective_cold"] = 0.61
        with self.assertRaisesRegex(SelectionError, "max_hedge_regret"):
            validate_selection_plan(plan)

    def test_official_feedback_round_is_bounded(self) -> None:
        plan = _plan()
        plan["candidates"][1]["official_feedback_round"] = 2
        with self.assertRaisesRegex(SelectionError, "official feedback budget"):
            validate_selection_plan(plan)

    def test_selected_candidate_cannot_hide_stress_regression(self) -> None:
        plan = _plan()
        plan["candidates"][1]["metric_values"]["worst_fold"] = 0.63
        with self.assertRaisesRegex(SelectionError, "max_stress_regret"):
            validate_selection_plan(plan)

    def test_interval_group_must_match_selection_profile(self) -> None:
        plan = _plan()
        plan["candidates"][1]["interval_vs_champion"]["group_key"] = "session_id"
        with self.assertRaisesRegex(SelectionError, "match selection profile"):
            validate_selection_plan(plan)

    def test_v2_binds_declared_values_and_eligibility_to_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _write_v2_plan(root)
            self.assertTrue(validate_selection_plan(plan, root=root)["eligibility_verified"])
            plan["candidates"][1]["metric_values"]["objective_cold"] = 0.1
            with self.assertRaisesRegex(SelectionError, "does not match result evidence"):
                validate_selection_plan(plan, root=root)

    def test_v2_rejects_unverified_selected_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _write_v2_plan(root)
            plan["candidates"][1]["eligibility"] = {
                "status": "unverified",
                "evidence": None,
            }
            with self.assertRaisesRegex(SelectionError, "must be verified eligible"):
                validate_selection_plan(plan, root=root)

    def test_v2_rejects_empty_profile_and_stale_correlation_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _write_v2_plan(root)
            candidate = plan["candidates"][1]
            evidence_path = root / candidate["result_evidence"]["path"]
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["profile_results"]["worst_fold"]["n_observations"] = 0
            evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
            candidate["result_evidence"]["sha256"] = fingerprint_path(evidence_path)["sha256"]
            with self.assertRaisesRegex(SelectionError, "n_observations"):
                validate_selection_plan(plan, root=root)

            plan = _write_v2_plan(root)
            candidate = plan["candidates"][1]
            evidence_path = root / candidate["result_evidence"]["path"]
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            candidate["correlation_champion_record_id"] = "stale-champion"
            evidence["correlation_champion_record_id"] = "stale-champion"
            evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
            candidate["result_evidence"]["sha256"] = fingerprint_path(evidence_path)["sha256"]
            with self.assertRaisesRegex(SelectionError, "correlation target"):
                validate_selection_plan(plan, root=root)

    def test_v2_feedback_budget_is_shared_by_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _write_v2_plan(root)
            plan["policy"]["minimum_distinct_lineages"] = 1
            first = plan["candidates"][0]
            second = plan["candidates"][1]
            first["official_feedback_round"] = 1
            first["lineage_signature"] = second["lineage_signature"]
            evidence_path = root / first["result_evidence"]["path"]
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["lineage_signature"] = second["lineage_signature"]
            evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
            first["result_evidence"]["sha256"] = fingerprint_path(evidence_path)["sha256"]
            with self.assertRaisesRegex(SelectionError, "shared lineage"):
                validate_selection_plan(plan, root=root)

    def test_v2_matched_cohort_binds_rows_and_observation_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _write_v2_plan(root, matched_cohort=True)
            self.assertEqual(validate_selection_plan(plan, root=root)["status"], "PASS")

            candidate = plan["candidates"][1]
            evidence_path = root / candidate["result_evidence"]["path"]
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["profile_results"]["objective_cold"]["n_observations"] = 99
            evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
            candidate["result_evidence"]["sha256"] = fingerprint_path(evidence_path)["sha256"]
            with self.assertRaisesRegex(SelectionError, "observation count differs"):
                validate_selection_plan(plan, root=root)

            evidence["profile_results"]["objective_cold"]["n_observations"] = 100
            evidence["profile_results"]["objective_cold"]["scored_cohort_sha256"] = _sha("f")
            evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
            candidate["result_evidence"]["sha256"] = fingerprint_path(evidence_path)["sha256"]
            with self.assertRaisesRegex(SelectionError, "scored cohort mismatch"):
                validate_selection_plan(plan, root=root)

            plan = _write_v2_plan(root, matched_cohort=True)
            profile = next(profile for profile in plan["profiles"] if profile["name"] == "objective_cold")
            cohort_path = root / profile["scored_cohort"]["path"]
            cohort_path.write_text('{"row_key": "only", "reference": 1}\n', encoding="utf-8")
            profile["scored_cohort"]["sha256"] = fingerprint_path(cohort_path)["sha256"]
            for candidate in plan["candidates"]:
                evidence_path = root / candidate["result_evidence"]["path"]
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                evidence["profile_results"]["objective_cold"]["scored_cohort_sha256"] = profile["scored_cohort"]["sha256"]
                evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
                candidate["result_evidence"]["sha256"] = fingerprint_path(evidence_path)["sha256"]
            with self.assertRaisesRegex(SelectionError, "observation count differs"):
                validate_selection_plan(plan, root=root)

    def test_v2_matched_cohort_rejects_duplicate_and_nonfinite_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _write_v2_plan(root, matched_cohort=True)
            profile = next(profile for profile in plan["profiles"] if profile["name"] == "objective_cold")
            cohort_path = root / profile["scored_cohort"]["path"]
            cohort_path.write_text(
                '{"row_key":"same","reference":1}\n'
                '{"row_key":"same","reference":2}\n',
                encoding="utf-8",
            )
            profile["scored_cohort"]["sha256"] = fingerprint_path(cohort_path)["sha256"]
            with self.assertRaisesRegex(SelectionError, "duplicate key"):
                validate_selection_plan(plan, root=root)

            cohort_path.write_text('{"row_key":"one","reference":NaN}\n', encoding="utf-8")
            profile["scored_cohort"]["sha256"] = fingerprint_path(cohort_path)["sha256"]
            with self.assertRaisesRegex(SelectionError, "non-finite"):
                validate_selection_plan(plan, root=root)


class RetrospectiveTests(unittest.TestCase):
    """Ensure final rank flips and unmet targets are computed from frozen evidence."""

    def test_final_public_private_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = root / "evidence"
            automation = root / "automation"
            evidence.mkdir()
            automation.mkdir()
            official = evidence / "final.json"
            official.write_text("official final leaderboard snapshot\n", encoding="utf-8")
            plan_path = automation / "selection.json"
            plan_path.write_text(json.dumps(_plan()) + "\n", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "competition_ended_at": "2026-08-28T09:00:00Z",
                "metric": {"name": "log_loss", "direction": "min", "target": 0.55},
                "official_snapshot": {
                    "path": "evidence/final.json",
                    "sha256": fingerprint_path(official)["sha256"],
                },
                "selection_plan": {
                    "path": "automation/selection.json",
                    "sha256": fingerprint_path(plan_path)["sha256"],
                },
                "submissions": [
                    {
                        "record_id": "v004",
                        "submission_id": "id-5852",
                        "submitted_at": "2026-08-20T00:00:00Z",
                        "artifact_sha256": _sha("a"),
                        "family": "sparse",
                        "complexity_rank": 1,
                        "public_score": 0.5971,
                        "private_score": 0.5998,
                    },
                    {
                        "record_id": "v008",
                        "submission_id": "id-7346",
                        "submitted_at": "2026-08-27T12:00:00Z",
                        "artifact_sha256": _sha("b"),
                        "family": "semantic-hedge",
                        "complexity_rank": 2,
                        "public_score": 0.5982,
                        "private_score": 0.5995,
                    },
                ],
            }
            result = evaluate_retrospective(manifest, root)
            self.assertFalse(result["target_achieved"])
            self.assertTrue(result["public_private_winner_changed"])
            self.assertEqual(result["best_private"]["record_id"], "v008")
            self.assertEqual(result["simplest_equivalent_private_candidate"], "v004")
            self.assertAlmostEqual(result["champion_private_regret"], 0.0003)
            manifest_path = automation / "retrospective.json"
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    ["retrospective-check", str(manifest_path), "--root", str(root)]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "PASS")

    def test_v2_reconciles_snapshot_metadata_and_selection_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _write_v2_plan(root)
            automation = root / "automation"
            automation.mkdir()
            plan_path = automation / "selection.json"
            plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8")
            rows = [
                {
                    "record_id": "v004",
                    "submission_id": "id-5852",
                    "submitted_at": "2026-08-20T00:00:00Z",
                    "artifact_sha256": _sha("a"),
                    "family": "sparse",
                    "complexity_rank": 1,
                    "public_score": 0.5971,
                    "private_score": 0.5998,
                },
                {
                    "record_id": "v008",
                    "submission_id": "id-7346",
                    "submitted_at": "2026-08-27T12:00:00Z",
                    "artifact_sha256": _sha("b"),
                    "family": "semantic-hedge",
                    "complexity_rank": 2,
                    "public_score": 0.5982,
                    "private_score": 0.5995,
                },
            ]
            official = root / "evidence" / "final.json"
            raw_official = root / "evidence" / "final-raw.html"
            raw_official.write_text("official leaderboard export\n", encoding="utf-8")
            official.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "scope": "all_user_submissions",
                        "evidence_kind": "official",
                        "source": {
                            "path": "evidence/final-raw.html",
                            "sha256": fingerprint_path(raw_official)["sha256"],
                        },
                        "submissions": [
                            {
                                "submission_id": row["submission_id"],
                                "public_score": row["public_score"],
                                "private_score": row["private_score"],
                            }
                            for row in rows
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            manifest = {
                "schema_version": 2,
                "competition_ended_at": "2026-08-28T09:00:00Z",
                "metric": {"name": "log_loss", "direction": "min", "target": 0.55},
                "official_snapshot": {
                    "path": "evidence/final.json",
                    "sha256": fingerprint_path(official)["sha256"],
                },
                "selection_plan": {
                    "path": "automation/selection.json",
                    "sha256": fingerprint_path(plan_path)["sha256"],
                },
                "submissions": rows,
            }
            result = evaluate_retrospective(manifest, root)
            self.assertEqual(result["result_scope"], "all_user_submissions")
            self.assertEqual(result["best_verified_eligible_private"]["record_id"], "v008")
            self.assertAlmostEqual(result["selected_private_regret"], 0.0)
            self.assertIsNotNone(result["selection_private_rank_correlation"])
            manifest["submissions"][1]["family"] = "renamed-family"
            with self.assertRaisesRegex(RetrospectiveError, "does not match selection plan"):
                evaluate_retrospective(manifest, root)


if __name__ == "__main__":
    unittest.main()
from tools.competition_automation.cli import main
