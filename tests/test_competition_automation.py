"""Regression tests for model-independent competition controls."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path

from tools.competition_automation.cli import main
from tools.competition_automation.contract import ContractError, load_contract, validate_submission
from tools.competition_automation.audit import AuditError, audit_repository
from tools.competition_automation.fingerprint import fingerprint_path
from tools.competition_automation.gate import evaluate_gate
from tools.competition_automation.ledger import LedgerError, append_record, assert_new_submission
from tools.competition_automation.record import RecordError, validate_record
from tools.competition_automation.research import ResearchError, validate_research_manifest
from tools.competition_automation.quota import QuotaError, build_daily_plan, validate_quota_profile
from tools.competition_automation.completion import CompletionError, validate_completion_state
from tools.competition_automation.signature import experiment_signature
from tools.competition_automation.preflight import PreflightError, validate_preflight
from tools.competition_automation.submission import build_submission_plan


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "tests" / "fixtures" / "reference_contracts"


class ContractTests(unittest.TestCase):
    """Check the four task contracts and representative artifact failures."""

    def test_all_reference_contracts_load(self) -> None:
        for name in ("sad", "geoai", "nedo", "rogii"):
            with self.subTest(name=name):
                contract = load_contract(CONTRACTS / f"{name}.json")
                self.assertEqual(contract["schema_version"], 1)

    def test_csv_contract_checks_order_and_finite_values(self) -> None:
        contract = load_contract(CONTRACTS / "sad.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sample = root / "sample.csv"
            submission = root / "submission.csv"
            sample.write_text("ID,Target\na,0\nb,0\n", encoding="utf-8")
            submission.write_text("ID,Target\na,1.5\nb,-2\n", encoding="utf-8")
            result = validate_submission(submission, contract, sample)
            self.assertEqual(result["rows"], 2)
            self.assertEqual(result["artifact_status"], "ready")
            self.assertEqual(result["external_submission_status"], "manual_pending")
            self.assertEqual(result["upload_mode"], "manual")
            self.assertFalse(result["external_upload_attempted"])
            self.assertEqual(result["next_action"], "human_upload_required")
            with self.assertRaises(ContractError):
                validate_submission(submission, contract)
            submission.write_text("ID,Target\nb,1\na,2\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                validate_submission(submission, contract, sample)

    def test_non_kaggle_agent_upload_mode_is_rejected(self) -> None:
        source = json.loads((CONTRACTS / "sad.json").read_text(encoding="utf-8"))
        source["submission"]["upload_mode"] = "agent"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaises(ContractError):
                load_contract(path)

    def test_submission_plan_is_non_uploading_manual_handoff(self) -> None:
        contract = load_contract(CONTRACTS / "sad.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sample = root / "sample.csv"
            submission = root / "submission.csv"
            sample.write_text("ID,Target\na,0\n", encoding="utf-8")
            submission.write_text("ID,Target\na,1.5\n", encoding="utf-8")
            plan = build_submission_plan(contract, submission, sample)
            self.assertEqual(plan["status"], "READY_FOR_MANUAL_UPLOAD")
            self.assertEqual(plan["artifact_status"], "ready")
            self.assertEqual(plan["external_submission_status"], "manual_pending")
            self.assertFalse(plan["external_upload_attempted"])
            self.assertEqual(plan["validation"]["status"], "PASS")

    def test_submission_plan_cli_does_not_attempt_external_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sample = root / "sample.csv"
            submission = root / "submission.csv"
            sample.write_text("ID,Target\na,0\n", encoding="utf-8")
            submission.write_text("ID,Target\na,1.5\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "submission-plan",
                        str(CONTRACTS / "sad.json"),
                        str(submission),
                        "--sample",
                        str(sample),
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "READY_FOR_MANUAL_UPLOAD")
            self.assertFalse(payload["external_upload_attempted"])

    def test_zip_contract_rejects_path_traversal(self) -> None:
        contract = load_contract(CONTRACTS / "nedo.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "candidate.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("..\\agent.py", "class Agent: pass\n")
            with self.assertRaises(ContractError):
                validate_submission(path, contract)

    def test_nedo_zip_requires_compilable_agent_interface(self) -> None:
        contract = load_contract(CONTRACTS / "nedo.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "candidate.zip"
            source = """\
