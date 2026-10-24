"""Tests for dependency-bound validation planning."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from tools.competition_automation.cli import main
from tools.competition_automation.validation import (
    ValidationError,
    plan_validation,
    run_check,
    semantic_check_signature,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan(
    root: Path, *, config_value: int = 1, changed_branches: list[str] | None = None
) -> dict[str, Any]:
    dependency = {
        "path": "input.txt",
        "sha256": _sha(root / "input.txt"),
        "kind": "data",
        "role": "input",
    }
    check = {
        "id": "smoke",
        "kind": "smoke",
        "command": ["python3", "runner.py", "--sample"],
        "dependencies": [dependency],
        "scope": {"dataset": "sample", "split": "v1"},
        "config": {"coefficient": config_value},
        "changed_branches": changed_branches or [],
        "depends_on": [],
        "required": True,
        "fanout": False,
        "run_mode": "sample",
    }
    return {"schema_version": 1, "checks": [check], "receipts": []}


def _receipt(
    root: Path, plan: dict[str, Any], *, branch_coverage: dict[str, int] | None = None
) -> dict[str, Any]:
    check = plan["checks"][0]
    report = root / "report.json"
    report.write_text(
        json.dumps({"status": "PASS", "branch_coverage": branch_coverage or {}}) + "\n",
        encoding="utf-8",
    )
    evidence = root / "process.json"
    started = "2026-01-01T00:00:00+00:00"
    finished = "2026-01-01T00:00:01+00:00"
    evidence.write_text(
        json.dumps(
            {
                "command": check["command"],
                "pid": 1,
                "returncode": 0,
                "started_at": started,
                "finished_at": finished,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "check_id": "smoke",
        "status": "PASS",
        "command": check["command"],
        "dependencies": check["dependencies"],
        "scope": check["scope"],
        "config": check["config"],
        "process": {
            "returncode": 0,
            "pid": 1,
            "started_at": started,
            "finished_at": finished,
            "evidence": {"path": "process.json", "sha256": _sha(evidence)},
        },
        "report": {"path": "report.json", "sha256": _sha(report)},
        "executable_fingerprint": "exe-sha",
        "prediction_fingerprint": "pred-sha",
    }


class ValidationPlanTests(unittest.TestCase):
    """Ensure cache, invalidation, branch, and canary decisions are deterministic."""

    def test_compatible_receipt_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            plan["receipts"] = [_receipt(root, plan)]
            result = plan_validation(plan, root)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["checks"][0]["status"], "CACHED_PASS")
            self.assertFalse(result["prediction_identity_is_executable_identity"])

    def test_stale_dependency_invalidates_only_dependent_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            plan["checks"].append(
                {
                    **plan["checks"][0],
                    "id": "independent",
                    "dependencies": [],
                    "changed_branches": [],
                    "depends_on": [],
                }
            )
            independent_receipt = _receipt(root, plan)
            independent_receipt["check_id"] = "independent"
            independent_receipt["dependencies"] = []
            independent_receipt["config"] = plan["checks"][1]["config"]
            plan["receipts"] = [_receipt(root, plan), independent_receipt]
            (root / "input.txt").write_text("changed\n", encoding="utf-8")
            result = plan_validation(plan, root)
            decisions = {item["check_id"]: item["status"] for item in result["checks"]}
            self.assertEqual(decisions["smoke"], "RUN_REQUIRED")
            self.assertEqual(decisions["independent"], "CACHED_PASS")

    def test_stale_report_requires_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            receipt = _receipt(root, plan)
            (root / "report.json").write_text(
                '{"status":"tampered"}\n', encoding="utf-8"
            )
            plan["receipts"] = [receipt]
            result = plan_validation(plan, root)
            self.assertEqual(result["checks"][0]["status"], "RUN_REQUIRED")

    def test_changed_branch_needs_targeted_input_even_with_smoke_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root, changed_branches=["new_branch"])
            plan["receipts"] = [_receipt(root, plan)]
            result = plan_validation(plan, root)
            self.assertEqual(result["checks"][0]["status"], "NEEDS_TARGETED_INPUT")
            self.assertEqual(result["targeted_input_checks"][0]["check_id"], "smoke")
            self.assertEqual(
                result["targeted_input_checks"][0]["missing_branches"], ["new_branch"]
            )

            plan["receipts"] = [_receipt(root, plan, branch_coverage={"new_branch": 1})]
            self.assertEqual(
                plan_validation(plan, root)["checks"][0]["status"], "CACHED_PASS"
            )

    def test_coefficient_change_does_not_reuse_old_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            old = _plan(root, config_value=1)
            old_receipt = _receipt(root, old)
            current = _plan(root, config_value=2)
            current["receipts"] = [old_receipt]
            result = plan_validation(current, root)
            self.assertEqual(result["checks"][0]["status"], "RUN_REQUIRED")
            self.assertEqual(result["checks"][0]["run_scope"], "targeted")
            self.assertFalse(result["full_run_required"])

    def test_pending_canary_blocks_fanout_but_does_not_claim_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            plan["checks"][0]["fanout"] = True
            plan["canary"] = {
                "required": True,
                "runtime_class": "remote-v2",
                "attempts": 0,
                "official_receipt": {"status": "Scoring", "runtime_class": "remote-v2"},
            }
            result = plan_validation(plan, root)
            self.assertEqual(result["canary"]["status"], "PENDING_CANARY")
            self.assertFalse(result["canary"]["fanout_allowed"])
            self.assertEqual(result["checks"][0]["status"], "PENDING_CANARY")
            self.assertNotEqual(result["status"], "PASS")

    def test_pending_canary_still_allows_independent_local_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            plan["checks"][0]["fanout"] = False
            plan["canary"] = {
                "required": True,
                "runtime_class": "remote-v2",
                "environment": {"image": "v2"},
                "attempts": 0,
                "official_receipt": {"status": "Scoring", "runtime_class": "remote-v2"},
            }
            result = plan_validation(plan, root)
            self.assertEqual(result["runnable_checks"], ["smoke"])

    def test_official_canary_requires_hashed_raw_and_normalized_observations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            official = {
                "status": "COMPLETE",
                "submission_id": "sub-1",
                "runtime_class": "remote-v2",
                "environment": {"image": "v2"},
            }
            raw = root / "raw.json"
            observation = root / "observation.json"
            raw.write_text(json.dumps(official) + "\n", encoding="utf-8")
            observation.write_text(json.dumps(official) + "\n", encoding="utf-8")
            plan = _plan(root)
            plan["canary"] = {
                "required": True,
                "runtime_class": "remote-v2",
                "environment": {"image": "v2"},
                "attempts": 1,
                "official_receipt": {
                    **official,
                    "source": "official",
                    "raw_snapshot": {"path": "raw.json", "sha256": _sha(raw)},
                    "observation": {
                        "path": "observation.json",
                        "sha256": _sha(observation),
                    },
                },
            }
            self.assertEqual(plan_validation(plan, root)["canary"]["status"], "OPEN")
            plan["canary"]["official_receipt"]["source"] = "local"
            self.assertNotEqual(plan_validation(plan, root)["canary"]["status"], "OPEN")

            plan["canary"]["official_receipt"]["source"] = "official"
            plan["canary"]["official_receipt"]["observation"]["path"] = "raw.json"
            plan["canary"]["official_receipt"]["observation"]["sha256"] = _sha(raw)
            self.assertNotEqual(plan_validation(plan, root)["canary"]["status"], "OPEN")

    def test_all_declared_artifacts_must_remain_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            receipt = _receipt(root, plan)
            extra = root / "extra.bin"
            extra.write_bytes(b"extra")
            receipt["artifacts"] = [
                {"path": "extra.bin", "sha256": _sha(extra)},
                {"path": "missing.bin", "sha256": "0" * 64},
            ]
            plan["receipts"] = [receipt]
            self.assertEqual(
                plan_validation(plan, root)["checks"][0]["status"], "RUN_REQUIRED"
            )

    def test_branch_gap_invalidates_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root, changed_branches=["new_branch"])
            child = {
                **plan["checks"][0],
                "id": "child",
                "changed_branches": [],
                "depends_on": ["smoke"],
            }
            plan["checks"].append(child)
            plan["receipts"] = [
                _receipt(root, plan),
                {**_receipt(root, plan), "check_id": "child"},
            ]
            result = plan_validation(plan, root)
            statuses = {item["check_id"]: item["status"] for item in result["checks"]}
            self.assertEqual(statuses["smoke"], "NEEDS_TARGETED_INPUT")
            self.assertIn("child", result["invalidated_checks"])
            self.assertEqual(statuses["child"], "RUN_REQUIRED")
            self.assertEqual(result["runnable_checks"], [])

    def test_dependency_cycle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            plan["checks"].append(
                {**plan["checks"][0], "id": "child", "depends_on": ["smoke"]}
            )
            plan["checks"][0]["depends_on"] = ["child"]
            with self.assertRaises(ValidationError):
                plan_validation(plan, root)

    def test_finite_float_is_allowed_and_nonfinite_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            plan["checks"][0]["config"]["learning_rate"] = 0.001
            plan_validation(plan, root)
            plan["checks"][0]["config"]["learning_rate"] = float("inf")
            with self.assertRaises(ValidationError):
                plan_validation(plan, root)

    def test_run_check_creates_real_process_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            check = plan["checks"][0]
            check["command"] = ["python3", "-c", 'print(\'{"status": "PASS"}\')']
            receipt = run_check(check, root)
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["process"]["returncode"], 0)
            self.assertTrue((root / receipt["process"]["evidence"]["path"]).is_file())
            plan["receipts"] = [receipt]
            self.assertEqual(plan_validation(plan, root)["status"], "PASS")

    def test_run_check_rejects_preexisting_report_before_starting_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            marker = root / "marker.txt"
            check = plan["checks"][0]
            check["command"] = [
                "python3",
                "-c",
                "open('marker.txt', 'w').write('started')",
            ]
            (root / "old-report.json").write_text(
                '{"status":"PASS"}\n', encoding="utf-8"
            )
            with self.assertRaises(ValidationError):
                run_check(check, root, report_path=Path("old-report.json"))
            self.assertFalse(marker.exists())

    def test_run_check_rejects_stale_dependency_before_starting_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            check = plan["checks"][0]
            check["dependencies"][0]["sha256"] = "0" * 64
            check["command"] = [
                "python3",
                "-c",
                "open('marker.txt', 'w').write('started')",
            ]
            with self.assertRaises(ValidationError):
                run_check(check, root)
            self.assertFalse((root / "marker.txt").exists())

    def test_run_check_marks_input_changed_during_run_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            check = plan["checks"][0]
            check["command"] = [
                "python3",
                "-c",
                "open('input.txt', 'w').write('changed\\n'); print('{\"status\":\"PASS\"}')",
            ]
            receipt = run_check(check, root)
            self.assertEqual(receipt["status"], "FAILED")
            self.assertTrue(receipt["dependency_changed"])

    def test_run_check_does_not_promote_fail_report_with_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            check = plan["checks"][0]
            check["command"] = ["python3", "-c", 'print(\'{"status":"FAIL"}\')']
            receipt = run_check(check, root)
            self.assertEqual(receipt["status"], "FAILED")

    def test_validation_run_cli_executes_and_reuses_cached_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            plan["checks"][0]["command"] = [
                "python3",
                "-c",
                'print(\'{"status":"PASS"}\')',
            ]
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            report = root / "report.json"
            evidence = root / "evidence.json"
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "validation-run",
                        str(plan_path),
                        "smoke",
                        "--root",
                        str(root),
                        "--report",
                        str(report),
                        "--evidence",
                        str(evidence),
                        "--timeout",
                        "10",
                    ]
                )
            self.assertEqual(code, 0)
            executed = json.loads(output.getvalue())
            self.assertEqual(executed["status"], "EXECUTED")
            plan["receipts"] = [executed["receipt"]]
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            report_bytes = report.read_bytes()
            evidence_bytes = evidence.read_bytes()
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "validation-run",
                        str(plan_path),
                        "smoke",
                        "--root",
                        str(root),
                        "--report",
                        str(report),
                        "--evidence",
                        str(evidence),
                        "--timeout",
                        "10",
                    ]
                )
            self.assertEqual(code, 0)
            reused = json.loads(output.getvalue())
            self.assertEqual(reused["status"], "REUSE")
            self.assertEqual(report.read_bytes(), report_bytes)
            self.assertEqual(evidence.read_bytes(), evidence_bytes)

    def test_validation_run_cli_returns_nonzero_for_targeted_input_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root, changed_branches=["new_branch"])
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "validation-run",
                        str(plan_path),
                        "smoke",
                        "--root",
                        str(root),
                        "--report",
                        "report.json",
                        "--evidence",
                        "evidence.json",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "BLOCKED")

    def test_semantic_signature_ignores_names_paths_and_timestamps(self) -> None:
        first = {
            "id": "one",
            "kind": "smoke",
            "name": "run-one",
            "scope": {"dataset": "sample", "split": "v1"},
            "config": {"coefficient": 1},
            "operational_metadata": {"path": "/tmp/one", "created_at": "a"},
            "dependencies": [{"path": "a.txt", "sha256": "a" * 64}],
        }
        second = {
            "id": "two",
            "kind": "smoke",
            "name": "run-two",
            "scope": {"dataset": "sample", "split": "v1"},
            "config": {"coefficient": 1},
            "operational_metadata": {"path": "/tmp/two", "created_at": "b"},
            "dependencies": [{"path": "b.txt", "sha256": "a" * 64}],
        }
        self.assertEqual(
            semantic_check_signature(first), semantic_check_signature(second)
        )

    def test_nan_and_unknown_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_text("a\n", encoding="utf-8")
            plan = _plan(root)
            plan["metadata"] = {"score": float("nan")}
            with self.assertRaises(ValidationError):
                plan_validation(plan, root)
            plan = _plan(root)
            plan["unexpected"] = True
            with self.assertRaises(ValidationError):
                plan_validation(plan, root)


if __name__ == "__main__":
    unittest.main()
