"""Regression tests for deterministic local/Kaggle/external compute routing."""

from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from tools.competition_automation.cli import main
from tools.competition_automation.compute import (
    ComputeError,
    append_compute_job,
    plan_compute_request,
    validate_compute_job,
    validate_compute_profile,
)
from tools.competition_automation.fingerprint import fingerprint_path


class ComputeAutomationTests(unittest.TestCase):
    """Ensure external workers remain bounded by rules, evidence, and budgets."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        for name, content in (
            ("rules.html", "official competition rules\n"),
            ("input.json", "{}\n"),
            ("benchmark.json", '{"sample_count": 100}\n'),
            ("data.json", "{}\n"),
            ("code.json", "{}\n"),
            ("config.json", "{}\n"),
            ("split.json", "{}\n"),
            ("model.ckpt", "model\n"),
            ("artifact-check.json", '{"status": "PASS"}\n'),
            ("replay-check.json", '{"status": "PASS"}\n'),
            ("availability.json", '{"available": true}\n'),
            ("controller.json", '{"allowed": true, "decision": "ALLOW_EXPERIMENT"}\n'),
        ):
            (self.root / name).write_text(content, encoding="utf-8")
        self.profile = self._profile()
        (self.root / "profile.json").write_text(json.dumps(self.profile), encoding="utf-8")
        self.request = self._training_request()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _snapshot(self, name: str) -> dict[str, str]:
        return {
            "path": name,
            "sha256": str(fingerprint_path(self.root / name)["sha256"]),
        }

    def _profile(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "competition": {"slug": "medical-image-code-competition"},
            "rules": {
                "source": {
                    "kind": "official_rules",
                    "uri": "https://example.test/rules",
                    "observed_at": "2026-09-01T00:00:00Z",
                    "artifact_path": "rules.html",
                    "sha256": self._snapshot("rules.html")["sha256"],
                },
                "external_compute_allowed": True,
                "raw_competition_data_external_allowed": False,
                "final_inference": {
                    "backend": "kaggle-gpu",
                    "max_runtime_minutes": 540,
                    "internet_enabled": False,
                    "required_output": "submission.csv",
                },
            },
            "budgets": {
                "max_external_cost_total": 100.0,
                "max_external_cost_per_job": 25.0,
            },
            "backends": [
                {
                    "name": "local",
                    "kind": "local",
                    "priority": 10,
                    "allowed_roles": ["smoke", "eda", "package"],
                    "gpu_count": 0,
                    "vram_gb": 0,
                    "ram_gb": 16,
                    "storage_gb": 100,
                    "max_runtime_minutes": 240,
                    "max_cost_per_job": 0,
                    "availability": {
                        "available": True,
                        "remaining_runtime_minutes": 240,
                        "observed_at": "2026-09-01T00:00:00Z",
                        "artifact_path": "availability.json",
                        "sha256": self._snapshot("availability.json")["sha256"],
                    },
                },
                {
                    "name": "kaggle-gpu",
                    "kind": "kaggle",
                    "priority": 20,
                    "allowed_roles": ["smoke", "eda", "preprocess", "train", "evaluate", "inference", "package"],
                    "gpu_count": 2,
                    "vram_gb": 16,
                    "ram_gb": 29,
                    "storage_gb": 20,
                    "max_runtime_minutes": 720,
                    "max_cost_per_job": 0,
                    "availability": {
                        "available": True,
                        "remaining_runtime_minutes": 1800,
                        "observed_at": "2026-09-01T00:00:00Z",
                        "artifact_path": "availability.json",
                        "sha256": self._snapshot("availability.json")["sha256"],
                    },
                },
                {
                    "name": "external-gpu",
                    "kind": "remote_gpu",
                    "priority": 30,
                    "allowed_roles": ["preprocess", "train", "evaluate"],
                    "gpu_count": 1,
                    "vram_gb": 48,
                    "ram_gb": 64,
                    "storage_gb": 800,
                    "max_runtime_minutes": 1440,
                    "max_cost_per_job": 25.0,
                    "availability": {
                        "available": True,
                        "remaining_runtime_minutes": 1440,
                        "observed_at": "2026-09-01T00:00:00Z",
                        "artifact_path": "availability.json",
                        "sha256": self._snapshot("availability.json")["sha256"],
                    },
                },
            ],
            "dispatch_policy": {
                "microbenchmark_required_roles": ["preprocess", "train", "evaluate", "inference"],
                "utilization_limit": 0.8,
                "duplicate_job_forbidden": True,
                "checkpoint_required_after_minutes": 60,
                "artifact_hash_required": True,
            },
        }

    def _training_request(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "role": "train",
            "resources": {
                "gpu_count": 1,
                "vram_gb": 18,
                "ram_gb": 48,
                "storage_gb": 500,
                "estimated_runtime_minutes": 480,
                "max_cost": 20.0,
            },
            "data_access": {
                "raw_competition_data": False,
                "input_manifest": self._snapshot("input.json"),
            },
            "final_submission_inference": False,
            "network": {"internet_enabled": True},
            "expected_output": "model.ckpt",
            "microbenchmark": {
                "sample_count": 100,
                "evidence": self._snapshot("benchmark.json"),
            },
        }

    def _job(
        self,
        job_id: str = "expA00-fold0-attempt1",
        profile_path: str = "profile.json",
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "job_id": job_id,
            "recorded_at": "2026-09-01T00:00:00Z",
            "experiment_signature": "a" * 64,
            "work_unit": "fold0-train",
            "backend": "external-gpu",
            "status": "planned",
            "command": ["python3", "train.py", "--fold", "0"],
            "profile": self._snapshot(profile_path),
            "request": deepcopy(self.request),
            "snapshots": {
                "data": self._snapshot("data.json"),
                "code": self._snapshot("code.json"),
                "config": self._snapshot("config.json"),
                "split": self._snapshot("split.json"),
                "controller": self._snapshot("controller.json"),
            },
            "checkpoint": {
                "enabled": True,
                "interval_minutes": 30,
                "path": "checkpoints/fold0",
                "resume_from": None,
            },
            "retry_of": None,
            "retry_reason": None,
            "runtime": {
                "backend_job_id": None,
                "started_at": None,
                "finished_at": None,
                "actual_runtime_minutes": None,
                "actual_cost": None,
            },
            "artifacts": [],
            "checks": {},
        }

    def test_profile_and_microbenchmark_select_external_gpu(self) -> None:
        validate_compute_profile(self.profile, self.root)
        result = plan_compute_request(self.profile, self.request, self.root)
        self.assertEqual(result["selected_backend"], "external-gpu")
        rejected = {item["name"]: item["reasons"] for item in result["rejected_backends"]}
        self.assertIn("role_not_allowed", rejected["local"])
        self.assertIn("vram_gb_capacity_exceeded", rejected["kaggle-gpu"])

    def test_local_only_profile_does_not_require_code_submission_contract(self) -> None:
        """Non-code competitions can use compute planning without Kaggle inference."""
        profile = deepcopy(self.profile)
        profile["rules"]["final_inference"] = None
        profile["backends"] = [profile["backends"][0]]
        profile["backends"][0]["allowed_roles"].append("train")
        request = deepcopy(self.request)
        request["resources"].update(
            {"gpu_count": 0, "vram_gb": 0, "ram_gb": 4, "storage_gb": 2,
             "estimated_runtime_minutes": 60, "max_cost": 0}
        )
        self.assertEqual(plan_compute_request(profile, request, self.root)["selected_backend"], "local")
        request["role"] = "inference"
        request["final_submission_inference"] = True
        with self.assertRaisesRegex(ComputeError, "final_inference_not_configured"):
            plan_compute_request(profile, request, self.root)

    def test_generic_templates_plan_local_work_without_kaggle(self) -> None:
        """The shipped neutral templates must not require a Kaggle backend."""
        templates = Path(__file__).resolve().parents[1] / "automation"
        profile = json.loads((templates / "compute_profiles.template.json").read_text())
        request = json.loads((templates / "compute_request.template.json").read_text())
        profile["rules"]["source"].update(
            {"artifact_path": "rules.html", "sha256": self._snapshot("rules.html")["sha256"]}
        )
        profile["backends"][0]["availability"].update(
            {"artifact_path": "availability.json", "sha256": self._snapshot("availability.json")["sha256"]}
        )
        request["data_access"]["input_manifest"] = self._snapshot("input.json")
        request["microbenchmark"]["evidence"] = self._snapshot("benchmark.json")
        self.assertEqual(plan_compute_request(profile, request, self.root)["selected_backend"], "local")

    def test_raw_competition_data_cannot_be_sent_to_external_worker(self) -> None:
        request = deepcopy(self.request)
        request["data_access"]["raw_competition_data"] = True
        with self.assertRaisesRegex(ComputeError, "raw_competition_data_external_forbidden"):
            plan_compute_request(self.profile, request, self.root)

    def test_required_microbenchmark_cannot_be_omitted(self) -> None:
        request = deepcopy(self.request)
        del request["microbenchmark"]
        with self.assertRaisesRegex(ComputeError, "materialized microbenchmark"):
            plan_compute_request(self.profile, request, self.root)

    def test_unavailable_kaggle_backend_is_excluded_by_evidence(self) -> None:
        request = deepcopy(self.request)
        request["resources"].update(
            {"vram_gb": 8, "ram_gb": 16, "storage_gb": 10, "estimated_runtime_minutes": 120}
        )
        profile = deepcopy(self.profile)
        profile["backends"][1]["availability"]["available"] = False
        result = plan_compute_request(profile, request, self.root)
        self.assertEqual(result["selected_backend"], "external-gpu")
        rejected = {item["name"]: item["reasons"] for item in result["rejected_backends"]}
        self.assertIn("backend_unavailable", rejected["kaggle-gpu"])

    def test_final_submission_inference_is_forced_to_kaggle_contract(self) -> None:
        request = deepcopy(self.request)
        request.update(
            {
                "role": "inference",
                "resources": {
                    "gpu_count": 1,
                    "vram_gb": 8,
                    "ram_gb": 16,
                    "storage_gb": 10,
                    "estimated_runtime_minutes": 400,
                    "max_cost": 0,
                },
                "final_submission_inference": True,
                "network": {"internet_enabled": False},
                "expected_output": "submission.csv",
            }
        )
        result = plan_compute_request(self.profile, request, self.root)
        self.assertEqual(result["selected_backend"], "kaggle-gpu")
        request["network"]["internet_enabled"] = True
        with self.assertRaisesRegex(ComputeError, "final_inference_network_mismatch"):
            plan_compute_request(self.profile, request, self.root)

    def test_long_job_requires_checkpoint(self) -> None:
        job = self._job()
        job["checkpoint"]["enabled"] = False
        job["checkpoint"]["interval_minutes"] = 0
        with self.assertRaisesRegex(ComputeError, "checkpoint.enabled"):
            validate_compute_job(job, self.profile, self.root)

    def test_controller_denial_and_profile_drift_block_dispatch(self) -> None:
        denied_path = self.root / "controller-denied.json"
        denied_path.write_text('{"allowed": false, "decision": "DUPLICATE_EXPERIMENT"}\n', encoding="utf-8")
        denied_job = self._job()
        denied_job["snapshots"]["controller"] = self._snapshot("controller-denied.json")
        with self.assertRaisesRegex(ComputeError, "allowed=true"):
            validate_compute_job(denied_job, self.profile, self.root)

        changed_profile = deepcopy(self.profile)
        changed_profile["budgets"]["max_external_cost_total"] = 99.0
        with self.assertRaisesRegex(ComputeError, "does not match the job profile snapshot"):
            validate_compute_job(self._job(), changed_profile, self.root)

    def test_duplicate_dispatch_is_rejected_but_infra_retry_is_allowed(self) -> None:
        ledger = self.root / "compute_jobs.jsonl"
        planned = self._job()
        append_compute_job(ledger, planned, self.profile, self.root)

        dispatched = deepcopy(planned)
        dispatched["status"] = "dispatched"
        dispatched["recorded_at"] = "2026-09-01T00:01:00Z"
        dispatched["runtime"]["backend_job_id"] = "pod-1"
        append_compute_job(ledger, dispatched, self.profile, self.root)

        duplicate = self._job("expA00-fold0-attempt2")
        with self.assertRaisesRegex(ComputeError, "duplicate compute job"):
            append_compute_job(ledger, duplicate, self.profile, self.root)

        running = deepcopy(dispatched)
        running["status"] = "running"
        running["recorded_at"] = "2026-09-01T00:02:00Z"
        append_compute_job(ledger, running, self.profile, self.root)

        failed = deepcopy(running)
        failed["status"] = "infra_failed"
        failed["recorded_at"] = "2026-09-01T01:00:00Z"
        failed["runtime"].update(
            {
                "started_at": "2026-09-01T00:02:00Z",
                "finished_at": "2026-09-01T01:00:00Z",
                "actual_runtime_minutes": 58,
                "actual_cost": 5.0,
            }
        )
        append_compute_job(ledger, failed, self.profile, self.root)

        retry = self._job("expA00-fold0-attempt2")
        retry["retry_of"] = "expA00-fold0-attempt1"
        retry["retry_reason"] = "provider preempted the first worker"
        result = append_compute_job(ledger, retry, self.profile, self.root)
        self.assertEqual(result["job_status"], "planned")

        third_attempt = self._job("expA00-fold0-attempt3")
        third_attempt["retry_of"] = "expA00-fold0-attempt1"
        third_attempt["retry_reason"] = "incorrectly retrying an old failure after a live retry"
        with self.assertRaisesRegex(ComputeError, "duplicate compute job"):
            append_compute_job(ledger, third_attempt, self.profile, self.root)

    def test_terminal_cost_overrun_is_recorded_and_blocks_next_dispatch(self) -> None:
        ledger = self.root / "budget_jobs.jsonl"
        profile = deepcopy(self.profile)
        profile["budgets"]["max_external_cost_total"] = 10.0
        (self.root / "budget-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        planned = self._job(profile_path="budget-profile.json")
        planned["request"]["resources"]["max_cost"] = 10.0
        append_compute_job(ledger, planned, profile, self.root)

        dispatched = deepcopy(planned)
        dispatched["status"] = "dispatched"
        dispatched["recorded_at"] = "2026-09-01T00:01:00Z"
        dispatched["runtime"]["backend_job_id"] = "pod-budget"
        append_compute_job(ledger, dispatched, profile, self.root)
        running = deepcopy(dispatched)
        running["status"] = "running"
        running["recorded_at"] = "2026-09-01T00:02:00Z"
        append_compute_job(ledger, running, profile, self.root)

        failed = deepcopy(running)
        failed["status"] = "infra_failed"
        failed["recorded_at"] = "2026-09-01T01:00:00Z"
        failed["runtime"].update(
            {
                "started_at": "2026-09-01T00:02:00Z",
                "finished_at": "2026-09-01T01:00:00Z",
                "actual_runtime_minutes": 58,
                "actual_cost": 12.0,
            }
        )
        overrun = append_compute_job(ledger, failed, profile, self.root)
        self.assertEqual(overrun["external_cost_budget_status"], "EXCEEDED")

        retry = self._job("expA00-fold0-attempt2", profile_path="budget-profile.json")
        retry["request"]["resources"]["max_cost"] = 10.0
        retry["retry_of"] = "expA00-fold0-attempt1"
        retry["retry_reason"] = "retry after provider failure"
        with self.assertRaisesRegex(ComputeError, "cost commitment"):
            append_compute_job(ledger, retry, profile, self.root)

    def test_validated_job_requires_declared_output_artifact(self) -> None:
        """A partial checkpoint cannot become a validated worker result."""
        job = self._job()
        job["status"] = "validated"
        job["runtime"].update(
            {
                "backend_job_id": "worker-1",
                "started_at": "2026-09-01T00:00:00Z",
                "finished_at": "2026-09-01T01:00:00Z",
                "actual_runtime_minutes": 60,
                "actual_cost": 1.0,
            }
        )
        job["artifacts"] = [self._snapshot("data.json")]
        job["checks"] = {
            "artifact": {"status": "PASS", "evidence": self._snapshot("artifact-check.json")},
            "reproducibility": {"status": "PASS", "evidence": self._snapshot("replay-check.json")},
        }
        with self.assertRaisesRegex(ComputeError, "missing expected_output artifact"):
            validate_compute_job(job, self.profile, self.root)
        job["artifacts"].append(self._snapshot("model.ckpt"))
        self.assertEqual(validate_compute_job(job, self.profile, self.root)["job_status"], "validated")

    def test_deadline_requires_full_critical_path_not_just_gpu_quota(self) -> None:
        """Training that fits quota but not scoring and submission is blocked."""
        request = deepcopy(self.request)
        request["timeline"] = {
            "observed_at": "2026-09-30T06:00:00Z",
            "deadline_at": "2026-09-30T08:00:00Z",
            "downstream_minutes": 90,
            "safety_minutes": 10,
            "deadline_source": self._snapshot("rules.html"),
        }
        with self.assertRaisesRegex(ComputeError, "critical_path_exceeds_deadline"):
            plan_compute_request(self.profile, request, self.root)
        request["timeline"]["deadline_at"] = "2026-10-01T08:00:00Z"
        self.assertEqual(plan_compute_request(self.profile, request, self.root)["selected_backend"], "external-gpu")

    def test_cli_compute_plan_returns_machine_selected_backend(self) -> None:
        profile_path = self.root / "profile.json"
        request_path = self.root / "request.json"
        profile_path.write_text(json.dumps(self.profile), encoding="utf-8")
        request_path.write_text(json.dumps(self.request), encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "compute-plan",
                    str(profile_path),
                    str(request_path),
                    "--root",
                    str(self.root),
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["selected_backend"], "external-gpu")


if __name__ == "__main__":
    unittest.main()
