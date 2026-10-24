"""Collect one Kaggle submission-status snapshot.

This module intentionally performs one API observation and exits. A native
scheduler can invoke it again at the returned ``next_check_at``; keeping the
wait outside this process prevents a score poll from blocking independent
experiments. Kaggle is imported and authenticated lazily, so importing this
module never requires credentials or network access.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol


class KaggleStatusError(RuntimeError):
    """Raised when a Kaggle status observation cannot be interpreted safely."""


class SubmissionClient(Protocol):
    """Small client interface used by the collector and its injected fakes."""

    def competition_submissions(self, competition: str, **kwargs: Any) -> Iterable[Any]:
        """Return the submission objects exposed by the Kaggle API."""


def _utc_now(now: datetime | None = None) -> datetime:
    """Return an aware UTC clock value."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _format_timestamp(value: datetime | None) -> str | None:
    """Serialize an aware timestamp in canonical UTC form."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    """Parse API timestamps, treating legacy naive API values as UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _status_text(value: object) -> str:
    """Normalize enum-like API status values without inventing a status."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _finite_score(value: object) -> float | None:
    """Return a finite score, preserving zero and rejecting NaN/infinity."""
    if value is None or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def normalize_submission_obj(
    obj: Any,
) -> tuple[str, str, datetime | None, float | None, str, str]:
    """Extract stable fields from one Kaggle submission object.

    The tuple remains compatible with the original helper:
    ``submission_id, status, submitted_at, score, filename, description``.
    The timestamp is always timezone-aware UTC when present. A score of
    ``0.0`` is retained, while non-numeric and non-finite scores become
    ``None``.
    """
    sid_raw = (
        getattr(obj, "id", None)
        or getattr(obj, "ref", None)
        or getattr(obj, "submissionId", None)
    )
    sid = str(sid_raw) if sid_raw is not None else ""
    raw_status = getattr(obj, "status", None)
    if raw_status is None:
        raw_status = getattr(obj, "state", None)
    status = _status_text(raw_status)
    raw_time = (
        getattr(obj, "submissionTime", None)
        or getattr(obj, "date", None)
        or getattr(obj, "createdAt", None)
        or getattr(obj, "created", None)
    )
    submitted_at = _parse_timestamp(raw_time)
    score_raw = None
    for attr in ("public_score", "publicScore", "score"):
        value = getattr(obj, attr, None)
        if value is not None:
            score_raw = value
            break
    filename = getattr(obj, "fileName", None) or getattr(obj, "filename", None) or ""
    description = getattr(obj, "description", None) or getattr(obj, "notes", None) or ""
    return (
        sid,
        status,
        submitted_at,
        _finite_score(score_raw),
        str(filename),
        str(description),
    )


def classify_submission_status(status: str, score: float | None) -> str:
    """Classify one API observation without treating a missing score as success."""
    normalized = _status_text(status)
    if normalized in {"complete", "completed"}:
        return (
            "scored"
            if score is not None and math.isfinite(score)
            else "completed_unscored"
        )
    if normalized in {"pending", "scoring", "running", "queued", "submitted"}:
        return "pending"
    if normalized in {"error", "failed", "failure", "cancelled", "canceled"}:
        return "error"
    return "unknown"


def _lazy_client() -> SubmissionClient:
    """Create and authenticate the Kaggle client only when a collection runs."""
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
    except ImportError as exc:
        raise KaggleStatusError("Kaggle API client is not installed") from exc
    client = KaggleApi()
    client.authenticate()
    return client


def _competition_submissions_all(comp: str, client: SubmissionClient) -> list[Any]:
    """Make one supported submissions call and reject unsupported pagination."""
    try:
        try:
            items = client.competition_submissions(comp, page_size=1000)
        except TypeError:
            items = client.competition_submissions(comp)
    except Exception as exc:
        raise KaggleStatusError(f"Kaggle submission lookup failed: {exc}") from exc
    if items is None or isinstance(items, (str, bytes, dict)):
        raise KaggleStatusError(
            "Kaggle API returned an unsupported submission collection"
        )
    try:
        return list(items)
    except TypeError as exc:
        raise KaggleStatusError(
            "Kaggle API returned a non-iterable submission collection"
        ) from exc


def get_all_submissions(comp: str, client: SubmissionClient | None = None) -> list[Any]:
    """Return one submission-list observation using an injected or lazy client."""
    if not isinstance(comp, str) or not comp.strip():
        raise KaggleStatusError("competition must be a non-empty string")
    return _competition_submissions_all(comp.strip(), client or _lazy_client())


def collect_submission_status(
    competition: str,
    *,
    client: SubmissionClient | None = None,
    now: datetime | None = None,
    next_check_seconds: int = 300,
) -> dict[str, Any]:
    """Collect one API snapshot and return a scheduler handoff when pending."""
    if (
        isinstance(next_check_seconds, bool)
        or not isinstance(next_check_seconds, int)
        or next_check_seconds <= 0
    ):
        raise KaggleStatusError("next_check_seconds must be a positive integer")
    observed_at = _utc_now(now)
    records: list[dict[str, Any]] = []
    for obj in get_all_submissions(competition, client):
        sid, status, submitted_at, score, filename, description = (
            normalize_submission_obj(obj)
        )
        records.append(
            {
                "submission_id": sid,
                "status": status,
                "classification": classify_submission_status(status, score),
                "score": score,
                "submitted_at": _format_timestamp(submitted_at),
                "filename": filename,
                "description": description,
                "observed_at": _format_timestamp(observed_at),
                "evidence_type": "kaggle_api_observation",
            }
        )
    records.sort(key=lambda item: (item["submitted_at"] or "", item["submission_id"]))
    classifications = {item["classification"] for item in records}
    if not records:
        overall = "no_submissions"
    elif "pending" in classifications:
        overall = "pending"
    elif "unknown" in classifications:
        overall = "unknown"
    elif "scored" in classifications:
        overall = "scored"
    elif "completed_unscored" in classifications:
        overall = "completed_unscored"
    else:
        overall = "error"
    next_check = (
        observed_at + timedelta(seconds=next_check_seconds)
        if "pending" in classifications
        else None
    )
    return {
        "competition": competition,
        "observed_at": _format_timestamp(observed_at),
        "status": overall,
        "submissions": records,
        "next_check_at": _format_timestamp(next_check),
        "evidence_type": "kaggle_api_observation",
    }


def write_snapshot(snapshot: dict[str, Any], output: Path) -> None:
    """Write one deterministic JSON observation to an optional output path."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main(
    argv: list[str] | None = None, *, client: SubmissionClient | None = None
) -> int:
    """Run one status collection and optionally write its JSON snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competition", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--next-check-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    snapshot = collect_submission_status(
        args.competition,
        client=client,
        next_check_seconds=args.next_check_seconds,
    )
    if args.output is not None:
        write_snapshot(snapshot, args.output)
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    main()
