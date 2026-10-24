"""Regression tests for the dependency-aware work-queue planner."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone

from tools.competition_automation.scheduler import SchedulerError, plan_work_queue

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def job(
    job_id: str,
    status: str = "ready",
    *,
    depends_on: list[str] | None = None,
    resources: dict[str, float] | None = None,
    estimated_seconds: float = 10,
    priority: int = 0,
    deadline: str | None = None,
    next_check_at: str | None = None,
    candidate: bool = False,
) -> dict[str, object]:
    """Build a compact valid scheduler job for tests."""
    value: dict[str, object] = {
        "id": job_id,
        "status": status,
        "depends_on": depends_on or [],
        "resources": resources or {},
        "estimated_seconds": estimated_seconds,
        "priority": priority,
    }
    if deadline is not None:
        value["deadline"] = deadline
    if next_check_at is not None:
        value["next_check_at"] = next_check_at
    if candidate:
        value["candidate"] = True
    return value


def plan_job(result: dict[str, object], job_id: str) -> dict[str, object]:
    """Return one per-job planning result."""
    return next(item for item in result["jobs"] if item["id"] == job_id)  # type: ignore[index]


class SchedulerTests(unittest.TestCase):
    """Ensure waits, dependencies, resources, and deadlines stay deterministic."""

    def test_future_scoring_wait_does_not_block_independent_cpu_work(self) -> None:
        result = plan_work_queue(
            {
                "schema_version": 1,
                "resources": {"cpu": 2},
                "jobs": [
                    job("score-poll", "waiting", next_check_at="2026-10-15T13:00:00Z"),
                    job("independent", resources={"cpu": 1}, priority=1),
                ],
            },
            NOW,
        )

        self.assertEqual(result["selected_jobs"], ["independent"])
        self.assertEqual(result["due_waiting_jobs"], [])
        self.assertEqual(result["next_wake_at"], "2026-10-15T13:00:00Z")

    def test_due_poll_is_reported_but_ready_work_has_precedence(self) -> None:
        result = plan_work_queue(
            {
                "schema_version": 1,
                "resources": {"cpu": 1},
                "jobs": [
                    job("score-poll", "waiting", next_check_at="2026-10-15T11:00:00Z"),
                    job("local-check", resources={"cpu": 1}),
                ],
            },
            NOW,
        )

        self.assertEqual(result["selected_jobs"], ["local-check"])
        self.assertEqual(result["due_waiting_jobs"], ["score-poll"])
        self.assertIsNone(result["handoff"])

    def test_resource_contention_selects_only_fitting_jobs(self) -> None:
        result = plan_work_queue(
            {
                "schema_version": 1,
                "resources": {"gpu": 1},
                "jobs": [
                    job("running", "running", resources={"gpu": 1}),
                    job("blocked", resources={"gpu": 1}, priority=10),
                    job("cpu-free", priority=1),
                ],
            },
            NOW,
        )

        self.assertEqual(result["selected_jobs"], ["cpu-free"])
        self.assertEqual(plan_job(result, "blocked")["reason"], "resource_contention")
        self.assertEqual(result["resource_usage"], {"gpu": 1.0})

    def test_deadline_uses_graph_critical_path_preparation_and_downstream(self) -> None:
        result = plan_work_queue(
            {
                "schema_version": 1,
                "resources": {},
                "preparation_seconds": 5,
                "downstream_seconds": 7,
                "deadline": "2026-10-15T12:01:00Z",
                "jobs": [
                    job("prepare-model", estimated_seconds=20),
                    job("evaluate", depends_on=["prepare-model"], estimated_seconds=15),
                ],
            },
            NOW,
        )

        self.assertEqual(result["critical_path_seconds"], 47.0)
        self.assertEqual(
            plan_job(result, "prepare-model")["critical_path_seconds"], 47.0
        )
        self.assertEqual(
            plan_job(result, "prepare-model")["latest_start_at"], "2026-10-15T12:00:13Z"
        )
        self.assertEqual(
            plan_job(result, "evaluate")["latest_start_at"], "2026-10-15T12:00:33Z"
        )

    def test_daily_candidate_shortfall_prioritizes_distinct_candidate(self) -> None:
        result = plan_work_queue(
            {
                "schema_version": 1,
                "resources": {"cpu": 1},
                "daily_plan": {
                    "target_ready_candidates": 2,
                    "ready_candidates": 0,
                    "distinct_candidates_validated": True,
                    "reset_at": "2026-10-15T23:59:00Z",
                    "reserve_seconds": 3600,
                    "candidate_job_ids": ["candidate"],
                },
                "jobs": [
                    job("routine", resources={"cpu": 1}, priority=100),
                    job("candidate", resources={"cpu": 1}, priority=0),
                ],
            },
            NOW,
        )

        self.assertEqual(result["selected_jobs"], ["candidate"])
        self.assertEqual(result["daily_plan"]["shortfall"], 2)  # type: ignore[index]

    def test_daily_reserve_deadline_reaches_candidate_ancestors(self) -> None:
        result = plan_work_queue(
            {
                "schema_version": 1,
                "resources": {"cpu": 1},
                "daily_plan": {
                    "target_ready_candidates": 1,
                    "ready_candidates": 0,
                    "distinct_candidates_validated": True,
                    "reset_at": "2026-10-15T13:00:00Z",
                    "reserve_seconds": 600,
                    "candidate_job_ids": ["candidate"],
                },
                "jobs": [
                    job("ancestor", resources={"cpu": 1}, estimated_seconds=20),
                    job("candidate", depends_on=["ancestor"], estimated_seconds=60),
                    job("routine", resources={"cpu": 1}, priority=100),
                ],
            },
            NOW,
        )

        self.assertEqual(result["selected_jobs"], ["ancestor"])
        self.assertEqual(
            plan_job(result, "ancestor")["latest_start_at"], "2026-10-15T12:48:40Z"
        )
        self.assertEqual(
            plan_job(result, "candidate")["latest_start_at"], "2026-10-15T12:49:00Z"
        )

    def test_completed_graph_has_no_remaining_critical_path_or_wake(self) -> None:
        result = plan_work_queue(
            {
                "schema_version": 1,
                "resources": {},
                "deadline": "2026-10-15T11:00:00Z",
                "jobs": [
                    job("train", "completed", estimated_seconds=100),
                    job(
                        "score",
                        "completed",
                        depends_on=["train"],
                        estimated_seconds=100,
                    ),
                ],
            },
            NOW,
        )

        self.assertEqual(result["critical_path_seconds"], 0.0)
        self.assertEqual(result["handoff"]["reason"], "no_pending_work")  # type: ignore[index]
        self.assertIsNone(result["next_wake_at"])

    def test_past_deadline_does_not_create_immediate_wake_loop(self) -> None:
        result = plan_work_queue(
            {
                "schema_version": 1,
                "resources": {"cpu": 1},
                "jobs": [
                    job(
                        "running",
                        "running",
                        resources={"cpu": 1},
                        next_check_at="2026-10-15T13:00:00Z",
                    ),
                    job("late", deadline="2026-10-15T11:00:00Z", resources={"cpu": 1}),
                ],
            },
            NOW,
        )

        self.assertEqual(result["selected_jobs"], [])
        self.assertEqual(result["deadline_missed_jobs"], ["late"])
        self.assertEqual(result["next_wake_at"], "2026-10-15T13:00:00Z")
        self.assertEqual(result["handoff"]["reason"], "deadline_missed")  # type: ignore[index]
        self.assertFalse(plan_job(result, "late")["deadline_feasible"])

    def test_missing_dependency_and_cycle_are_rejected(self) -> None:
        with self.assertRaisesRegex(SchedulerError, "missing dependency"):
            plan_work_queue(
                {
                    "schema_version": 1,
                    "resources": {},
                    "jobs": [job("a", depends_on=["missing"])],
                },
                NOW,
            )
        with self.assertRaisesRegex(SchedulerError, "dependency cycle"):
            plan_work_queue(
                {
                    "schema_version": 1,
                    "resources": {},
                    "jobs": [job("a", depends_on=["b"]), job("b", depends_on=["a"])],
                },
                NOW,
            )

    def test_impossible_capacity_and_nan_are_rejected(self) -> None:
        with self.assertRaisesRegex(SchedulerError, "exceeds capacity"):
            plan_work_queue(
                {
                    "schema_version": 1,
                    "resources": {"gpu": 1},
                    "jobs": [job("too-large", resources={"gpu": 2})],
                },
                NOW,
            )
        with self.assertRaisesRegex(SchedulerError, "finite"):
            plan_work_queue(
                {
                    "schema_version": 1,
                    "resources": {"cpu": 1},
                    "jobs": [job("nan", estimated_seconds=math.nan)],
                },
                NOW,
            )

    def test_no_runnable_work_returns_native_scheduler_handoff(self) -> None:
        result = plan_work_queue(
            {
                "schema_version": 1,
                "resources": {},
                "jobs": [
                    job("waiting", "waiting", next_check_at="2026-10-15T13:00:00Z")
                ],
            },
            NOW,
        )

        self.assertEqual(result["selected_jobs"], [])
        self.assertEqual(
            result["handoff"],
            {
                "kind": "native_scheduler",
                "reason": "dependencies_or_waiting",
                "wake_at": "2026-10-15T13:00:00Z",
            },
        )


if __name__ == "__main__":
    unittest.main()
