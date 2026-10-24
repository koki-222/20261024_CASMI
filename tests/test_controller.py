"""Regression tests for deterministic orchestration controls."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
import json
import tempfile
import unittest
from pathlib import Path

from tools.competition_automation.cli import main
from tools.competition_automation.controller import (
    ControllerError,
    evaluate_controller_state,
    provider_error_fingerprint,
)
from tools.competition_automation.fingerprint import fingerprint_path


def _signature(character: str) -> str:
    """Return a readable test SHA-256 value."""
    return character * 64


class ControllerTests(unittest.TestCase):
    """Ensure repeated/no-op orchestration actions are rejected before execution."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.snapshot = self.root / "state.json"
        self.snapshot.write_text("{}\n", encoding="utf-8")
        self.digest = str(fingerprint_path(self.snapshot)["sha256"])
        self.profile = self.root / "evaluation-profiles.json"
        self.profile.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profiles": [
                        {
                            "name": "session_group",
                            "role": "diagnostic",
                            "distribution": "known sessions",
                        },
                        {
                            "name": "objective_cold",
                            "role": "selection",
                            "distribution": "unseen objectives and sessions",
                        },
                        {
                            "name": "worst_fold",
                            "role": "stress",
                            "distribution": "worst objective-cold fold",
                        },
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.profile_digest = str(fingerprint_path(self.profile)["sha256"])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _evidence(self) -> dict[str, str]:
        return {"path": "state.json", "sha256": self.digest}

    def _profile_evidence(self) -> dict[str, str]:
        return {"path": "evaluation-profiles.json", "sha256": self.profile_digest}

    def _experiment_action(self) -> dict[str, object]:
        return {
            "type": "experiment",
            "family": "external-feature",
            "hypothesis_signature": _signature("1"),
            "mechanism_signature": _signature("2"),
            "input_signature": _signature("3"),
            "change_scope": "feature",
            "run_snapshot": {
                "data": self._evidence(),
                "code": self._evidence(),
                "config": self._evidence(),
                "split": self._evidence(),
            },
        }

    def _state(self, action: dict[str, object] | None = None) -> dict[str, object]:
        return {
            "schema_version": 1,
            "state_inputs": [self._evidence()],
            "policy": {
                "provider_failure_threshold": 1,
                "family_failure_threshold": 3,
            },
            "providers": [],
            "families": [
                {
                    "name": "external-feature",
                    "status": "open",
                    "attempts": 1,
                    "budget": 6,
                    "consecutive_robust_failures": 0,
                    "mechanism_signatures": [_signature("2")],
                    "input_signatures": [_signature("3")],
                    "batch_required": False,
                }
            ],
            "experiment_history": [],
            "action": action or self._experiment_action(),
        }

    def _add_evaluation_policy(self, state: dict[str, object]) -> None:
        """Attach a frozen deployment-aligned selection policy to ``state``."""
        state["evaluation_policy"] = {
            "profile_manifest": self._profile_evidence(),
            "selection_profile": "objective_cold",
            "diagnostic_profiles": ["session_group"],
            "required_stress_profiles": ["worst_fold"],
            "max_official_feedback_rounds_per_family": 1,
        }

    def _add_user_issue(self, state: dict[str, object]) -> Path:
        """Attach a frozen, user-authored issue to an experiment action."""
        issue_path = self.root / "issue-7.json"
        issue_path.write_text(
            json.dumps(
                {
                    "number": 7,
                    "url": "https://github.com/example/competition/issues/7",
                    "state": "OPEN",
                    "author": {"login": "researcher"},
                    "title": "Test a new feature family",
                    "body": "Compare against the frozen champion on the same folds.",
                    "updatedAt": "2026-10-01T00:00:00Z",
                }
            ) + "\n",
            encoding="utf-8",
        )
        evidence = {"path": "issue-7.json", "sha256": fingerprint_path(issue_path)["sha256"]}
        state["state_inputs"].append(evidence)
        state["action"]["source_issue"] = {"number": 7, "snapshot": evidence}
        return issue_path

    def test_provider_error_fingerprint_normalizes_message_whitespace(self) -> None:
        first = provider_error_fingerprint("Provider", "400", "Reasoning_Content  failed")
        second = provider_error_fingerprint("provider", "400", "  reasoning_content failed ")
        self.assertEqual(first, second)

    def test_provider_circuit_blocks_identical_failure_retry(self) -> None:
        state = self._state({"type": "provider_dispatch", "provider": "provider-x"})
        state["providers"] = [
            {
                "name": "provider-x",
                "error_fingerprint": _signature("a"),
                "consecutive_failures": 1,
                "retry_after": "2026-08-21T00:00:00Z",
            }
        ]
        result = evaluate_controller_state(
            state,
            self.root,
            now=datetime.fromisoformat("2026-08-20T00:00:00+00:00"),
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["decision"], "PROVIDER_CIRCUIT_OPEN")

    def test_unchanged_audit_is_skipped(self) -> None:
        state = self._state({"type": "audit"})
        first = evaluate_controller_state(state, self.root)
        state["previous_audit_state_fingerprint"] = first["state_fingerprint"]
        second = evaluate_controller_state(state, self.root)
        self.assertFalse(second["allowed"])
        self.assertEqual(second["decision"], "UNCHANGED_AUDIT_SKIPPED")

    def test_controller_cli_returns_one_for_disallowed_action(self) -> None:
        state = self._state({"type": "audit"})
        first = evaluate_controller_state(state, self.root)
        state["previous_audit_state_fingerprint"] = first["state_fingerprint"]
        state_path = self.root / "controller.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["controller-check", str(state_path), "--root", str(self.root)])
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["decision"], "UNCHANGED_AUDIT_SKIPPED")

    def test_blocked_state_resumes_only_on_due_time_or_changed_evidence(self) -> None:
        state = self._state({"type": "resume_check"})
        fingerprint = evaluate_controller_state(state, self.root)["state_fingerprint"]
        state["blocked_state"] = {
            "active": True,
            "reason": "waiting for reveal",
            "resume_condition": "official snapshot changes or scheduled reveal passes",
            "state_fingerprint": fingerprint,
            "next_check_at": "2026-08-21T00:00:00Z",
        }
        waiting = evaluate_controller_state(
            state,
            self.root,
            now=datetime.fromisoformat("2026-08-20T00:00:00+00:00"),
        )
        self.assertEqual(waiting["decision"], "WAITING_FOR_RESUME")
        due = evaluate_controller_state(
            state,
            self.root,
            now=datetime.fromisoformat("2026-08-22T00:00:00+00:00"),
        )
        self.assertTrue(due["allowed"])
        self.assertEqual(due["decision"], "RESUME_DUE")

    def test_duplicate_hypothesis_is_rejected(self) -> None:
        state = self._state()
        state["experiment_history"] = [_signature("1")]
        result = evaluate_controller_state(state, self.root)
        self.assertEqual(result["decision"], "DUPLICATE_EXPERIMENT")

    def test_closed_family_requires_new_mechanism_or_input(self) -> None:
        state = self._state()
        state["families"][0]["consecutive_robust_failures"] = 3
        closed = evaluate_controller_state(state, self.root)
        self.assertEqual(closed["decision"], "FAMILY_CLOSED")
        state["action"]["reopen"] = {"reason": "new external source became available"}
        state["action"]["input_signature"] = _signature("4")
        reopened = evaluate_controller_state(state, self.root)
        self.assertTrue(reopened["allowed"])
        self.assertEqual(reopened["decision"], "REOPEN_FAMILY")

    def test_seed_averaging_is_not_a_new_hypothesis(self) -> None:
        state = self._state()
        state["action"]["seed_averaging"] = True
        result = evaluate_controller_state(state, self.root)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["decision"], "SEED_ONLY_NOT_A_HYPOTHESIS")

    def test_external_input_requires_materialized_coverage_preflight(self) -> None:
        state = self._state()
        state["action"]["external_input"] = True
        with self.assertRaises(ControllerError):
            evaluate_controller_state(state, self.root)

    def test_batch_required_family_rejects_serial_micro_experiment(self) -> None:
        state = self._state()
        state["families"][0]["batch_required"] = True
        with self.assertRaises(ControllerError):
            evaluate_controller_state(state, self.root)

    def test_evaluation_policy_rejects_optimistic_diagnostic_split(self) -> None:
        state = self._state()
        self._add_evaluation_policy(state)
        state["action"]["evaluation_plan"] = {
            "selection_profile": "session_group",
            "stress_profiles": ["worst_fold"],
            "official_feedback_round": 0,
        }
        result = evaluate_controller_state(state, self.root)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["decision"], "SELECTION_DISTRIBUTION_MISMATCH")

    def test_evaluation_policy_requires_stress_and_caps_official_feedback(self) -> None:
        state = self._state()
        self._add_evaluation_policy(state)
        state["action"]["evaluation_plan"] = {
            "selection_profile": "objective_cold",
            "stress_profiles": [],
            "official_feedback_round": 0,
        }
        missing = evaluate_controller_state(state, self.root)
        self.assertEqual(missing["decision"], "STRESS_PROFILE_MISSING")
        state["action"]["evaluation_plan"] = {
            "selection_profile": "objective_cold",
            "stress_profiles": ["worst_fold"],
            "official_feedback_round": 2,
        }
        feedback = evaluate_controller_state(state, self.root)
        self.assertEqual(feedback["decision"], "OFFICIAL_FEEDBACK_BUDGET_EXCEEDED")

    def test_evaluation_policy_allows_registered_profiles(self) -> None:
        state = self._state()
        self._add_evaluation_policy(state)
        state["action"]["evaluation_plan"] = {
            "selection_profile": "objective_cold",
            "stress_profiles": ["worst_fold"],
            "official_feedback_round": 1,
        }
        result = evaluate_controller_state(state, self.root)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["decision"], "RUN_EXPERIMENT")

    def test_user_issue_policy_waits_then_allows_frozen_issue(self) -> None:
        state = self._state()
        state["hypothesis_source"] = {
            "mode": "user_github_issue",
            "repository": "example/competition",
            "allowed_authors": ["researcher"],
        }
        waiting = evaluate_controller_state(state, self.root)
        self.assertEqual(waiting["decision"], "WAITING_FOR_USER_ISSUE")
        self.assertFalse(waiting["allowed"])
        self._add_user_issue(state)
        self.assertEqual(evaluate_controller_state(state, self.root)["decision"], "RUN_EXPERIMENT")

    def test_user_issue_policy_rejects_wrong_author_and_stale_snapshot(self) -> None:
        state = self._state()
        state["hypothesis_source"] = {
            "mode": "user_github_issue",
            "repository": "example/competition",
            "allowed_authors": ["researcher"],
        }
        issue_path = self._add_user_issue(state)
        saved = json.loads(issue_path.read_text(encoding="utf-8"))
        saved["author"]["login"] = "other"
        issue_path.write_text(json.dumps(saved) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ControllerError, "hash mismatch"):
            evaluate_controller_state(state, self.root)
        digest = fingerprint_path(issue_path)["sha256"]
        state["state_inputs"][1]["sha256"] = digest
        state["action"]["source_issue"]["snapshot"]["sha256"] = digest
        with self.assertRaisesRegex(ControllerError, "author does not match"):
            evaluate_controller_state(state, self.root)


if __name__ == "__main__":
    unittest.main()