class Agent:
    def __init__(self, *args, **kwargs):
        pass
    def get_init_states(self):
        return []
    def optimize(self):
        return None
    def policy(self):
        return None
"""
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("base/agent.py", source)
            result = validate_submission(path, contract)
            self.assertEqual(result["entries"], 1)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("base/agent.py", "class Agent(:\n")
            with self.assertRaises(ContractError):
                validate_submission(path, contract)

    def test_code_competition_contract_checks_metadata(self) -> None:
        contract = load_contract(CONTRACTS / "rogii.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "inference_notebook.ipynb").write_text("{}\n", encoding="utf-8")
            (root / "kernel-metadata.json").write_text(
                json.dumps({"code_file": "inference_notebook.ipynb", "competition_sources": ["x"]}),
                encoding="utf-8",
            )
            result = validate_submission(root, contract)
            self.assertEqual(result["format"], "code_competition")

    def test_code_competition_rejects_code_symlink(self) -> None:
        contract = load_contract(CONTRACTS / "rogii.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root.parent / "outside.py"
            outside.write_text("print('outside')\n", encoding="utf-8")
            (root / "kernel-metadata.json").write_text(
                json.dumps({"code_file": "inference.py", "competition_sources": []}),
                encoding="utf-8",
            )
            try:
                (root / "inference.py").symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(ContractError):
                validate_submission(root, contract)

    def test_contract_rejects_unknown_validator_field(self) -> None:
        source = json.loads((CONTRACTS / "sad.json").read_text(encoding="utf-8"))
        source["policy"]["typoed_required_check"] = True
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaises(ContractError):
                load_contract(path)

    def test_feature_and_id_boundaries(self) -> None:
        contract = load_contract(CONTRACTS / "rogii.json")
        from tools.competition_automation.contract import validate_feature_boundary, validate_id_disjointness

        validate_feature_boundary(["TVT", "x"], ["x"], contract, ["x"])
        with self.assertRaises(ContractError):
            validate_feature_boundary(["TVT", "x"], ["x"], contract, ["TVT"])
        validate_id_disjointness(["a"], ["b"])
        with self.assertRaises(ContractError):
            validate_id_disjointness(["a"], ["a"])


class EvidenceGateTests(unittest.TestCase):
    """Ensure proxy evidence cannot be promoted to an adopted result."""

    def setUp(self) -> None:
        self.contract = load_contract(CONTRACTS / "sad.json")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        check_artifacts = []
        check_values = {}
        for name in ("contract", "leakage", "reproducibility", "submission"):
            evidence_path = Path("evidence") / f"{name}.json"
            absolute = self.root / evidence_path
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_text(json.dumps({"check": name, "status": "PASS"}) + "\n", encoding="utf-8")
            digest = str(fingerprint_path(absolute)["sha256"])
            check_values[name] = {
                "status": "PASS",
                "command": f"validator {name}",
                "evidence_path": evidence_path.as_posix(),
                "evidence_sha256": digest,
            }
            check_artifacts.append({"path": evidence_path.as_posix(), "sha256": digest})
        research_source = self.root / "evidence" / "research-source.json"
        research_source.write_text("official rules snapshot\n", encoding="utf-8")
        research_source_digest = str(fingerprint_path(research_source)["sha256"])
        required_sections = ("rules", "data", "metric", "submission", "timeline_quota", "leakage", "history")
        research_sections = {
            section: {
                "sources": [{
                    "kind": "official",
                    "uri": f"https://example.test/{section}",
                    "observed_at": "2026-08-15T00:00:00Z",
                    "artifact_path": "evidence/research-source.json",
                    "sha256": research_source_digest,
                }]
            }
            for section in required_sections
        }
        research_manifest = self.root / "evidence" / "research.json"
        research_manifest.write_text(
            json.dumps({"schema_version": 1, "sections": research_sections}) + "\n",
            encoding="utf-8",
        )
        research_digest = str(fingerprint_path(research_manifest)["sha256"])
        objective_proof = self.root / "evidence" / "objective.json"
        objective_proof.write_text("objective proof\n", encoding="utf-8")
        objective_digest = str(fingerprint_path(objective_proof)["sha256"])
        completion_state = self.root / "evidence" / "completion.json"
        completion_state.write_text(
            json.dumps({
                "schema_version": 1,
                "status": "completed",
                "objective": {
                    "achieved": True,
                    "proof": {"artifact_path": "evidence/objective.json", "sha256": objective_digest},
                },
            }) + "\n",
            encoding="utf-8",
        )
        completion_digest = str(fingerprint_path(completion_state)["sha256"])
        check_artifacts.extend([
            {"path": "evidence/research-source.json", "sha256": research_source_digest},
            {"path": "evidence/research.json", "sha256": research_digest},
            {"path": "evidence/objective.json", "sha256": objective_digest},
            {"path": "evidence/completion.json", "sha256": completion_digest},
        ])
        self.base = {
            "record_id": "expA00",
            "provenance": {
                "data": {"sha256": "0" * 64},
                "code": {"sha256": "1" * 64},
                "config": {"sha256": "2" * 64},
                "split": {"sha256": "3" * 64},
            },
            "checks": check_values,
            "metrics": [{"name": "rmse", "value": 0.8, "evidence_kind": "proxy"}],
            "artifacts": check_artifacts,
            "research_manifest_path": "evidence/research.json",
            "research_manifest_sha256": research_digest,
            "completion_state_path": "evidence/completion.json",
            "completion_state_sha256": completion_digest,
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_proxy_is_candidate_only(self) -> None:
        result = evaluate_gate({**self.base, "requested_decision": "adopted"}, self.contract, root=self.root)
        self.assertEqual(result["decision"], "candidate")
        self.assertIn("official", " ".join(result["reasons"]))

    def test_official_can_be_adopted(self) -> None:
        score = self.root / "evidence" / "score.json"
        score.parent.mkdir(parents=True, exist_ok=True)
        score.write_text("{\"score\": 0.7}\n", encoding="utf-8")
        digest = str(fingerprint_path(score)["sha256"])
        record = {
            **self.base,
            "requested_decision": "adopted",
            "metrics": [
                {
                    "name": "rmse",
                    "value": 0.7,
                    "evidence_kind": "official",
                    "source": {
                        "platform": "zindi",
                        "submission_id": "submission-1",
                        "observed_at": "2026-08-15T00:00:00Z",
                        "verification": "official_page_snapshot",
                        "raw_artifact_path": "evidence/score.json",
                        "raw_artifact_sha256": digest,
                    },
                }
            ],
            "artifacts": [*self.base["artifacts"], {"path": "evidence/score.json", "sha256": digest}],
        }
        self.assertEqual(evaluate_gate(record, self.contract, root=self.root)["decision"], "adopted")

    def test_adoption_can_require_a_matching_selection_plan(self) -> None:
        score = self.root / "evidence" / "score-selection.json"
        score.write_text('{"score": 0.7}\n', encoding="utf-8")
        digest = str(fingerprint_path(score)["sha256"])
        contract = {
            **self.contract,
            "policy": {
                **self.contract["policy"],
                "selection_proof_required_for_adoption": True,
            },
        }
        record = {
            **self.base,
            "requested_decision": "adopted",
            "family": "candidate-family",
            "metrics": [
                {
                    "name": "rmse",
                    "value": 0.7,
                    "evidence_kind": "official",
                    "source": {
                        "platform": "zindi",
                        "submission_id": "submission-1",
                        "observed_at": "2026-08-15T00:00:00Z",
                        "verification": "official_page_snapshot",
                        "raw_artifact_path": "evidence/score-selection.json",
                        "raw_artifact_sha256": digest,
                    },
                }
            ],
            "artifacts": [
                *self.base["artifacts"],
                {"path": "evidence/score-selection.json", "sha256": digest},
            ],
        }
        result = evaluate_gate(record, contract, root=self.root)
        self.assertEqual(result["decision"], "blocked")
        self.assertIn("selection_plan_path", " ".join(result["reasons"]))

    def test_llm_cannot_promote_a_record(self) -> None:
        record = {
            **self.base,
            "requested_decision": "adopted",
            "decision_source": "llm:some-model",
            "metrics": [
                {
                    "name": "rmse",
                    "value": 0.7,
                    "evidence_kind": "official",
                    "source": {
                        "platform": "zindi",
                        "submission_id": "submission-1",
                        "observed_at": "2026-08-15T00:00:00Z",
                        "verification": "official_page_snapshot",
                        "raw_artifact_path": "evidence/score.json",
                        "raw_artifact_sha256": "4" * 64,
                    },
                }
            ],
            "artifacts": [*self.base["artifacts"], {"path": "evidence/score.json", "sha256": "4" * 64}],
        }
        result = evaluate_gate(record, self.contract, root=self.root)
        self.assertEqual(result["decision"], "candidate")
        self.assertTrue(result["llm_decision_ignored"])

    def test_unverified_official_self_report_is_downgraded(self) -> None:
        record = {
            **self.base,
            "requested_decision": "adopted",
            "metrics": [
                {"name": "rmse", "value": 0.7, "evidence_kind": "official", "source": "user text"}
            ],
        }
        result = evaluate_gate(record, self.contract, root=self.root)
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(result["evidence_kind"], "user_reported_official")
        self.assertTrue(result["warnings"])

    def test_metric_domain_blocks_impossible_official_value(self) -> None:
        record = {
            **self.base,
            "requested_decision": "adopted",
            "metrics": [{"name": "rmse", "value": -1.0, "evidence_kind": "proxy"}],
        }
        result = evaluate_gate(record, self.contract, root=self.root)
        self.assertEqual(result["decision"], "blocked")
        self.assertTrue(any("domain" in reason for reason in result["reasons"]))

    def test_seed_only_candidate_is_blocked_without_explicit_control(self) -> None:
        record = {
            **self.base,
            "requested_decision": "candidate",
            "change_scope": "seed_only",
            "base_signature": "4" * 64,
        }
        result = evaluate_gate(record, self.contract, root=self.root)
        self.assertEqual(result["decision"], "blocked")
        self.assertTrue(any("seed-only" in reason for reason in result["reasons"]))

    def test_check_without_materialized_evidence_is_blocked(self) -> None:
        record = {**self.base, "checks": {**self.base["checks"]}}
        record["checks"]["exact_replay"] = {
            "status": "PASS",
            "command": "exact-replay",
            "evidence_sha256": "f" * 64,
        }
        contract = {**self.contract, "policy": {**self.contract["policy"], "required_checks": ["exact_replay"]}}
        result = evaluate_gate(record, contract, root=self.root)
        self.assertEqual(result["decision"], "blocked")
        self.assertTrue(any("invalid record" in reason for reason in result["reasons"]))


class LedgerTests(unittest.TestCase):
    """Ensure duplicate record and duplicate artifact guards are deterministic."""

    def test_duplicate_record_and_artifact_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger.jsonl"
            record = {
                "record_id": "expA00",
                "artifacts": [{"path": "submission.csv", "sha256": "a" * 64}],
            }
            append_record(ledger, record)
            with self.assertRaises(LedgerError):
                append_record(ledger, record)
            with self.assertRaises(LedgerError):
                append_record(
                    ledger,
                    {"record_id": "expA01", "artifacts": [{"path": "copy.csv", "sha256": "a" * 64}]},
                )
            with self.assertRaises(LedgerError):
                append_record(
                    ledger,
                    {"record_id": "expA01b", "artifacts": [{"path": "copy.csv", "sha256": "a" * 64}]},
                    allow_duplicate_artifact=True,
                )
            append_record(
                ledger,
                {
                    "record_id": "expA01c",
                    "control": True,
                    "control_of": "expA00",
                    "control_reason": "replay control",
                    "artifacts": [{"path": "copy.csv", "sha256": "a" * 64}],
                },
                allow_duplicate_artifact=True,
            )

    def test_semantic_experiment_signature_rejects_repeat_with_new_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger.jsonl"
            first = {
                "record_id": "exp-semantic-1",
                "family": "model-a",
                "provenance": {"code": {"sha256": "1" * 64}, "config": {"sha256": "2" * 64}},
                "artifacts": [{"path": "first.csv", "sha256": "a" * 64}],
            }
            self.assertIsNotNone(experiment_signature(first))
            append_record(ledger, first)
            with self.assertRaises(LedgerError):
                append_record(
                    ledger,
                    {
                        "record_id": "exp-semantic-2",
                        "family": "model-a",
                        "provenance": {"code": {"sha256": "1" * 64}, "config": {"sha256": "2" * 64}},
                        "artifacts": [{"path": "second.csv", "sha256": "b" * 64}],
                    },
                )

    def test_seed_change_is_not_a_new_experiment_without_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger.jsonl"
            first = {
                "record_id": "exp-seed-1",
                "family": "model-a",
                "seed": 1,
                "provenance": {"code": {"sha256": "1" * 64}, "config": {"sha256": "2" * 64}},
                "artifacts": [{"path": "first.csv", "sha256": "a" * 64}],
            }
            append_record(ledger, first)
            with self.assertRaises(LedgerError):
                append_record(
                    ledger,
                    {
                        "record_id": "exp-seed-2",
                        "family": "model-a",
                        "seed": 2,
                        "provenance": {"code": {"sha256": "1" * 64}, "config": {"sha256": "2" * 64}},
                        "artifacts": [{"path": "second.csv", "sha256": "b" * 64}],
                    },
                )

    def test_forged_experiment_signature_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger.jsonl"
            with self.assertRaises(LedgerError):
                append_record(
                    ledger,
                    {
                        "record_id": "exp-forged-signature",
                        "family": "model-a",
                        "provenance": {"code": {"sha256": "1" * 64}},
                        "experiment_signature": "f" * 64,
                        "artifacts": [],
                    },
                )

    def test_submission_hash_cannot_be_offered_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            submission = root / "submission.csv"
            submission.write_text("ID,Target\na,0\n", encoding="utf-8")
            digest = str(fingerprint_path(submission)["sha256"])
            ledger = root / "ledger.jsonl"
            append_record(
                ledger,
                {"record_id": "exp-submit-1", "artifacts": [{"path": "submission.csv", "sha256": digest}]},
            )
            with self.assertRaises(LedgerError):
                assert_new_submission(submission, ledger)
            distinct = root / "distinct.csv"
            distinct.write_text("ID,Target\na,1\n", encoding="utf-8")
            self.assertEqual(assert_new_submission(distinct, ledger)["status"], "PASS")

    def test_fingerprint_is_content_based(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data.txt"
            path.write_text("same", encoding="utf-8")
            first = fingerprint_path(path)
            path.touch()
            second = fingerprint_path(path)
            self.assertEqual(first["sha256"], second["sha256"])

    def test_root_rehashes_artifact_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "metrics.json"
            artifact.write_text("{}\n", encoding="utf-8")
            digest = str(fingerprint_path(artifact)["sha256"])
            ledger = root / "ledger.jsonl"
            append_record(
                ledger,
                {"record_id": "expA02", "artifacts": [{"path": "metrics.json", "sha256": digest}]},
                root=root,
            )
            with self.assertRaises(LedgerError):
                append_record(
                    ledger,
                    {"record_id": "expA03", "artifacts": [{"path": "metrics.json", "sha256": "0" * 64}]},
                    root=root,
                )

    def test_root_rejects_symlink_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "metrics.json"
            target.write_text("{}\n", encoding="utf-8")
            link = root / "link.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            ledger = root / "ledger.jsonl"
            with self.assertRaises(LedgerError):
                append_record(
                    ledger,
                    {
                        "record_id": "expA04",
                        "artifacts": [{"path": "link.json", "sha256": str(fingerprint_path(target)["sha256"])}],
                    },
                    root=root,
                )


class GenericLayoutAuditTests(unittest.TestCase):
    """Ensure audit paths and payload rules come from a generic layout profile."""

    def test_custom_layout_profile_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            experiment = root / "runs" / "exp001"
            experiment.mkdir(parents=True)
            (experiment / "notes.md").write_text("done\n", encoding="utf-8")
            (experiment / "execute.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (experiment / "output").mkdir()
            delivery = root / "deliverables"
            delivery.mkdir()
            (delivery / "prediction.csv").write_text("id,pred\na,0\n", encoding="utf-8")
            (delivery / "cache.pyc").write_bytes(b"generated\n")
            layout = root / "layout.json"
            layout.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "experiment_roots": ["runs"],
                        "experiment_prefixes": ["exp"],
                        "submission_roots": ["deliverables"],
                        "required_experiment_files": {
                            "notes": "notes.md",
                            "runner": "execute.sh",
                            "outputs_dir": "output",
                        },
                        "payload_extensions": [".csv"],
                        "payload_names": [],
                        "ignored_parts": [".git"],
                        "ignored_extensions": [".pyc"],
                        "ignored_names": [".DS_Store"],
                        "history_paths": ["history.md"],
                    }
                ),
                encoding="utf-8",
            )
            result = audit_repository(root, layout)
            self.assertEqual(result["experiment_count"], 1)
            self.assertEqual(result["submission_payload_count"], 1)
            self.assertEqual(result["submission_support_file_count"], 0)
            self.assertEqual(result["history_checks"], {"history.md": False})

    def test_layout_rejects_parent_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layout = root / "layout.json"
            layout.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "experiment_roots": ["../outside"],
                        "experiment_prefixes": ["exp"],
                        "submission_roots": [],
                        "required_experiment_files": {},
                        "payload_extensions": [".csv"],
                        "payload_names": [],
                        "ignored_parts": [],
                        "history_paths": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(AuditError):
                audit_repository(root, layout)


class GenericRecordTests(unittest.TestCase):
    """Ensure lifecycle metadata is generic and malformed evidence is rejected."""

    def test_family_and_proposal_are_allowed_metadata(self) -> None:
        record = {
            "record_id": "exp001",
            "family": "baseline",
            "parent_record_id": None,
            "proposal": {"source": "human", "text": "baseline"},
            "requested_decision": "candidate",
            "artifacts": [],
            "checks": {},
            "metrics": [],
        }
        self.assertIs(validate_record(record), record)

    def test_pass_check_requires_materialized_evidence_fields(self) -> None:
        record = {
            "record_id": "exp002",
            "checks": {"contract": {"status": "PASS", "command": "contract-check"}},
        }
        with self.assertRaises(RecordError):
            validate_record(record)

    def test_source_issue_requires_number_and_snapshot_hash(self) -> None:
        record = {
            "record_id": "exp-issue",
            "source_issue": {
                "number": 7,
                "snapshot": {"path": "research/issues/7.json", "sha256": "a" * 64},
            },
        }
        self.assertIs(validate_record(record), record)
        record["source_issue"]["snapshot"]["sha256"] = "invalid"
        with self.assertRaisesRegex(RecordError, "source_issue.snapshot.sha256"):
            validate_record(record)

    def test_template_gate_requires_research_and_completion_proof(self) -> None:
        contract = load_contract(CONTRACTS / "template.json")
        result = evaluate_gate({"record_id": "exp003"}, contract)
        self.assertIn("user Issue source_issue is required", result["reasons"])
        self.assertEqual(result["decision"], "blocked")
        self.assertTrue(any("research preflight" in reason for reason in result["reasons"]))
        self.assertTrue(any("completion proof" in reason for reason in result["reasons"]))

    def test_issue_gate_rehashes_source_snapshot(self) -> None:
        contract = load_contract(CONTRACTS / "template.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            issue_path = root / "issue.json"
            issue_path.write_text('{"number": 7, "title": "Ablation"}\n', encoding="utf-8")
            snapshot = {"path": "issue.json", "sha256": fingerprint_path(issue_path)["sha256"]}
            record = {
                "record_id": "exp-issue-gate",
                "source_issue": {"number": 7, "snapshot": snapshot},
                "artifacts": [snapshot],
            }
            valid = evaluate_gate(record, contract, root)
            self.assertFalse(any("user Issue" in reason for reason in valid["reasons"]))
            issue_path.write_text('{"number": 7, "title": "Edited"}\n', encoding="utf-8")
            stale = evaluate_gate(record, contract, root)
            self.assertIn("user Issue snapshot is missing or its hash does not match", stale["reasons"])

    def test_ready_record_requires_submission_pass_and_payload_hash(self) -> None:
        with self.assertRaises(RecordError):
            validate_record(
                {
                    "record_id": "exp-ready",
                    "submission_status": "ready",
                    "ready_at": "2026-08-15T10:00:00+09:00",
                    "artifacts": [],
                    "checks": {},
                    "metrics": [],
                }
            )


class ResearchQuotaCompletionTests(unittest.TestCase):
    """Guard pre-analysis research, daily quota planning, and terminal states."""

    def test_research_manifest_rehashes_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sections = {}
            for section in ("rules", "metric"):
                snapshot = root / f"{section}.txt"
                snapshot.write_text(f"{section} source\n", encoding="utf-8")
                digest = str(fingerprint_path(snapshot)["sha256"])
                sections[section] = {
                    "sources": [{
                        "kind": "official",
                        "uri": f"https://example.test/{section}",
                        "observed_at": "2026-08-15T00:00:00Z",
                        "artifact_path": snapshot.name,
                        "sha256": digest,
                    }]
                }
            manifest = root / "research.json"
            manifest.write_text(
                json.dumps({"schema_version": 1, "sections": sections}),
                encoding="utf-8",
            )
            result = validate_research_manifest(manifest, root, ["rules", "metric"], minimum_sources=2)
            self.assertEqual(result["status"], "PASS")
            sections.pop("metric")
            manifest.write_text(json.dumps({"schema_version": 1, "sections": sections}), encoding="utf-8")
            with self.assertRaises(ResearchError):
                validate_research_manifest(manifest, root, ["rules", "metric"], minimum_sources=2)

    def test_signate_preflight_requires_rule_backed_quota(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "rules.txt"
            source.write_text("official daily limit\n", encoding="utf-8")
            digest = str(fingerprint_path(source)["sha256"])
            manifest = root / "research.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "sections": {
                            "rules": {
                                "sources": [
                                    {
                                        "kind": "official",
                                        "uri": "https://example.test/rules",
                                        "observed_at": "2026-08-15T00:00:00Z",
                                        "artifact_path": "rules.txt",
                                        "sha256": digest,
                                    }
                                ]
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            contract = root / "contract.json"
            contract.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "competition": {"name": "demo", "slug": "demo"},
                        "platform": {"kind": "signate"},
                        "data": {},
                        "submission": {"format": "csv", "columns": ["id", "prediction"]},
                        "evaluation": {"metric": "score", "direction": "max"},
                        "split": {"strategy": "holdout"},
                        "research": {"required_sections": ["rules"], "minimum_sources": 1},
                        "policy": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(PreflightError):
                validate_preflight(contract, manifest, root)

    def test_quota_plan_requires_distinct_non_seed_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rules = root / "rules.txt"
            rules.write_text("daily limit 5\n", encoding="utf-8")
            rules_digest = str(fingerprint_path(rules)["sha256"])
            profile_path = root / "quota.json"
            profile_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "platform": "signate",
                    "competition_slug": "demo",
                    "period": "daily",
                    "timezone": "Asia/Tokyo",
                    "reset_at_local": "00:00",
                    "scope": "team",
                    "max_submissions_per_period": 5,
                    "target_ready_artifacts_per_period": 5,
                    "count_failed_attempts": True,
                    "source": {
                        "kind": "official_rules_snapshot",
                        "uri": "https://example.test/rules",
                        "observed_at": "2026-08-15T00:00:00Z",
                        "artifact_path": "rules.txt",
                        "sha256": rules_digest,
                    },
                }),
                encoding="utf-8",
            )
            profile = validate_quota_profile(profile_path, root)
            shared_a = root / "shared-a.csv"
            shared_a.write_text("ID,Target\na,0\n", encoding="utf-8")
            shared_b = root / "shared-b.csv"
            shared_b.write_text(shared_a.read_text(encoding="utf-8"), encoding="utf-8")
            seed_payload = root / "seed.csv"
            seed_payload.write_text("ID,Target\na,1\n", encoding="utf-8")

            def ready_record(record_id: str, payload: Path, ready_at: str) -> dict[str, object]:
                payload_digest = str(fingerprint_path(payload)["sha256"])
                evidence = root / f"{record_id}-submission.json"
                evidence.write_text(json.dumps({"status": "PASS", "artifact": payload.name}), encoding="utf-8")
                evidence_digest = str(fingerprint_path(evidence)["sha256"])
                return {
                    "record_id": record_id,
                    "submission_status": "ready",
                    "ready_at": ready_at,
                    "submission_artifact_sha256": [payload_digest],
                    "checks": {
                        "submission": {
                            "status": "PASS",
                            "command": "submission-check",
                            "evidence_path": evidence.name,
                            "evidence_sha256": evidence_digest,
                        }
                    },
                    "artifacts": [
                        {"path": payload.name, "sha256": payload_digest},
                        {"path": evidence.name, "sha256": evidence_digest},
                    ],
                }

            records = [
                ready_record("a", shared_a, "2026-08-15T09:00:00+09:00"),
                ready_record("b", shared_b, "2026-08-15T10:00:00+09:00"),
                {
                    **ready_record("seed", seed_payload, "2026-08-15T11:00:00+09:00"),
                    "change_scope": "seed_only",
                    "base_signature": "c" * 64,
                },
            ]
            plan = build_daily_plan(
                profile,
                records,
                datetime_from_iso("2026-08-15T12:00:00+09:00"),
                root=root,
            )
            self.assertEqual(plan["status"], "NEEDS_DISTINCT_CANDIDATES")
            self.assertEqual(plan["distinct_ready_artifacts"], 1)
            self.assertEqual(plan["missing_distinct_candidates"], 4)
            self.assertEqual(plan["duplicate_ready_record_ids"], ["b"])
            self.assertTrue(plan["seed_only_excluded"])

    def test_completion_cannot_claim_success_without_objective_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = root / "completion.json"
            state.write_text(
                json.dumps({"schema_version": 1, "status": "completed", "objective": {"achieved": False}}),
                encoding="utf-8",
            )
            with self.assertRaises(CompletionError):
                validate_completion_state(state, root)

    def test_ended_state_records_terminal_failure_without_fake_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            leaderboard = root / "final-leaderboard.json"
            leaderboard.write_text("final leaderboard\n", encoding="utf-8")
            leaderboard_digest = str(fingerprint_path(leaderboard)["sha256"])
            state = root / "completion.json"
            state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "ended",
                        "daily_quota_required": True,
                        "objective": {"achieved": False},
                        "reason": "competition ended before the target was achieved",
                        "ended_at": "2026-08-28T09:00:00+09:00",
                        "terminal_evidence": {
                            "artifact_path": "final-leaderboard.json",
                            "sha256": leaderboard_digest,
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = validate_completion_state(state, root)
            self.assertEqual(result["task_status"], "ended")
            self.assertFalse(result["objective_achieved"])
            self.assertTrue(result["terminal_without_success"])

    def test_daily_quota_is_part_of_success_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            proof = root / "objective.json"
            proof.write_text("objective\n", encoding="utf-8")
            proof_digest = str(fingerprint_path(proof)["sha256"])
            state = root / "completion.json"
            state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "completed",
                        "daily_quota_required": True,
                        "objective": {
                            "achieved": True,
                            "proof": {"artifact_path": "objective.json", "sha256": proof_digest},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(CompletionError):
                validate_completion_state(state, root, quota_plan={"status": "NEEDS_DISTINCT_CANDIDATES"})
            result = validate_completion_state(state, root, quota_plan={"status": "COMPLETE"})
            self.assertEqual(result["status"], "PASS")


def datetime_from_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp used by quota planner tests."""
    return datetime.fromisoformat(value)

if __name__ == "__main__":
    unittest.main()
