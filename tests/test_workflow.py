"""Bootstrap/publish/goal-loop behavior with no production network or compute."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.competition_automation import workflow as w
from tools.competition_automation.research import DEFAULT_REQUIRED_SECTIONS


class WorkflowTests(unittest.TestCase):
    """Exercise lifecycle boundaries using actual files and a local bare remote."""

    def setUp(self) -> None:
        """Create an isolated adapted competition with genuine hash references."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "work"
        self.root.mkdir()
        self.url = "https://www.kaggle.com/competitions/demo"
        self.repo = "https://github.com/example/demo"
        self.initialize = w.initialize(self.root, self.url)
        self.write(".gitignore", ".local/\n")
        self.ref = self.write("research/source.md", "Original public documentation.")
        contract = {
            "schema_version": 1,
            "competition": {"name": "Demo", "slug": "demo"},
            "platform": {"kind": "kaggle"},
            "data": {},
            "submission": {"format": "csv", "columns": []},
            "evaluation": {
                "metric": "auc",
                "direction": "max",
                "metric_domain": {"minimum": 0, "maximum": 1},
            },
            "split": {"strategy": "group"},
        }
        self.package = {
            "schema_version": 1,
            "contract": self.write("automation/contracts/current.json", contract),
            "research": self.write(
                "research/manifest.json",
                {
                    "schema_version": 1,
                    "competition_slug": "demo",
                    "sections": {
                        key: {
                            "sources": [
                                {
                                    "kind": "official",
                                    "uri": self.url,
                                    "observed_at": w._now(),
                                    "artifact_path": self.ref["path"],
                                    "sha256": self.ref["sha256"],
                                }
                            ]
                        }
                        for key in DEFAULT_REQUIRED_SECTIONS
                    },
                },
            ),
            "adaptation": self.write(
                "research/ADAPTATION.md", "Task-specific port and smoke checks."
            ),
            "evaluation": self.write(
                "automation/evaluation_design.json",
                {
                    "mode": "hybrid",
                    **{
                        key: "Recorded task-specific boundary"
                        for key in (
                            "population",
                            "leakage_boundary",
                            "exposure",
                            "selection_rule",
                            "limitations",
                        )
                    },
                },
            ),
            "coverage": [],
            "publication": [
                {
                    "artifact": self.ref,
                    "redistribution": "allowed",
                    "basis": "Original public fixture",
                }
            ],
        }
        for category in sorted(w.CATEGORIES):
            inventory = self.write(
                f"research/inventory/{category}.json",
                {
                    "schema_version": 1,
                    "scope": "All pages at observed time",
                    "observed_at": w._now(),
                    "pagination_complete": True,
                    "items": [
                        {
                            "uri": self.url,
                            "version": "v1",
                            "status": "archived",
                            "artifact": self.ref,
                        }
                    ],
                },
            )
            self.package["coverage"].append(
                {
                    "category": category,
                    "status": "complete",
                    "reason": "All listed fixture bodies archived",
                    "inventory": inventory,
                }
            )
        self.write("research/bootstrap_package.json", self.package)
        self.write(
            "docs/workflow/PRO_ISSUES_PROMPT.md",
            "{{COMPETITION_URL}}\n{{REPOSITORY}}\n{{COMMIT}}\n{{FILES}}\n{{COVERAGE}}",
        )
        self.goal = {
            "metric": "auc",
            "direction": "max",
            "scope": "public",
            "target": 0.95,
            "max_attempts": 3,
            "max_wall_seconds": 3600,
            "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
        self.write("automation/goal.json", self.goal)
        self.issue = {
            "number": 1,
            "snapshot": self.write(
                "research/issues/1.json",
                {
                    "number": 1,
                    "url": self.repo + "/issues/1",
                    "body": "Compare teachers on held-out groups.",
                },
            ),
        }
        self.write(
            "automation/intake.json",
            {
                "schema_version": 1,
                "issues": [
                    {"source_issue": self.issue, "scope_ids": ["teacher-comparison"]}
                ],
            },
        )

    def tearDown(self) -> None:
        """Discard test-only repositories and data."""
        self.temp.cleanup()

    def write(self, relative: str, value: object) -> dict[str, str]:
        """Write a fixture and return its content-bound identity."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
        )
        return w.snapshot(self.root, relative)

    def waiting(self) -> None:
        """Prepare a handoff fixture without asserting a real external push."""
        w.mark_ready(self.root, "research/bootstrap_package.json")
        state = w._state(self.root)
        state.update(
            phase="waiting_for_issues",
            publication={"repository": self.repo, "commit": "a" * 40},
        )
        w._write(self.root / w.STATE, state)
        w.handoff(self.root)

    def start(self) -> None:
        """Start one explicitly bounded test loop."""
        self.waiting()
        w.start_pdca(
            self.root, "automation/goal.json", "automation/intake.json", w.TRIGGER
        )

    def test_initialization_is_idempotent_but_not_cross_competition(self) -> None:
        """Repeated setup preserves phase and rejects an unrelated target."""
        self.assertEqual(w.initialize(self.root, self.url), self.initialize)
        with self.assertRaises(w.WorkflowError):
            w.initialize(self.root, self.url + "-other")

    def test_url_cannot_carry_credentials_or_search_tokens(self) -> None:
        """Only canonical URLs enter shared metadata."""
        for url in (
            "file:///tmp/x",
            "https://user:pass@example.com/c",
            self.url + "?token=abc",
        ):
            with self.subTest(url=url), self.assertRaises(w.WorkflowError):
                w.initialize(self.root, url)

    def test_handoff_requires_verified_publication(self) -> None:
        """A prepared local repo is not a successful push."""
        w.mark_ready(self.root, "research/bootstrap_package.json")
        with self.assertRaises(w.WorkflowError):
            w.handoff(self.root)

    def test_package_detects_missing_category_and_changed_body(self) -> None:
        """Coverage names alone do not substitute for archived bodies."""
        self.package["coverage"].pop()
        self.write("research/bootstrap_package.json", self.package)
        with self.assertRaises(w.WorkflowError):
            w.mark_ready(self.root, "research/bootstrap_package.json")

    def test_package_rejects_changed_body(self) -> None:
        """Demonstrate that a saved source cannot silently change after binding."""
        self.write("research/source.md", "Changed original")
        with self.assertRaises(ValueError):
            w.validate_package(self.root, "research/bootstrap_package.json")

    def test_complete_inventory_requires_body_and_pagination(self) -> None:
        """Unfinished pagination cannot be described as complete."""
        ref = self.package["coverage"][0]["inventory"]
        inventory = w._read(self.root / ref["path"])
        inventory["pagination_complete"] = False
        self.package["coverage"][0]["inventory"] = self.write(ref["path"], inventory)
        self.write("research/bootstrap_package.json", self.package)
        with self.assertRaisesRegex(w.WorkflowError, "pagination"):
            w.validate_package(self.root, "research/bootstrap_package.json")

    def test_restricted_material_is_not_publishable(self) -> None:
        """A licence gap does not inherit permission from a public URL."""
        self.package["publication"][0]["redistribution"] = "unknown"
        self.write("research/bootstrap_package.json", self.package)
        with self.assertRaisesRegex(w.WorkflowError, "restricted"):
            w.mark_ready(self.root, "research/bootstrap_package.json")

    def test_symlink_and_traversal_evidence_are_rejected(self) -> None:
        """Evidence must remain beneath the actual workspace without aliases."""
        (self.root / "alias").symlink_to(
            self.root / "research", target_is_directory=True
        )
        for name in ("alias/source.md", "../outside", "/etc/hosts"):
            with self.subTest(name=name), self.assertRaises(w.WorkflowError):
                w.snapshot(self.root, name)

    def test_handoff_uses_commit_pinned_links(self) -> None:
        """A changing branch cannot silently change Pro's evidence."""
        self.waiting()
        result = w.handoff(self.root)
        self.assertIn("/blob/" + "a" * 40, result["prompt"])
        self.assertEqual(result["next_message"], "PDCA開始")

    def test_start_requires_exact_trigger_and_numeric_goal(self) -> None:
        """Vague targets, non-finite values and invalid metric ranges fail."""
        self.waiting()
        with self.assertRaises(w.WorkflowError):
            w.start_pdca(
                self.root, "automation/goal.json", "automation/intake.json", "start"
            )
        for target in (None, True, "silver", float("inf"), 1.1):
            with self.subTest(target=target):
                self.write("automation/goal.json", dict(self.goal, target=target))
                with self.assertRaises(ValueError):
                    w.start_pdca(
                        self.root,
                        "automation/goal.json",
                        "automation/intake.json",
                        w.TRIGGER,
                    )

    def test_start_rejects_cross_repository_issues(self) -> None:
        """Only the explicitly handed-off repository is eligible."""
        self.waiting()
        issue = {
            "number": 1,
            "snapshot": self.write(
                "research/issues/other.json",
                {
                    "number": 1,
                    "url": "https://github.com/other/repo/issues/1",
                    "body": "Wrong scope",
                },
            ),
        }
        self.write(
            "automation/intake.json",
            {
                "schema_version": 1,
                "issues": [{"source_issue": issue, "scope_ids": ["other"]}],
            },
        )
        with self.assertRaisesRegex(w.WorkflowError, "published"):
            w.start_pdca(
                self.root, "automation/goal.json", "automation/intake.json", w.TRIGGER
            )

    def test_dispatch_requires_start_and_enforces_global_attempt_budget(self) -> None:
        """Actual claims, including failed retries, consume the bounded budget."""
        with self.assertRaises(w.WorkflowError):
            w.check_dispatch(self.root, self.issue, [], 30)
        self.start()
        w.check_dispatch(self.root, self.issue, [], 30)
        attempts = [{"created_at": w._now()}] * 3
        with self.assertRaisesRegex(w.WorkflowError, "attempt budget"):
            w.check_dispatch(self.root, self.issue, attempts, 30)

    def test_dispatch_rejects_changed_goal_issue_and_overlong_run(self) -> None:
        """Frozen intent and remaining wall time govern every new dispatch."""
        self.start()
        with self.assertRaisesRegex(w.WorkflowError, "finish in time"):
            w.check_dispatch(self.root, self.issue, [], 7200)
        self.write("research/issues/1.json", {"number": 1, "body": "Changed intent"})
        with self.assertRaisesRegex(w.WorkflowError, "snapshot changed"):
            w.check_dispatch(self.root, self.issue, [], 30)

    def score(self, **changes: object) -> dict[str, Any]:
        """Construct a normalized official record bound to an original fixture."""
        raw = {
            "competition_url": self.url,
            "kind": "official",
            "status": "scored",
            "preliminary": False,
            "metric": "auc",
            "direction": "max",
            "scope": "public",
            "score": 0.96,
            "submission_id": "123",
            "source_url": self.url + "/submissions",
            "observed_at": w._now(),
            **changes,
        }
        self.write(
            "evidence/ref.json",
            {
                "snapshot": self.write("evidence/normalized.json", raw),
                "native_snapshot": self.write(
                    "evidence/native.json",
                    {
                        "ref": "123",
                        "publicScore": str(raw["score"]),
                        "status": "COMPLETE",
                    },
                ),
                "native_row_path": [],
                "native_fields": {
                    "submission_id": "ref",
                    "score": "publicScore",
                    "status": "status",
                },
                "native_success_status": "COMPLETE",
            },
        )
        return raw

    def test_only_matching_official_finalized_score_achieves_target(self) -> None:
        """Proxy, errors, preliminary results and wrong scopes never complete."""
        self.start()
        for changes in (
            {"kind": "proxy"},
            {"status": "pending"},
            {"preliminary": True},
            {"scope": "private"},
            {"competition_url": self.url + "x"},
        ):
            with self.subTest(changes=changes):
                self.score(**changes)
                with self.assertRaises(w.WorkflowError):
                    w.observe_score(self.root, "evidence/ref.json")
        self.score(score=0.94)
        self.assertFalse(
            w.observe_score(self.root, "evidence/ref.json")["target_achieved"]
        )
        self.score()
        self.assertTrue(
            w.observe_score(self.root, "evidence/ref.json")["target_achieved"]
        )
        with self.assertRaises(w.WorkflowError):
            w.check_dispatch(self.root, self.issue, [], 30)

    def test_publication_pushes_once_to_local_bare_remote_and_verifies_head(
        self,
    ) -> None:
        """Exercise real Git objects and ref reconciliation without network writes."""
        remote = Path(self.temp.name) / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)], check=True, capture_output=True
        )
        w._git(self.root, "init", "-b", "main")
        w._git(self.root, "config", "user.name", "Test")
        w._git(self.root, "config", "user.email", "test@example.invalid")
        w._git(self.root, "remote", "add", "origin", str(remote))
        self.package["publication"].append(
            {
                "artifact": self.issue["snapshot"],
                "redistribution": "allowed",
                "basis": "Original fixture Issue",
            }
        )
        self.write("research/bootstrap_package.json", self.package)
        w.mark_ready(self.root, "research/bootstrap_package.json")
        w._git(self.root, "add", ".gitignore", "automation", "research", "docs")
        w._git(self.root, "commit", "-m", "fixture")
        real_git = w._git

        def local_git(root: Path, *args: str) -> str:
            """Replace displayed remote identity only; use real local Git transport."""
            return (
                self.repo + ".git"
                if args == ("remote", "get-url", "origin")
                else real_git(root, *args)
            )

        with patch.object(w, "_git", side_effect=local_git):
            with self.assertRaises(w.WorkflowError):
                w.publish(self.root, "origin", "main", False)
            state = w.publish(self.root, "origin", "main", True)
        self.assertEqual(state["phase"], "waiting_for_issues")
        self.assertEqual(
            state["publication"]["commit"], real_git(self.root, "rev-parse", "HEAD")
        )
        self.assertIn(
            state["publication"]["commit"],
            real_git(self.root, "ls-remote", "--heads", "origin", "refs/heads/main"),
        )

    def test_real_runtime_enforces_start_budget_and_completed_reuse(self) -> None:
        """The actual SQLite claim boundary enforces phase and the global cap."""
        from tests.test_runtime import RuntimeTests
        from tools.competition_automation.runtime import registry_status

        fixture = RuntimeTests()
        fixture.setUp()
        try:
            w.initialize(fixture.root, self.url)
            with self.assertRaises(w.WorkflowError):
                fixture.run_request()
            state = w._state(fixture.root)
            goal = fixture.snapshot(
                "goal.json", dict(self.goal, metric="score", max_attempts=1)
            )
            intake = fixture.snapshot("intake.json", {"issues": [1]})
            state["package"] = fixture.snapshot(
                "package.json",
                {
                    "contract": fixture.evidence("contract.json"),
                    "evaluation": fixture.evidence("exploration.json"),
                },
            )
            state.update(
                phase="pdca_running",
                loop={
                    "goal": goal,
                    "intake": intake,
                    "started_at": w._now(),
                    "issue_snapshots": [fixture.controller["action"]["source_issue"]],
                    "reviews": [],
                },
            )
            w._write(fixture.root / w.STATE, state)
            self.assertEqual(fixture.run_request()["status"], "completed")
            fixture.request["output_dir"] = "outputs/reused"
            self.assertEqual(fixture.run_request()["decision"], "REUSE_COMPLETED")
            fixture.spec["config"]["x"] = 2
            fixture.bind()
            fixture.request["output_dir"] = "outputs/new"
            with self.assertRaisesRegex(w.WorkflowError, "attempt budget"):
                fixture.run_request()
            self.assertEqual(
                registry_status(fixture.registry)["counts"], {"completed": 1}
            )
        finally:
            fixture.doCleanups()

    def test_legacy_workspace_does_not_gain_a_new_start_requirement(self) -> None:
        """Existing competitions remain on their old gates until explicit migration."""
        other = Path(self.temp.name) / "legacy"
        other.mkdir()
        w.check_dispatch(other, {}, [], 30)

    def test_false_exhaustion_cannot_close_a_running_goal(self) -> None:
        """No hypothesis-exhaustion shortcut may masquerade as a budget stop."""
        self.start()
        with self.assertRaisesRegex(w.WorkflowError, "actually been reached"):
            w.close_pdca(self.root, "budget_exhausted")

    def test_normalized_score_cannot_invent_a_native_improvement(self) -> None:
        """An LLM-authored official label cannot override the original score."""
        self.start()
        raw = self.score(score=0.94)
        ref = w._read(self.root / "evidence/ref.json")
        ref["snapshot"] = self.write("evidence/normalized.json", dict(raw, score=0.99))
        self.write("evidence/ref.json", ref)
        with self.assertRaisesRegex(w.WorkflowError, "differs from the native"):
            w.observe_score(self.root, "evidence/ref.json")
        self.assertEqual(w.workflow_status(self.root)["phase"], "pdca_running")

    def test_expired_budget_closes_unmet_and_refuses_new_dispatch(self) -> None:
        """A stopped loop is closed explicitly instead of remaining forever active."""
        self.start()
        state = w._state(self.root)
        state["loop"]["started_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        w._write(self.root / w.STATE, state)
        self.assertEqual(w.workflow_status(self.root)["decision"], "STOP_BUDGET_UNMET")
        self.assertEqual(
            w.close_pdca(self.root, "budget_exhausted")["phase"],
            "budget_exhausted_unmet",
        )
        with self.assertRaises(w.WorkflowError):
            w.check_dispatch(self.root, self.issue, [], 30)

    def test_competition_closure_needs_native_and_normalized_evidence(self) -> None:
        """Competition ending is not target achievement or a permanent blocker."""
        self.start()
        with self.assertRaises(w.WorkflowError):
            w.close_pdca(self.root, "competition_ended")
        raw = self.write(
            "evidence/closed.json",
            {
                "competition_url": self.url,
                "kind": "official",
                "status": "ended",
                "observed_at": w._now(),
            },
        )
        self.write(
            "evidence/closed-ref.json", {"snapshot": raw, "native_snapshot": self.ref}
        )
        self.assertEqual(
            w.close_pdca(self.root, "competition_ended", "evidence/closed-ref.json")[
                "phase"
            ],
            "competition_ended_unmet",
        )

    def test_minimization_goal_and_out_of_domain_score(self) -> None:
        """Lower-is-better metrics and impossible scores are handled explicitly."""
        contract = w._read(self.root / self.package["contract"]["path"])
        contract["evaluation"]["direction"] = "min"
        self.package["contract"] = self.write(
            self.package["contract"]["path"], contract
        )
        self.write("research/bootstrap_package.json", self.package)
        self.write("automation/goal.json", dict(self.goal, direction="min"))
        self.start()
        self.score(direction="min", score=-1)
        with self.assertRaisesRegex(w.WorkflowError, "domain"):
            w.observe_score(self.root, "evidence/ref.json")
        self.score(direction="min", score=0.94)
        self.assertTrue(
            w.observe_score(self.root, "evidence/ref.json")["target_achieved"]
        )

    def test_changed_published_package_cannot_start_pdca(self) -> None:
        """Pro's reviewed publication cannot silently change before execution."""
        self.waiting()
        self.write(
            "research/bootstrap_package.json", dict(self.package, unknown="changed")
        )
        with self.assertRaisesRegex(w.WorkflowError, "snapshot changed"):
            w.start_pdca(
                self.root, "automation/goal.json", "automation/intake.json", w.TRIGGER
            )

    def test_language_setup_preserves_shared_rules_and_user_direction(self) -> None:
        """Both locale switches must retain the new canonical entry points."""
        source = Path(__file__).resolve().parents[1]
        target = Path(self.temp.name) / "language-test"
        target.mkdir()
        shutil.copyfile(source / "setup.sh", target / "setup.sh")
        shutil.copytree(source / "locales", target / "locales")
        (target / "AGENTS.md").write_text("User shared contract", encoding="utf-8")
        (target / "KAGGLE_DIRECTION.md").write_text(
            "Active competition direction", encoding="utf-8"
        )
        for language in ("ja", "en"):
            subprocess.run(
                ["bash", str(target / "setup.sh"), language],
                check=True,
                capture_output=True,
            )
            self.assertEqual((target / "AGENTS.md").read_text(), "User shared contract")
            self.assertEqual(
                (target / "KAGGLE_DIRECTION.md").read_text(),
                "Active competition direction",
            )
            self.assertEqual((target / "CLAUDE.md").readlink(), Path("AGENTS.md"))


if __name__ == "__main__":
    unittest.main()
