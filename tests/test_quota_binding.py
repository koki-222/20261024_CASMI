"""Tests for rule-backed quota binding to the generic work queue."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from tools.competition_automation.cli import main
from tools.competition_automation.fingerprint import fingerprint_path
from tools.competition_automation.quota import build_daily_plan
from tools.competition_automation.scheduler import (
    SchedulerError,
    bind_daily_quota,
    plan_work_queue,
)


class QuotaBindingTests(unittest.TestCase):
    """Ensure official reset and materialized candidate facts override queue claims."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "automation").mkdir()
        (self.root / "rules.html").write_text("official rules\n", encoding="utf-8")
        (self.root / "submission.csv").write_text("id,pred\n1,0\n", encoding="utf-8")
        (self.root / "submission-check.json").write_text(
            '{"status":"PASS"}\n', encoding="utf-8"
        )
        self.profile_path = self.root / "automation" / "quota.json"
        self.ledger_path = self.root / "automation" / "ledger.jsonl"
        rules_hash = fingerprint_path(self.root / "rules.html")["sha256"]
        self.profile = {
            "schema_version": 1,
            "platform": "generic",
            "competition_slug": "demo",
            "period": "daily",
            "timezone": "Asia/Tokyo",
            "reset_at_local": "09:30",
            "scope": "competition",
            "max_submissions_per_period": 2,
            "target_ready_artifacts_per_period": 1,
            "count_failed_attempts": False,
            "source": {
                "kind": "official_rules",
                "uri": "https://example.test/rules",
                "observed_at": "2026-10-01T00:00:00Z",
                "artifact_path": "rules.html",
                "sha256": rules_hash,
            },
        }
        self.profile_path.write_text(json.dumps(self.profile), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _record(self, *, validated: bool) -> dict[str, object]:
        payload_hash = fingerprint_path(self.root / "submission.csv")["sha256"]
        evidence_hash = fingerprint_path(self.root / "submission-check.json")["sha256"]
        return {
            "record_id": "candidate-1",
            "submission_status": "ready",
            "ready_at": "2026-10-14T02:00:00Z",
            "submission_artifact_sha256": [payload_hash],
            "artifacts": [
                {"path": "submission.csv", "sha256": payload_hash},
                {"path": "submission-check.json", "sha256": evidence_hash},
            ],
            "checks": (
                {
                    "submission": {
                        "status": "PASS",
                        "evidence_path": "submission-check.json",
                        "evidence_sha256": evidence_hash,
                    }
                }
                if validated
                else {}
            ),
        }

    def test_build_daily_plan_returns_next_reset_for_non_midnight_timezone(
        self,
    ) -> None:
        result = build_daily_plan(
            self.profile,
            [self._record(validated=True)],
            now=datetime(2026, 10, 15, 0, tzinfo=timezone.utc),
            root=self.root,
        )

        self.assertEqual(result["distinct_ready_artifacts"], 1)
        self.assertEqual(result["next_reset_at"], "2026-10-15T00:30:00Z")

        after_reset = build_daily_plan(
            self.profile,
            [],
            now=datetime(2026, 10, 15, 1, tzinfo=timezone.utc),
            root=self.root,
        )
        self.assertEqual(after_reset["next_reset_at"], "2026-10-16T00:30:00Z")

    def test_unvalidated_ready_artifact_is_not_counted(self) -> None:
        result = build_daily_plan(
            self.profile,
            [self._record(validated=False)],
            now=datetime(2026, 10, 15, 0, tzinfo=timezone.utc),
            root=self.root,
        )

        self.assertEqual(result["distinct_ready_artifacts"], 0)
        self.assertEqual(result["unvalidated_ready_record_ids"], ["candidate-1"])

    def test_bind_overwrites_self_reported_quota_and_preserves_queue_fields(
        self,
    ) -> None:
        self.ledger_path.write_text(
            json.dumps(self._record(validated=True)) + "\n", encoding="utf-8"
        )
        original = {
            "schema_version": 1,
            "resources": {"cpu": 1},
            "jobs": [
                {
                    "id": "candidate",
                    "status": "ready",
                    "depends_on": [],
                    "resources": {"cpu": 1},
                    "estimated_seconds": 10,
                    "priority": 0,
                }
            ],
            "daily_plan": {
                "target_ready_candidates": 999,
                "ready_candidates": 999,
                "distinct_candidates_validated": False,
                "reset_at": "2026-01-01T00:00:00Z",
                "reserve_seconds": 120,
                "candidate_job_ids": ["candidate"],
            },
        }
        bound, quota = bind_daily_quota(
            original,
            self.profile_path,
            self.ledger_path,
            self.root,
            now=datetime(2026, 10, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(bound["daily_plan"]["target_ready_candidates"], 1)  # type: ignore[index]
        self.assertEqual(bound["daily_plan"]["ready_candidates"], 1)  # type: ignore[index]
        self.assertEqual(bound["daily_plan"]["reset_at"], "2026-10-15T00:30:00Z")  # type: ignore[index]
        self.assertEqual(bound["daily_plan"]["next_reset_at"], "2026-10-15T00:30:00Z")  # type: ignore[index]
        self.assertEqual(bound["daily_plan"]["reserve_seconds"], 120.0)  # type: ignore[index]
        self.assertEqual(bound["daily_plan"]["candidate_job_ids"], ["candidate"])  # type: ignore[index]
        self.assertEqual(quota["next_reset_at"], "2026-10-15T00:30:00Z")
        self.assertEqual(original["daily_plan"]["target_ready_candidates"], 999)  # type: ignore[index]
        planned = plan_work_queue(bound, datetime(2026, 10, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(planned["daily_plan"]["shortfall"], 0)

    def test_failed_report_cannot_count_as_a_ready_candidate(self) -> None:
        (self.root / "submission-check.json").write_text(
            '{"status":"FAIL"}', encoding="utf-8"
        )
        record = self._record(validated=True)
        result = build_daily_plan(
            self.profile,
            [record],
            datetime(2026, 10, 15, 0, tzinfo=timezone.utc),
            self.root,
        )
        self.assertEqual(result["distinct_ready_artifacts"], 0)

    def test_queue_cli_uses_verified_quota_and_requires_complete_binding(self) -> None:
        plan_path = self.root / "queue.json"
        plan_path.write_text(
            json.dumps({"schema_version": 1, "resources": {}, "jobs": []}),
            encoding="utf-8",
        )
        output = StringIO()
        with redirect_stdout(output):
            status = main(
                [
                    "queue-plan",
                    str(plan_path),
                    "--quota",
                    str(self.profile_path),
                    "--ledger",
                    str(self.ledger_path),
                    "--root",
                    str(self.root),
                    "--now",
                    "2026-10-15T00:00:00Z",
                ]
            )
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["quota"]["distinct_ready_artifacts"], 0)
        self.assertEqual(result["daily_plan"]["shortfall"], 1)
        self.assertEqual(
            result["daily_quota_evidence"], "verified_profile_and_materialized_ledger"
        )
        with redirect_stdout(StringIO()):
            self.assertEqual(
                main(["queue-plan", str(plan_path), "--quota", str(self.profile_path)]),
                2,
            )

    def test_profile_reference_must_stay_below_root(self) -> None:
        with self.assertRaisesRegex(SchedulerError, "below root"):
            bind_daily_quota(
                {"schema_version": 1, "resources": {}, "jobs": []},
                Path("/tmp/quota.json"),
                self.ledger_path,
                self.root,
            )


if __name__ == "__main__":
    unittest.main()
