"""Rule-backed daily submission quota profiles and distinct-candidate plans."""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .fingerprint import canonical_json_digest, fingerprint_path


class QuotaError(ValueError):
    """Raised when quota rules or a daily candidate plan are invalid."""


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ALLOWED_KEYS = {
    "schema_version", "platform", "competition_slug", "period", "timezone",
    "reset_at_local", "scope", "max_submissions_per_period",
    "target_ready_artifacts_per_period", "count_failed_attempts", "source",
}


def _text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise QuotaError(f"{field} must be a non-empty string")


def _sha(value: object, field: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise QuotaError(f"{field} must be a lowercase SHA-256")


def _source_snapshot(source: dict[str, Any], root: Path) -> None:
    for field in ("kind", "uri", "observed_at", "artifact_path"):
        _text(source.get(field), f"source.{field}")
    _sha(source.get("sha256"), "source.sha256")
    root_resolved = root.expanduser().resolve()
    raw = root_resolved / str(source["artifact_path"])
    if raw.is_symlink():
        raise QuotaError("quota source snapshot must not be a symlink")
    snapshot = raw.resolve()
    try:
        snapshot.relative_to(root_resolved)
    except ValueError as exc:
        raise QuotaError("quota source snapshot escapes root") from exc
    if not snapshot.is_file():
        raise QuotaError(f"quota source snapshot is missing: {source['artifact_path']}")
    if fingerprint_path(snapshot)["sha256"] != source["sha256"]:
        raise QuotaError("quota source snapshot hash mismatch")


def validate_quota_profile(path: Path, root: Path) -> dict[str, Any]:
    """Load a quota profile and verify its official rules snapshot."""
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise QuotaError(f"quota profile is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise QuotaError(f"invalid quota profile: {path}: {exc}") from exc
    if not isinstance(profile, dict) or profile.get("schema_version") != 1:
        raise QuotaError("quota profile schema_version must be 1")
    unknown = sorted(set(profile).difference(ALLOWED_KEYS))
    if unknown:
        raise QuotaError(f"unknown quota field(s): {unknown}")
    for field in ("platform", "competition_slug", "period", "timezone", "reset_at_local", "scope"):
        _text(profile.get(field), field)
    if profile["period"] != "daily":
        raise QuotaError("only daily quota profiles are supported by this planner")
    try:
        ZoneInfo(profile["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise QuotaError(f"unknown timezone: {profile['timezone']}") from exc
    try:
        hour, minute = (int(part) for part in profile["reset_at_local"].split(":", 1))
        time(hour, minute)
    except (ValueError, TypeError) as exc:
        raise QuotaError("reset_at_local must be HH:MM") from exc
    for field in ("max_submissions_per_period", "target_ready_artifacts_per_period"):
        value = profile.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise QuotaError(f"{field} must be a positive integer")
    if profile["target_ready_artifacts_per_period"] > profile["max_submissions_per_period"]:
        raise QuotaError("target_ready_artifacts_per_period exceeds max_submissions_per_period")
    if not isinstance(profile.get("count_failed_attempts"), bool):
        raise QuotaError("count_failed_attempts must be boolean")
    source = profile.get("source")
    if not isinstance(source, dict):
        raise QuotaError("source must contain an official rules snapshot")
    _source_snapshot(source, root)
    return profile


def _parse_timestamp(value: object, field: str) -> datetime:
    _text(value, field)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuotaError(f"{field} must be ISO-8601") from exc


def _period_date(current: datetime, reset_at_local: str) -> str:
    hour, minute = (int(part) for part in reset_at_local.split(":", 1))
    boundary = time(hour, minute)
    date = current.date()
    if current.time().replace(tzinfo=None) < boundary:
        date -= timedelta(days=1)
    return date.isoformat()


def _next_reset_at(current: datetime, reset_at_local: str, timezone_name: str) -> datetime:
    """Return the next daily reset boundary in the profile timezone."""
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise QuotaError(f"unknown timezone: {timezone_name}") from exc
    hour, minute = (int(part) for part in reset_at_local.split(":", 1))
    local_now = current.astimezone(zone)
    boundary = datetime.combine(local_now.date(), time(hour, minute), tzinfo=zone)
    if local_now >= boundary:
        boundary += timedelta(days=1)
    return boundary.astimezone(timezone.utc)


def _submission_hashes(record: dict[str, Any]) -> set[str]:
    values = record.get("submission_artifact_sha256", [])
    if not isinstance(values, list):
        return set()
    return {value for value in values if isinstance(value, str) and SHA256_RE.fullmatch(value)}


def _materialized_submission(record: dict[str, Any], root: Path | None) -> bool:
    """Require a re-hashed submission-check result and payload files."""
    if root is None:
        return False
    checks = record.get("checks")
    submission_check = checks.get("submission") if isinstance(checks, dict) else None
    if not isinstance(submission_check, dict) or submission_check.get("status") != "PASS":
        return False
    evidence_path = submission_check.get("evidence_path")
    evidence_digest = submission_check.get("evidence_sha256")
    if not isinstance(evidence_path, str) or not SHA256_RE.fullmatch(str(evidence_digest)):
        return False
    root_resolved = root.expanduser().resolve()
    artifacts = record.get("artifacts", [])
    by_path = {
        artifact.get("path"): artifact.get("sha256")
        for artifact in artifacts
        if isinstance(artifact, dict)
    } if isinstance(artifacts, list) else {}
    if by_path.get(evidence_path) != evidence_digest:
        return False
    evidence_raw = root_resolved / evidence_path
    if evidence_raw.is_symlink():
        return False
    evidence = evidence_raw.resolve()
    try:
        evidence.relative_to(root_resolved)
    except ValueError:
        return False
    if not evidence.is_file() or fingerprint_path(evidence)["sha256"] != evidence_digest:
        return False
    try:
        report = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(report, dict) or report.get("status") != "PASS":
        return False
    hashes = _submission_hashes(record)
    if not hashes:
        return False
    payloads = {
        artifact.get("path"): artifact.get("sha256")
        for artifact in artifacts
        if isinstance(artifact, dict)
    }
    for digest in hashes:
        matching_paths = [path for path, value in payloads.items() if value == digest]
        if not matching_paths:
            return False
        verified = False
        for relative in matching_paths:
            if not isinstance(relative, str):
                continue
            raw = root_resolved / relative
            if raw.is_symlink():
                continue
            candidate = raw.resolve()
            try:
                candidate.relative_to(root_resolved)
            except ValueError:
                continue
            if candidate.exists() and fingerprint_path(candidate)["sha256"] == digest:
                verified = True
                break
        if not verified:
            return False
    return True


def build_daily_plan(
    profile: dict[str, Any],
    records: Iterable[dict[str, Any]],
    now: datetime | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Plan distinct, materialized ready candidates without fabricating seeds."""
    try:
        zone = ZoneInfo(profile["timezone"])
    except (KeyError, ZoneInfoNotFoundError) as exc:
        raise QuotaError("profile must be validated before planning") from exc
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    local_now = current.astimezone(zone)
    period_date = _period_date(local_now, profile["reset_at_local"])
    attempted_ids: list[str] = []
    ready_by_package: dict[str, str] = {}
    duplicate_ready_ids: list[str] = []
    unvalidated_ready_ids: list[str] = []
    seed_only_ids: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        record_id = record.get("record_id") or record.get("experiment_id")
        if not isinstance(record_id, str):
            continue
        submission_status = record.get("submission_status")
        if submission_status in {"submitted", "scored", "failed"} and record.get("submitted_at"):
            submitted_at = _parse_timestamp(record["submitted_at"], "submitted_at").astimezone(zone)
            if _period_date(submitted_at, profile["reset_at_local"]) == period_date and (
                submission_status != "failed" or profile["count_failed_attempts"]
            ):
                attempted_ids.append(record_id)
        if submission_status not in {"ready", "submitted", "scored"} or not record.get("ready_at"):
            continue
        if (
            record.get("control") is True
            or record.get("change_scope") == "seed_only"
            or record.get("seed_averaging") is True
        ):
            if isinstance(record_id, str):
                seed_only_ids.append(record_id)
            continue
        ready_at = _parse_timestamp(record["ready_at"], "ready_at").astimezone(zone)
        if _period_date(ready_at, profile["reset_at_local"]) != period_date:
            continue
        if not _materialized_submission(record, root):
            unvalidated_ready_ids.append(record_id)
            continue
        hashes = _submission_hashes(record)
        if not hashes:
            continue
        package_key = canonical_json_digest(sorted(hashes))
        if package_key in ready_by_package:
            duplicate_ready_ids.append(record_id)
        else:
            ready_by_package[package_key] = record_id
    target = int(profile["target_ready_artifacts_per_period"])
    max_submissions = int(profile["max_submissions_per_period"])
    ready_count = len(ready_by_package)
    attempted_count = len(set(attempted_ids))
    missing = max(0, target - ready_count)
    remaining_slots = max(0, max_submissions - attempted_count)
    if missing == 0:
        status = "COMPLETE"
    elif remaining_slots == 0:
        status = "BLOCKED_NO_DISTINCT_SLOTS"
    else:
        status = "NEEDS_DISTINCT_CANDIDATES"
    return {
        "status": status,
        "date": period_date,
        "timezone": profile["timezone"],
        "target_ready_artifacts": target,
        "distinct_ready_artifacts": ready_count,
        "missing_distinct_candidates": missing,
        "attempted_submissions": attempted_count,
        "remaining_submission_slots": remaining_slots,
        "ready_record_ids": sorted(set(ready_by_package.values())),
        "duplicate_ready_record_ids": sorted(set(duplicate_ready_ids)),
        "unvalidated_ready_record_ids": sorted(set(unvalidated_ready_ids)),
        "seed_only_record_ids": sorted(set(seed_only_ids)),
        "seed_only_excluded": True,
        "next_reset_at": _next_reset_at(local_now, profile["reset_at_local"], profile["timezone"]).isoformat().replace("+00:00", "Z"),
    }
