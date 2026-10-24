"""End-to-end regressions for guarded execution and legacy identity migration."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Event
from unittest.mock import patch

from tools.competition_automation import runtime
from tools.competition_automation.fidelity import create_checkpoint_manifest
from tools.competition_automation.fingerprint import canonical_json_digest, sha256_file
from tools.competition_automation.ledger import LedgerError, append_record
from tools.competition_automation.record import validate_record
from tools.competition_automation.runtime import (
    RuntimeError,
    registry_status,
    run_experiment,
)
from tools.competition_automation.signature import (
    computation_payload,
    computation_signature,
    experiment_signature,
    legacy_signature,
)
from tools.competition_automation.state import StateError, compact_state


class RuntimeTests(unittest.TestCase):
    """Exercise real subprocesses, atomic claims, frozen evidence and reuse."""

    def setUp(self) -> None:
        """Create a small fully authorized task without any external service."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "automation/runs.sqlite"
        self.root.joinpath("runner.py").write_text(
            "import json,sys,time\nfrom pathlib import Path\ntime.sleep(0.15)\n"
            "config=json.loads(Path(sys.argv[1]).read_text())\n"
            "Path(sys.argv[2]).joinpath('result.json').write_text(json.dumps({'value': config['x']}))\n",
            encoding="utf-8",
        )
        self.snapshot("data.json", {"rows": [1, 2]})
        self.snapshot("split.json", {"train": [1], "validation": [2], "seed": 3})
        self.issue = self.snapshot(
            "issue.json",
            {
                "number": 1,
                "url": "https://github.com/user/demo/issues/1",
                "state": "OPEN",
                "author": {"login": "user"},
                "title": "Test mechanism",
                "body": "Compare the fixed transform.",
                "updatedAt": "2026-10-15T00:00:00Z",
            },
        )
        self.spec = {
            "schema_version": 1,
            "task_type": "diagnostic",
            "code": {"main": self.evidence("runner.py")},
            "data": {"train": self.evidence("data.json")},
            "split": {"fold": self.evidence("split.json")},
            "config": {"x": 1, "model": {"seed": 3}},
            "seed": 3,
            "budget": {"unit": "iterations", "units": 1},
            "evaluation": {"metric": "score", "direction": "max"},
            "environment": {"python": sys.version},
            "command": [sys.executable, "{code.main}", "{config_path}", "{output_dir}"],
        }
        contract = self.snapshot(
            "contract.json",
            {
                "schema_version": 1,
                "competition": {"name": "demo", "slug": "demo"},
                "platform": {"kind": "custom"},
                "data": {},
                "submission": {"format": "csv", "columns": ["id", "prediction"]},
                "evaluation": {"metric": "score", "direction": "max"},
                "split": {"strategy": "holdout"},
                "research": {"required_sections": ["rules"], "minimum_sources": 1},
                "policy": {},
            },
        )
        rules = self.snapshot("rules.json", {"rules": "Local diagnostic only"})
        research = self.snapshot(
            "research.json",
            {
                "schema_version": 1,
                "sections": {
                    "rules": {
                        "sources": [
                            {
                                "kind": "official",
                                "uri": "https://example.test/rules",
                                "observed_at": "2026-10-15T00:00:00Z",
                                "artifact_path": rules["path"],
                                "sha256": rules["sha256"],
                            }
                        ]
                    }
                },
            },
        )
        exploration = self.snapshot(
            "exploration.json",
            {
                "schema_version": 1,
                "source_issue": {"number": 1, "snapshot": self.issue},
                "capabilities": ["ml"],
                "objective": {
                    "metric": "score",
                    "direction": "max",
                    "mechanism": ["representation"],
                },
                "authorized_axes": [
                    {
                        "id": "representation",
                        "kind": "mechanism",
                        "budget": 3,
                        "families": ["transform"],
                        "status": "untested",
                        "objective_link": "Improve representation",
                    }
                ],
                "families": [
                    {
                        "id": "transform",
                        "axis": "representation",
                        "budget": 3,
                        "observed_attempts": 0,
                    }
                ],
                "restrictions": [],
                "rl": None,
            },
        )
        self.request = {
            "schema_version": 1,
            "computation_spec": self.spec,
            "output_dir": "outputs/first",
            "outputs": ["result.json"],
            "timeout_seconds": 5,
            "preflight": {"contract": contract, "research": research},
            "exploration": exploration,
        }
        self.bind()

    def evidence(self, name: str) -> dict[str, str]:
        """Fingerprint a fixture artifact."""
        return {"path": name, "sha256": sha256_file(self.root / name)}

    def snapshot(self, name: str, value: object) -> dict[str, str]:
        """Write a source snapshot as a fixture, then hash it."""
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")
        return self.evidence(name)

    def bind(self) -> None:
        """Bind controller action fields to the exact computation fixture."""
        payload = computation_payload(self.spec)
        snapshot = self.evidence("data.json")
        self.controller = {
            "schema_version": 1,
            "state_inputs": [snapshot, self.issue],
            "hypothesis_source": {
                "mode": "user_github_issue",
                "repository": "user/demo",
                "allowed_authors": ["user"],
            },
            "families": [],
            "experiment_history": [],
            "action": {
                "type": "experiment",
                "family": "transform",
                "change_scope": "hypothesis",
                "hypothesis_signature": computation_signature(self.spec, True),
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
                    key: self.snapshot(f"run-{key}.json", payload[key])
                    for key in ("data", "code", "config", "split")
                },
                "source_issue": {"number": 1, "snapshot": self.issue},
                "computation_spec": deepcopy(self.spec),
            },
        }
        self.request["controller"] = self.snapshot("controller.json", self.controller)

    def run_request(self) -> dict[str, object]:
        """Execute the frozen local request."""
        return run_experiment(self.request, self.root, self.registry)

    def test_runs_once_and_reuses_renamed_output(self) -> None:
        result = self.run_request()
        self.assertEqual(result["status"], "completed")
        self.request["output_dir"] = "outputs/renamed"
        second = self.run_request()
        self.assertEqual(second["decision"], "REUSE_COMPLETED")
        self.assertFalse((self.root / "outputs/renamed").exists())
        self.assertEqual(registry_status(self.registry)["counts"], {"completed": 1})

    def test_concurrent_claims_execute_only_once(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_request(), range(2)))
        self.assertEqual(sum(result["decision"] == "EXECUTED" for result in results), 1)
        self.assertIn(
            next(
                result["decision"]
                for result in results
                if result["decision"] != "EXECUTED"
            ),
            {"JOIN_RUNNING", "REUSE_COMPLETED"},
        )
        self.assertEqual(len(registry_status(self.registry)["attempts"]), 1)

    def test_audit_cannot_authorize_computation(self) -> None:
        self.controller["action"] = {"type": "audit"}
        self.request["controller"] = self.snapshot("controller.json", self.controller)
        with self.assertRaisesRegex(RuntimeError, "action.type=experiment"):
            self.run_request()
        self.assertFalse((self.root / "outputs").exists())

    def test_changed_command_or_input_cannot_reuse_authorization(self) -> None:
        self.spec["command"].append("unexpected")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            self.run_request()
        self.spec["command"].pop()
        (self.root / "data.json").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
            self.run_request()

    def test_corrupt_completed_output_cannot_be_reused(self) -> None:
        self.run_request()
        (self.root / "outputs/first/result.json").write_text("broken", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "completed output changed"):
            self.run_request()

    def test_nested_seed_change_requires_control(self) -> None:
        self.run_request()
        self.spec["seed"] = 9
        self.spec["config"]["model"]["seed"] = 9
        self.request["output_dir"] = "outputs/seed"
        self.bind()
        with self.assertRaisesRegex(RuntimeError, "seed-only"):
            self.run_request()

    def test_real_semantic_change_runs(self) -> None:
        self.run_request()
        self.spec["config"]["x"] = 2
        self.request["output_dir"] = "outputs/second"
        self.bind()
        self.assertEqual(self.run_request()["status"], "completed")

    def test_timeout_is_inconclusive_and_retry_is_linked(self) -> None:
        self.request["timeout_seconds"] = 0.01
        result = self.run_request()
        self.assertEqual(result["status"], "preempted")
        self.assertEqual(result["receipt"]["hypothesis_result"], "inconclusive")
        self.request["timeout_seconds"] = 5
        self.request["output_dir"] = "outputs/retry"
        with self.assertRaisesRegex(RuntimeError, "already attempted"):
            self.run_request()
        self.request.update(
            retry_of=result["attempt_id"],
            retry_reason="Increase operational wall clock after preemption",
        )
        self.assertEqual(self.run_request()["status"], "completed")

    def test_training_requires_fidelity(self) -> None:
        self.spec["task_type"] = "training"
        self.spec["command"].append("{budget_units}")
        self.bind()
        with self.assertRaisesRegex(RuntimeError, "fidelity"):
            self.run_request()

    def training_plan(self) -> dict[str, object]:
        """Build an initial measured fidelity plan for the exact fixture."""
        payload = computation_payload(self.spec)
        objective = {"metric": "score", "direction": "max", "min_delta": 0.01}
        return {
            "schema_version": 1,
            "capability": "ml",
            "cohort_id": "fixed-holdout",
            "unit": "iterations",
            "total_budget": 3,
            "stages": [
                {
                    "name": "short",
                    "budget": 1,
                    "status": "planned",
                    "cohort_id": "fixed-holdout",
                },
                {
                    "name": "long",
                    "budget": 3,
                    "status": "planned",
                    "cohort_id": "fixed-holdout",
                },
            ],
            "observed": {
                "speed": 1,
                "speed_unit": "iterations_per_second",
                "completed": 0,
                "available_seconds": 2,
            },
            "computation_payload": {
                key: payload[key] for key in ("code", "data", "split", "config")
            },
            "lineage": {
                **{
                    f"{key}_sha256": canonical_json_digest(payload[key])
                    for key in ("code", "data", "split", "config")
                },
                "objective": objective,
            },
            "objective": objective,
            "checkpoint": None,
            "approved_issue_axes": [],
            "partial_artifacts": [],
        }

    def test_training_next_rung_runs_but_excess_budget_is_rejected(self) -> None:
        self.spec["task_type"] = "training"
        self.spec["command"].append("{budget_units}")
        self.spec["budget"]["units"] = 2
        self.bind()
        self.request["fidelity"] = self.snapshot("fidelity.json", self.training_plan())
        with self.assertRaisesRegex(RuntimeError, "authorized next rung"):
            self.run_request()
        self.spec["budget"]["units"] = 1
        self.bind()
        self.assertEqual(self.run_request()["status"], "completed")

    def test_training_budget_must_be_bound_to_argv(self) -> None:
        self.spec["task_type"] = "training"
        self.bind()
        self.request["fidelity"] = self.snapshot("fidelity.json", self.training_plan())
        with self.assertRaisesRegex(RuntimeError, "training command must bind"):
            self.run_request()

    def test_resume_is_content_bound_and_continuation_preserves_family_budget(
        self,
    ) -> None:
        self.spec["task_type"] = "training"
        self.spec["command"].append("{budget_units}")
        self.bind()
        exploration = json.loads((self.root / "exploration.json").read_text())
        exploration["authorized_axes"][0]["budget"] = 1
        exploration["families"][0]["budget"] = 1
        self.request["exploration"] = self.snapshot("exploration.json", exploration)
        plan = self.training_plan()
        self.request["fidelity"] = self.snapshot("first-fidelity.json", plan)
        self.assertEqual(self.run_request()["status"], "completed")
        self.snapshot("checkpoint-model.json", {"weight": 0.5})
        self.snapshot("checkpoint-state.json", {"step": 1})
        manifest = create_checkpoint_manifest(
            self.root,
            "checkpoint.json",
            [
                {"component": "model", "path": "checkpoint-model.json"},
                {"component": "native_state", "path": "checkpoint-state.json"},
            ],
            capability="ml",
            required_components=["model", "native_state"],
            lineage=plan["lineage"],
            resume_mode="exact_resume",
        )
        plan["checkpoint"] = {
            "resume_mode": "exact_resume",
            "manifest_path": manifest["manifest_path"],
            "manifest_sha256": manifest["manifest_sha256"],
            "required_components": ["model", "native_state"],
        }
        plan["observed"]["completed"] = 1
        metric = {
            "metric": "score",
            "value": 0.5,
            "cohort_id": "fixed-holdout",
            "status": "completed",
            "stage_name": "short",
            "budget": 1,
        }
        metric_snapshot = self.snapshot("first-metric.json", metric)
        plan["stages"][0].update(
            status="completed", metric_snapshot={**metric, **metric_snapshot}
        )
        self.spec["budget"]["units"] = 3
        self.spec["resume"] = {
            "mode": "exact_resume",
            "manifest": self.evidence("checkpoint.json"),
        }
        self.spec["command"].append("{checkpoint_manifest}")
        self.bind()
        self.request["fidelity"] = self.snapshot("resume-fidelity.json", plan)
        self.request["output_dir"] = "outputs/resumed"
        result = self.run_request()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["receipt"]["command"][-2], "3")
        self.assertFalse(result["receipt"]["resume_verified"])
        changed = deepcopy(self.spec)
        changed["resume"]["manifest"]["sha256"] = "e" * 64
        self.assertNotEqual(
            computation_signature(self.spec), computation_signature(changed)
        )
        self.snapshot("checkpoint-model.json", {"weight": 999})
        with self.assertRaisesRegex(RuntimeError, "checkpoint components changed"):
            self.run_request()

    def test_fidelity_objective_must_match_executed_evaluation(self) -> None:
        self.spec["task_type"] = "training"
        self.spec["command"].append("{budget_units}")
        self.bind()
        plan = self.training_plan()
        plan["objective"]["direction"] = "min"
        self.request["fidelity"] = self.snapshot("fidelity.json", plan)
        with self.assertRaisesRegex(RuntimeError, "objective differs"):
            self.run_request()

    def test_registry_cannot_be_changed_to_bypass_deduplication(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "single workspace registry"):
            run_experiment(self.request, self.root, self.root / "new.sqlite")

    def test_running_resource_claim_cannot_be_reduced_by_new_queue(self) -> None:
        queue = {
            "schema_version": 1,
            "resources": {"cpu": 1},
            "jobs": [
                {
                    "id": name,
                    "status": "ready",
                    "depends_on": [],
                    "resources": {"cpu": 1},
                    "estimated_seconds": 1,
                    "priority": priority,
                }
                for name, priority in (("first", 2), ("second", 1))
            ],
        }
        self.request.update(
            queue=self.snapshot("queue-first.json", queue), queue_job_id="first"
        )
        original_request = deepcopy(self.request)
        arrived, release = Event(), Event()
        original_finish = runtime._finish

        def hold_finish(*args: object) -> None:
            """Keep the registry reservation active until the assertion ends."""
            arrived.set()
            release.wait(timeout=5)
            original_finish(*args)

        with (
            patch.object(runtime, "_finish", side_effect=hold_finish),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            first = pool.submit(
                run_experiment, original_request, self.root, self.registry
            )
            try:
                self.assertTrue(arrived.wait(timeout=5))
                self.spec["config"]["x"] = 2
                self.request["output_dir"] = "outputs/second"
                self.bind()
                queue["jobs"][0]["resources"] = {}
                queue["jobs"][0]["status"] = "completed"
                self.request.update(
                    queue=self.snapshot("queue-second.json", queue),
                    queue_job_id="second",
                )
                with self.assertRaisesRegex(RuntimeError, "queue did not select"):
                    self.run_request()
            finally:
                release.set()
            self.assertEqual(first.result()["status"], "completed")

    def test_shared_issue_evidence_is_not_a_duplicate_prediction(self) -> None:
        ledger = self.root / "ledger.jsonl"
        for value in (1, 2):
            output = self.snapshot(f"prediction-{value}.json", {"prediction": value})
            append_record(
                ledger,
                {
                    "record_id": str(value),
                    "family": "transform",
                    "config": {"x": value},
                    "source_issue": {"number": 1, "snapshot": self.issue},
                    "artifacts": [self.issue, output],
                },
                root=self.root,
            )
        self.assertEqual(len(ledger.read_text().splitlines()), 2)

    def test_issue_or_output_path_does_not_change_identity(self) -> None:
        first = {
            "family": "x",
            "config": {"nested": {"seed": 1}},
            "source_issue": {"number": 1},
            "research_manifest_path": "one",
        }
        second = {
            **first,
            "source_issue": {"number": 2},
            "research_manifest_path": "two",
        }
        self.assertEqual(experiment_signature(first), experiment_signature(second))
        second["config"] = {"nested": {"seed": 2}}
        self.assertEqual(
            experiment_signature(first, True), experiment_signature(second, True)
        )
        changed_split = deepcopy(self.spec)
        changed_split["split"]["fold"]["sha256"] = "a" * 64
        self.assertNotEqual(
            computation_signature(self.spec, True),
            computation_signature(changed_split, True),
        )

    def test_historical_ledger_hash_is_verified_then_deduplicated(self) -> None:
        record = {
            "record_id": "old",
            "family": "x",
            "config": {"alpha": 1},
            "research_manifest_path": "old.json",
        }
        record["experiment_signature"] = legacy_signature(record)
        self.assertEqual(experiment_signature(record), record["experiment_signature"])
        self.assertNotEqual(experiment_signature(record, canonical=True), record["experiment_signature"])
        ledger = self.root / "ledger.jsonl"
        ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "signature already exists"):
            append_record(
                ledger,
                {
                    "record_id": "new",
                    "family": "x",
                    "config": {"alpha": 1},
                    "research_manifest_path": "new.json",
                },
            )
        record["experiment_signature"] = "e" * 64
        ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "forged"):
            append_record(
                ledger, {"record_id": "new", "family": "y", "config": {"alpha": 1}}
            )

    def test_legacy_issue_text_remains_unverified_provenance(self) -> None:
        record = {"record_id": "legacy-text", "family": "x", "config": {"alpha": 1}, "source_issue": "H07/#8"}
        record["experiment_signature"] = legacy_signature(record)
        self.assertIs(validate_record(record), record)
        ledger = self.root / "legacy.jsonl"
        append_record(ledger, record)
        saved = json.loads(ledger.read_text())
        self.assertEqual(saved["signature_version"], 2)
        self.assertEqual(experiment_signature(saved), saved["experiment_signature"])
        with self.assertRaisesRegex(LedgerError, "signature already exists"):
            append_record(ledger, {"record_id": "same", "family": "x", "config": {"alpha": 1}, "source_issue": "H08/#9"})

    def test_compaction_preserves_active_dependencies_and_source(self) -> None:
        state = {
            "schema_version": 1,
            "resources": {"cpu": 1},
            "jobs": [
                {
                    "id": "done",
                    "status": "completed",
                    "depends_on": [],
                    "resources": {"cpu": 1},
                    "estimated_seconds": 100,
                    "priority": 1,
                },
                {
                    "id": "next",
                    "status": "ready",
                    "depends_on": ["done"],
                    "resources": {"cpu": 1},
                    "estimated_seconds": 1,
                    "priority": 1,
                },
            ],
        }
        self.snapshot("queue.json", state)
        before = sha256_file(self.root / "queue.json")
        result = compact_state(
            self.root / "queue.json",
            self.root / "archive.json",
            self.root / "compact.json",
        )
        self.assertTrue(result["source_unchanged"])
        self.assertEqual(before, sha256_file(self.root / "queue.json"))
        self.assertEqual(
            json.loads((self.root / "compact.json").read_text())["jobs"][0][
                "estimated_seconds"
            ],
            0,
        )
        with self.assertRaises(StateError):
            compact_state(
                self.root / "queue.json",
                self.root / "archive.json",
                self.root / "compact2.json",
            )


if __name__ == "__main__":
    unittest.main()
