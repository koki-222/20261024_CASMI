"""Tests for the single-shot Kaggle status collector."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.kaggle_elapsed_time import (
    KaggleStatusError,
    classify_submission_status,
    collect_submission_status,
    main,
    normalize_submission_obj,
)


class Submission:
    """Minimal fake resembling a Kaggle API submission object."""

    def __init__(
        self, sid: str, status: str, score: object = None, submitted: object = None
    ) -> None:
        self.id = sid
        self.status = status
        self.public_score = score
        self.submissionTime = submitted
        self.fileName = f"{sid}.csv"
        self.description = sid


class FakeClient:
    """Injectable fake with the modern page_size call shape."""

    def __init__(self, submissions: list[Submission]) -> None:
        self.submissions = submissions
        self.calls: list[tuple[str, dict[str, object]]] = []

    def competition_submissions(
        self, competition: str, **kwargs: object
    ) -> list[Submission]:
        self.calls.append((competition, kwargs))
        return self.submissions


class LegacyFakeClient:
    """Injectable fake accepting only the older positional call shape."""

    def __init__(self, submissions: list[Submission]) -> None:
        self.submissions = submissions
        self.calls = 0

    def competition_submissions(self, competition: str) -> list[Submission]:
        self.calls += 1
        return self.submissions


class KaggleElapsedTimeTests(unittest.TestCase):
    """Ensure status observations are finite, explicit, and scheduler-friendly."""

    def test_complete_zero_score_is_scored(self) -> None:
        obj = Submission("zero", "COMPLETE", 0.0, "2026-10-15T12:00:00Z")
        sid, status, submitted, score, _, _ = normalize_submission_obj(obj)
        self.assertEqual((sid, status, score), ("zero", "complete", 0.0))
        self.assertEqual(submitted, datetime(2026, 10, 15, 12, tzinfo=timezone.utc))
        self.assertEqual(classify_submission_status(status, score), "scored")

    def test_complete_without_score_is_not_success(self) -> None:
        snapshot = collect_submission_status(
            "demo",
            client=FakeClient([Submission("unscored", "COMPLETE")]),
            now=datetime(2026, 10, 15, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(snapshot["status"], "completed_unscored")
        self.assertEqual(
            snapshot["submissions"][0]["classification"], "completed_unscored"
        )  # type: ignore[index]
        self.assertIsNone(snapshot["next_check_at"])

    def test_pending_remains_pending_even_with_score_and_has_next_check(self) -> None:
        snapshot = collect_submission_status(
            "demo",
            client=FakeClient([Submission("pending", "SCORING", 0.9)]),
            now=datetime(2026, 10, 15, 12, tzinfo=timezone.utc),
            next_check_seconds=90,
        )
        self.assertEqual(snapshot["status"], "pending")
        self.assertEqual(snapshot["submissions"][0]["classification"], "pending")  # type: ignore[index]
        self.assertEqual(snapshot["next_check_at"], "2026-10-15T12:01:30Z")

    def test_unknown_status_is_explicit(self) -> None:
        self.assertEqual(classify_submission_status("future_status", 1.0), "unknown")
        snapshot = collect_submission_status(
            "demo", client=FakeClient([Submission("x", "FUTURE_STATUS", 1.0)])
        )
        self.assertEqual(snapshot["status"], "unknown")

    def test_nonfinite_score_is_unscored(self) -> None:
        snapshot = collect_submission_status(
            "demo", client=FakeClient([Submission("x", "COMPLETE", float("nan"))])
        )
        self.assertEqual(snapshot["status"], "completed_unscored")
        self.assertIsNone(snapshot["submissions"][0]["score"])  # type: ignore[index]

    def test_legacy_one_argument_api_shape_is_supported_once(self) -> None:
        client = LegacyFakeClient([Submission("x", "COMPLETE", 0.2)])
        snapshot = collect_submission_status("demo", client=client)
        self.assertEqual(snapshot["status"], "scored")
        self.assertEqual(client.calls, 1)

    def test_unsupported_collection_is_not_silently_empty(self) -> None:
        class EmptyClient:
            def competition_submissions(
                self, competition: str, **kwargs: object
            ) -> None:
                return None

        with self.assertRaisesRegex(KaggleStatusError, "unsupported"):
            collect_submission_status("demo", client=EmptyClient())

    def test_cli_writes_one_snapshot_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "status.json"
            client = FakeClient([Submission("x", "PENDING")])
            self.assertEqual(
                main(
                    [
                        "--competition",
                        "demo",
                        "--output",
                        str(output),
                        "--next-check-seconds",
                        "30",
                    ],
                    client=client,
                ),
                0,
            )
            self.assertTrue(output.is_file())
            self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
