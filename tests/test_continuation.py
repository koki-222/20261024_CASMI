"""Integration regressions for useful work during external scoring waits."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
from threading import Event
from typing import Any
from unittest import TestCase
from unittest.mock import patch

import test_runtime as runtime_fixture
import test_validation as validation_fixture

from tools.competition_automation import runtime
from tools.competition_automation.cli import main
from tools.competition_automation.continuation import (
    ContinuationError,
    plan_continuation,
)
from tools.competition_automation.controller import evaluate_controller_state
from tools.competition_automation.fingerprint import canonical_json_digest, sha256_file
from tools.competition_automation.runtime import RuntimeError, run_experiment
from tools.competition_automation.signature import (
    computation_payload,
    computation_signature,
)

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
FUTURE = "2099-10-16T06:15:00Z"


class ContinuationTests(TestCase):
    """Exercise real claims, artifact reuse, scoped blockers and wait gates."""

    def setUp(self) -> None:
        """Reuse the authorized no-network runtime fixture and add strict policy."""
        self.fixture = runtime_fixture.RuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.registry = self.fixture.registry
        self.requests = {"science": self.request("science", 1)}
        self.queue: dict[str, Any] = {
            "schema_version": 1,
            "resources": {"cpu": 1, "gpu": 1},
            "jobs": [self.job("science", "hypothesis", {"cpu": 1})],
        }
        intake = self.snapshot(
            "intake.json",
            {
                "schema_version": 1,
                "issues": [
                    {
                        "source_issue": {"number": 1, "snapshot": self.fixture.issue},
                        "scope_ids": ["scope-a"],
                    }
                ],
            },
        )
        self.plan: dict[str, Any] = {
            "schema_version": 1,
            "intake": intake,
            "queue": None,
            "scopes": [{"id": "scope-a", "job_ids": ["science"], "resolution": None}],
            "work_items": [
                {
                    "job_id": "science",
                    "scope_id": "scope-a",
                    "request": None,
                    "blockers": [],
                }
            ],
        }
        self.save()

    def snapshot(self, name: str, value: Any) -> dict[str, str]:
        """Save one finite test fixture and return its path/hash binding."""
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(
            json.dumps(value, allow_nan=False), encoding="utf-8"
        )
        return {"path": name, "sha256": sha256_file(self.root / name)}

    def request(self, name: str, value: int) -> dict[str, Any]:
        """Build an independently frozen controller and computation request."""
        request = deepcopy(self.fixture.request)
        request["output_dir"] = f"outputs/{name}"
        request["computation_spec"]["config"]["x"] = value
        spec = request["computation_spec"]
        payload = computation_payload(spec)
        controller = deepcopy(self.fixture.controller)
        action = controller["action"]
        action.update(
            {
                "computation_spec": deepcopy(spec),
                "hypothesis_signature": computation_signature(spec, True),
                "mechanism_signature": canonical_json_digest(
                    {
                        key: payload[key]
                        for key in ("task_type", "code", "config", "evaluation")
                    }
                ),
                "input_signature": canonical_json_digest(
                    {key: payload[key] for key in ("data", "split")}
                ),
                "run_snapshot": {
                    key: self.snapshot(f"requests/{name}/{key}.json", payload[key])
                    for key in ("data", "code", "config", "split")
                },
            }
        )
        request["controller"] = self.snapshot(
            f"requests/{name}/controller.json", controller
        )
        return request

    def job(
        self, name: str, kind: str, resources: dict[str, int] | None = None
    ) -> dict[str, Any]:
        """Build one queue job without implicit scientific authorization."""
        return {
            "id": name,
            "kind": kind,
            "status": "ready",
            "depends_on": [],
            "resources": resources or {},
            "estimated_seconds": 1,
            "priority": 0,
        }

    def save(self) -> None:
        """Freeze the shared queue before requests, then the canonical policy."""
        self.plan["queue"] = self.snapshot("automation/work_queue.json", self.queue)
        for item in self.plan["work_items"]:
            key = item["job_id"]
            if key in self.requests:
                request = self.requests[key]
                request.update({"queue": self.plan["queue"], "queue_job_id": key})
                item["request"] = self.snapshot(f"requests/{key}/request.json", request)
        self.snapshot("automation/continuation.json", self.plan)

    def evaluate(self) -> dict[str, Any]:
        """Plan one cycle at a fixed clock without external I/O."""
        return plan_continuation(self.plan, self.root, NOW)

    def blocker(
        self, key: str, kind: str = "score", next_check: str = FUTURE
    ) -> dict[str, str]:
        """Record an external condition limited to one job."""
        return self.snapshot(
            f"blockers/{key}.json",
            {
                "kind": kind,
                "state": "waiting",
                "affected_job_ids": [key],
                "reason": "The declared comparison requires its official parent result.",
                "resume_condition": "Observe the matching official result.",
                "next_check_at": next_check,
            },
        )

    def add_other_scope(self) -> None:
        """Add an independently authorized untested scope with its own request."""
        self.requests["other"] = self.request("other", 2)
        self.queue["jobs"].append(self.job("other", "hypothesis", {"cpu": 1}))
        intake = json.loads((self.root / "intake.json").read_text())
        intake["issues"][0]["scope_ids"].append("scope-b")
        self.plan["intake"] = self.snapshot("intake.json", intake)
        self.plan["scopes"].append({"id": "scope-b", "job_ids": ["other"]})
        self.plan["work_items"].append({"job_id": "other", "scope_id": "scope-b"})
        self.save()

    def add_maintenance(self, cached: bool) -> None:
        """Attach a real validation plan, optionally with an existing receipt."""
        (self.root / "input.txt").write_text("input", encoding="utf-8")
        validation = validation_fixture._plan(self.root)
        if cached:
            validation["receipts"] = [
                validation_fixture._receipt(self.root, validation)
            ]
        self.queue["jobs"].append(self.job("audit", "maintenance", {"cpu": 1}))
        self.queue["jobs"][-1]["priority"] = 999
        self.plan["work_items"].append(
            {
                "job_id": "audit",
                "scope_id": None,
                "validation": {
                    "plan": self.snapshot("validation.json", validation),
                    "check_id": "smoke",
                },
            }
        )
        self.save()

    def resolve(self, scope_id: str = "scope-a") -> None:
        """Interpret verified outputs without claiming adoption or an LB gain."""
        result = self.evaluate()
        artifacts = [
            artifact
            for item in result["work_items"]
            if item["scope_id"] == scope_id and item["kind"] == "hypothesis"
            for artifact in item["artifacts"]
        ]
        scope = next(value for value in self.plan["scopes"] if value["id"] == scope_id)
        scope["resolution"] = self.snapshot(
            f"resolution-{scope_id}.json",
            {
                "scope_id": scope_id,
                "source_issue": {"number": 1, "snapshot": self.fixture.issue},
                "outcome": "inconclusive",
                "reason": "Fixed diagnostic finished; no official-score claim.",
                "artifacts": artifacts,
            },
        )
        self.save()

    def test_ready_science_denies_wait_without_creating_registry(self) -> None:
        result = self.evaluate()
        self.assertFalse(result["can_wait"])
        self.assertEqual(result["selected_jobs"], ["science"])
        self.assertFalse(self.registry.exists())

    def test_omitted_authorized_scope_is_rejected(self) -> None:
        self.plan["scopes"] = []
        with self.assertRaisesRegex(ContinuationError, "coverage mismatch"):
            self.evaluate()

    def test_empty_queue_is_not_proof_of_no_research(self) -> None:
        self.queue["jobs"] = []
        self.requests = {}
        self.plan["work_items"] = []
        self.plan["scopes"][0]["job_ids"] = []
        self.save()
        result = self.evaluate()
        self.assertEqual(result["decision"], "NEEDS_EXECUTABLE_PLAN")
        self.assertFalse(result["can_wait"])

    def test_finished_computation_needs_interpretation_then_can_wait(self) -> None:
        run_experiment(self.requests["science"], self.root, self.registry)
        result = self.evaluate()
        self.assertEqual(result["decision"], "INTERPRET_RESULTS")
        self.assertFalse(result["can_wait"])
        self.assertEqual(result["work_items"][0]["decision"], "REUSE_COMPLETED")
        self.resolve()
        result = self.evaluate()
        self.assertTrue(result["can_wait"])
        self.assertEqual(result["progress"]["resolved_scientific_scopes"], 1)
        self.assertFalse(result["goal_achieved"])

    def test_renamed_completed_computation_is_reused_not_reselected(self) -> None:
        run_experiment(self.requests["science"], self.root, self.registry)
        self.queue["jobs"][0]["id"] = "renamed"
        self.requests["renamed"] = self.requests.pop("science")
        self.requests["renamed"]["output_dir"] = "outputs/renamed"
        self.plan["work_items"][0]["job_id"] = "renamed"
        self.plan["scopes"][0]["job_ids"] = ["renamed"]
        self.save()
        result = self.evaluate()
        self.assertEqual(result["selected_jobs"], [])
        self.assertEqual(result["work_items"][0]["decision"], "REUSE_COMPLETED")
        self.assertFalse((self.root / "outputs/renamed").exists())

    def test_forged_completed_label_does_not_hide_untested_work(self) -> None:
        self.queue["jobs"][0]["status"] = "completed"
        self.save()
        with self.assertRaisesRegex(ContinuationError, "authoritative runtime"):
            self.evaluate()

    def test_changed_completed_output_invalidates_reuse(self) -> None:
        run_experiment(self.requests["science"], self.root, self.registry)
        (self.root / "outputs/science/result.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "completed output changed"):
            self.evaluate()

    def test_one_finished_scope_does_not_close_another(self) -> None:
        self.add_other_scope()
        self.queue["jobs"][0]["priority"] = 1
        self.save()
        run_experiment(self.requests["science"], self.root, self.registry)
        self.resolve()
        result = self.evaluate()
        self.assertFalse(result["can_wait"])
        self.assertEqual(result["selected_jobs"], ["other"])

    def test_scoped_score_wait_does_not_block_other_scope(self) -> None:
        self.add_other_scope()
        self.plan["work_items"][0]["blockers"] = [self.blocker("science")]
        self.save()
        result = self.evaluate()
        self.assertEqual(result["selected_jobs"], ["other"])
        self.assertFalse(result["can_wait"])

    def test_quota_cannot_block_local_scientific_work(self) -> None:
        self.plan["work_items"][0]["blockers"] = [self.blocker("science", "quota")]
        self.save()
        with self.assertRaisesRegex(ContinuationError, "quota cannot block local"):
            self.evaluate()

    def test_genuine_scoped_external_wait_is_allowed_until_due(self) -> None:
        self.plan["work_items"][0]["blockers"] = [self.blocker("science")]
        self.save()
        self.assertTrue(self.evaluate()["can_wait"])
        due = plan_continuation(
            self.plan, self.root, datetime(2099, 10, 17, tzinfo=timezone.utc)
        )
        self.assertFalse(due["can_wait"])
        self.assertEqual(due["decision"], "RECHECK_SCOPED_BLOCKER")

    def test_due_monitor_and_local_work_are_both_planned(self) -> None:
        monitor = self.job("score-monitor", "monitor")
        monitor.update({"status": "waiting", "next_check_at": "2026-10-15T00:00:00Z"})
        self.queue["jobs"].append(monitor)
        self.plan["work_items"].append(
            {
                "job_id": "score-monitor",
                "scope_id": None,
                "blockers": [
                    self.blocker("score-monitor", next_check="2026-10-15T00:00:00Z")
                ],
            }
        )
        self.save()
        result = self.evaluate()
        self.assertEqual(result["selected_jobs"], ["science"])
        self.assertEqual(result["monitor_jobs"], ["score-monitor"])
        self.assertEqual(result["monitor_timeout_seconds"], 45)

    def test_high_priority_maintenance_does_not_displace_science(self) -> None:
        self.add_maintenance(cached=False)
        result = self.evaluate()
        self.assertEqual(result["selected_jobs"], ["science"])
        self.assertEqual(result["progress"]["resolved_scientific_scopes"], 0)

    def test_cached_check_is_not_repeated_or_counted_as_science(self) -> None:
        self.add_maintenance(cached=True)
        result = self.evaluate()
        self.assertEqual(result["selected_jobs"], ["science"])
        self.assertEqual(result["progress"]["maintenance_completed"], 1)
        self.assertEqual(result["progress"]["resolved_scientific_scopes"], 0)
        self.assertFalse(result["can_wait"])

    def test_strict_runtime_rejects_unqueued_request(self) -> None:
        request = deepcopy(self.requests["science"])
        request.pop("queue")
        request.pop("queue_job_id")
        with self.assertRaisesRegex(ContinuationError, "did not select"):
            run_experiment(request, self.root, self.registry)
        self.assertFalse((self.root / request["output_dir"]).exists())

    def test_strict_runtime_rejects_modified_frozen_request(self) -> None:
        request = deepcopy(self.requests["science"])
        request["output_dir"] = "outputs/unregistered"
        with self.assertRaisesRegex(ContinuationError, "differs from the frozen"):
            run_experiment(request, self.root, self.registry)

    def test_duplicate_computations_in_queue_are_rejected(self) -> None:
        self.add_other_scope()
        self.requests["other"]["computation_spec"] = deepcopy(
            self.requests["science"]["computation_spec"]
        )
        self.requests["other"]["controller"] = self.requests["science"]["controller"]
        self.save()
        with self.assertRaisesRegex(ContinuationError, "duplicate computation"):
            self.evaluate()

    def test_independent_cpu_runs_while_gpu_claim_is_active(self) -> None:
        self.queue["jobs"][0]["resources"] = {"gpu": 1}
        self.add_other_scope()
        started = Event()
        original = runtime.subprocess.Popen

        def launch(*args: Any, **kwargs: Any) -> Any:
            """Signal only after the real process and durable claim exist."""
            process = original(*args, **kwargs)
            started.set()
            return process

        with patch.object(runtime.subprocess, "Popen", side_effect=launch):
            with ThreadPoolExecutor(max_workers=2) as pool:
                gpu = pool.submit(
                    run_experiment, self.requests["science"], self.root, self.registry
                )
                self.assertTrue(started.wait(3))
                during = self.evaluate()
                self.assertEqual(during["selected_jobs"], ["other"])
                self.assertEqual(during["queue_plan"]["resource_usage"]["gpu"], 1)
                cpu = run_experiment(self.requests["other"], self.root, self.registry)
                self.assertEqual(cpu["status"], "completed")
                self.assertEqual(gpu.result(timeout=5)["status"], "completed")

    def test_wait_cli_and_controller_refuse_idle_with_ready_science(self) -> None:
        with redirect_stdout(StringIO()):
            code = main(
                [
                    "wait-check",
                    str(self.root / "automation/continuation.json"),
                    "--root",
                    str(self.root),
                    "--now",
                    NOW.isoformat(),
                ]
            )
        self.assertEqual(code, 1)
        state = deepcopy(self.fixture.controller)
        state["action"] = {"type": "wait"}
        self.assertFalse(evaluate_controller_state(state, self.root, NOW)["allowed"])

    def test_controller_scoped_blocker_does_not_freeze_another_job(self) -> None:
        state = deepcopy(self.fixture.controller)
        fingerprint = evaluate_controller_state(state, self.root, NOW)[
            "state_fingerprint"
        ]
        state["blocked_state"] = {
            "active": True,
            "reason": "Wait only for one official contrast",
            "resume_condition": "matching score arrives",
            "state_fingerprint": fingerprint,
            "next_check_at": FUTURE,
            "affected_job_ids": ["score-dependent"],
        }
        self.assertTrue(
            evaluate_controller_state(state, self.root, NOW, job_id="science")[
                "allowed"
            ]
        )
        self.assertFalse(
            evaluate_controller_state(state, self.root, NOW, job_id="score-dependent")[
                "allowed"
            ]
        )
        self.assertEqual(
            evaluate_controller_state(state, self.root, NOW)["decision"],
            "JOB_CONTEXT_REQUIRED",
        )

    def test_incomplete_interpretation_cannot_claim_official_adoption(self) -> None:
        run_experiment(self.requests["science"], self.root, self.registry)
        self.resolve()
        path = self.plan["scopes"][0]["resolution"]["path"]
        result = json.loads((self.root / path).read_text())
        result["outcome"] = "adopted"
        self.plan["scopes"][0]["resolution"] = self.snapshot(path, result)
        with self.assertRaisesRegex(ContinuationError, "not adoption"):
            self.evaluate()

    def test_stale_or_escaping_snapshot_fails_closed(self) -> None:
        self.plan["queue"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
            self.evaluate()
        self.plan["queue"] = {"path": "../outside.json", "sha256": "0" * 64}
        with self.assertRaisesRegex(RuntimeError, "escapes root"):
            self.evaluate()

    def test_future_monitor_is_not_polled_early(self) -> None:
        monitor = self.job("score-monitor", "monitor")
        monitor.update({"status": "waiting", "next_check_at": FUTURE})
        self.queue["jobs"].append(monitor)
        self.plan["work_items"].append(
            {
                "job_id": "score-monitor",
                "scope_id": None,
                "blockers": [self.blocker("score-monitor")],
            }
        )
        self.save()
        self.assertEqual(self.evaluate()["monitor_jobs"], [])
        self.queue["jobs"][-1]["next_check_at"] = "2026-10-15T00:00:00Z"
        self.save()
        with self.assertRaisesRegex(ContinuationError, "must match"):
            self.evaluate()

    def test_family_closure_only_blocks_its_named_work(self) -> None:
        self.add_other_scope()
        self.plan["work_items"][0]["blockers"] = [self.blocker("science", "family")]
        self.save()
        self.assertEqual(self.evaluate()["selected_jobs"], ["other"])

    def test_prerequisite_completion_does_not_resolve_scientific_scope(self) -> None:
        self.queue["jobs"][0]["kind"] = "prerequisite"
        self.save()
        run_experiment(self.requests["science"], self.root, self.registry)
        self.resolve()
        with self.assertRaisesRegex(ContinuationError, "scientific outputs"):
            self.evaluate()

    def test_maintenance_cannot_be_assigned_to_a_scientific_scope(self) -> None:
        self.add_maintenance(cached=True)
        self.plan["work_items"][-1]["scope_id"] = "scope-a"
        self.plan["scopes"][0]["job_ids"].append("audit")
        self.save()
        with self.assertRaisesRegex(ContinuationError, "maintenance checks cannot"):
            self.evaluate()

    def test_closed_controller_is_reported_without_repeated_dispatch(self) -> None:
        request = self.requests["science"]
        path = request["controller"]["path"]
        controller = json.loads((self.root / path).read_text())
        fingerprint = evaluate_controller_state(controller, self.root, NOW)[
            "state_fingerprint"
        ]
        controller["blocked_state"] = {
            "active": True,
            "reason": "A global input restriction is unresolved",
            "resume_condition": "authorized source becomes available",
            "state_fingerprint": fingerprint,
            "next_check_at": FUTURE,
        }
        request["controller"] = self.snapshot(path, controller)
        self.save()
        result = self.evaluate()
        self.assertEqual(result["selected_jobs"], [])
        self.assertFalse(result["can_wait"])
        self.assertEqual(
            result["unresolved_work"][0]["reason"], "RESUME_CHECK_REQUIRED"
        )

    def test_actual_empty_intake_waits_for_user_without_new_hypotheses(self) -> None:
        self.plan["intake"] = self.snapshot(
            "intake-empty.json", {"schema_version": 1, "issues": []}
        )
        self.plan["scopes"] = []
        self.plan["work_items"] = []
        self.queue["jobs"] = []
        self.requests = {}
        self.save()
        result = self.evaluate()
        self.assertTrue(result["can_wait"])
        self.assertEqual(result["decision"], "WAITING_FOR_USER_ISSUE")
        with redirect_stdout(StringIO()):
            self.assertEqual(
                main(
                    [
                        "wait-check",
                        str(self.root / "automation/continuation.json"),
                        "--root",
                        str(self.root),
                    ]
                ),
                0,
            )

    def test_scoped_controller_cannot_use_an_unqueued_job_context(self) -> None:
        request = deepcopy(self.requests["science"])
        request.pop("queue")
        with self.assertRaisesRegex(RuntimeError, "supplied together"):
            run_experiment(request, self.root, self.registry)

    def test_invalid_blocker_timestamp_is_a_contract_error(self) -> None:
        self.plan["work_items"][0]["blockers"] = [
            self.blocker("science", next_check="tomorrow")
        ]
        self.save()
        with self.assertRaisesRegex(ContinuationError, "ISO-8601"):
            self.evaluate()

    def test_wait_cli_reports_malformed_coverage_as_error(self) -> None:
        self.plan["scopes"] = []
        self.save()
        with redirect_stdout(StringIO()):
            code = main(
                [
                    "wait-check",
                    str(self.root / "automation/continuation.json"),
                    "--root",
                    str(self.root),
                ]
            )
        self.assertEqual(code, 2)

    def test_completed_scope_does_not_recheck_obsolete_blocker(self) -> None:
        run_experiment(self.requests["science"], self.root, self.registry)
        self.resolve()
        self.plan["work_items"][0]["blockers"] = [
            self.blocker("science", next_check="2026-10-15T00:00:00Z")
        ]
        self.save()
        result = self.evaluate()
        self.assertTrue(result["can_wait"])
        self.assertIsNone(result["next_check_at"])
