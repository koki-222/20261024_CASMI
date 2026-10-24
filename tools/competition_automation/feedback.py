"""Compare two official results under competition-configured evidence gates."""

from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
from typing import Any

from .fingerprint import fingerprint_path


class FeedbackError(ValueError):
    """Raised when official-feedback comparison evidence is malformed."""


def _number(value: object, name: str, minimum: float = 0.0) -> float:
    """Return a finite number at or above the requested minimum."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeedbackError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise FeedbackError(f"{name} must be finite and >= {minimum}")
    return result


def _timestamp(value: object, name: str) -> datetime:
    """Parse a timezone-aware observation timestamp."""
    if not isinstance(value, str):
        raise FeedbackError(f"{name} must be ISO-8601")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackError(f"{name} must be ISO-8601") from exc
    if result.tzinfo is None:
        raise FeedbackError(f"{name} requires timezone")
    return result


def _evidence(value: object, root: Path, name: str) -> None:
    """Require an existing, in-root, hash-matching official source artifact."""
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise FeedbackError(f"{name} requires path and sha256")
    relative, digest = value["path"], value["sha256"]
    if not isinstance(relative, str) or not relative or not isinstance(digest, str):
        raise FeedbackError(f"{name} requires path and sha256")
    base = root.expanduser().resolve()
    raw = base / relative
    if raw.is_symlink():
        raise FeedbackError(f"{name} must not be a symlink")
    path = raw.resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise FeedbackError(f"{name} must be an in-root file")
    if fingerprint_path(path)["sha256"] != digest:
        raise FeedbackError(f"{name} hash mismatch")


def _observation(value: object, root: Path, name: str) -> dict[str, Any]:
    """Validate one normalized result without assuming a particular task format."""
    required = {
        "observation_id", "evaluation_id", "score", "observed_at",
        "n_observations", "cohort_counts", "recent_drift_per_observation",
        "source", "artifact",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise FeedbackError(f"{name} requires {sorted(required)}")
    for field in ("observation_id", "evaluation_id"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise FeedbackError(f"{name}.{field} is required")
    _number(value["score"], f"{name}.score", float("-inf"))
    _timestamp(value["observed_at"], f"{name}.observed_at")
    count = value["n_observations"]
    if count is not None and (
        isinstance(count, bool) or not isinstance(count, int) or count < 1
    ):
        raise FeedbackError(f"{name}.n_observations must be positive or null")
    cohorts = value["cohort_counts"]
    if cohorts is not None:
        if not isinstance(cohorts, dict) or not cohorts:
            raise FeedbackError(f"{name}.cohort_counts must be nonempty or null")
        for key, amount in cohorts.items():
            if (
                not isinstance(key, str)
                or not key
                or isinstance(amount, bool)
                or not isinstance(amount, int)
                or amount < 0
            ):
                raise FeedbackError(f"{name}.cohort_counts has invalid entries")
        if count is None or sum(cohorts.values()) != count:
            raise FeedbackError(f"{name}.cohort_counts must sum to n_observations")
    drift = value["recent_drift_per_observation"]
    if drift is not None:
        _number(drift, f"{name}.recent_drift_per_observation", float("-inf"))
    _evidence(value["source"], root, f"{name}.source")
    if value["artifact"] is not None:
        _evidence(value["artifact"], root, f"{name}.artifact")
    return value


def compare_official_feedback(plan: dict[str, Any], root: Path) -> dict[str, Any]:
    """Compare scores only when the configured evidence requirements pass.

    The caller must normalize source scores from platform evidence. This check
    verifies source hashes, not the semantics of arbitrary platform HTML/API.
    """
    required = {
        "schema_version", "direction", "requirements", "before", "after",
    }
    if not isinstance(plan, dict) or set(plan) != required or plan["schema_version"] != 2:
        raise FeedbackError("invalid feedback comparison schema")
    direction = plan["direction"]
    if direction not in {"min", "max"}:
        raise FeedbackError("direction must be min or max")
    requirements = plan["requirements"]
    fields = {"minimum_observations", "max_abs_recent_drift", "max_cohort_distance"}
    if not isinstance(requirements, dict) or set(requirements) != fields:
        raise FeedbackError("requirements must specify each optional gate")
    minimum = requirements["minimum_observations"]
    if minimum is not None and (
        isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1
    ):
        raise FeedbackError("minimum_observations must be positive or null")
    max_drift = requirements["max_abs_recent_drift"]
    if max_drift is not None:
        max_drift = _number(max_drift, "max_abs_recent_drift")
    max_distance = requirements["max_cohort_distance"]
    if max_distance is not None:
        max_distance = _number(max_distance, "max_cohort_distance")
        if max_distance > 1:
            raise FeedbackError("max_cohort_distance must be <= 1")
    before = _observation(plan["before"], root, "before")
    after = _observation(plan["after"], root, "after")
    reasons: list[str] = []
    if before["evaluation_id"] != after["evaluation_id"]:
        reasons.append("evaluation_mismatch")
    distance = None
    if max_distance is not None:
        if before["cohort_counts"] is None or after["cohort_counts"] is None:
            reasons.append("cohort_evidence_missing")
        else:
            keys = set(before["cohort_counts"]) | set(after["cohort_counts"])
            distance = sum(
                abs(before["cohort_counts"].get(key, 0) / before["n_observations"]
                    - after["cohort_counts"].get(key, 0) / after["n_observations"])
                for key in keys
            ) / 2
            if distance > max_distance:
                reasons.append("cohort_mismatch")
    for name, item in (("before", before), ("after", after)):
        if minimum is not None and (
            item["n_observations"] is None or item["n_observations"] < minimum
        ):
            reasons.append(f"{name}_insufficient_observations")
        drift = item["recent_drift_per_observation"]
        if max_drift is not None and (drift is None or abs(drift) > max_drift):
            reasons.append(f"{name}_not_mature")
    comparable = not reasons
    delta = float(after["score"]) - float(before["score"])
    return {
        "status": "COMPARABLE" if comparable else "INSUFFICIENT_EVIDENCE",
        "reasons": reasons,
        "cohort_distance": distance,
        "score_delta": delta if comparable else None,
        "after_is_better": (delta > 0 if direction == "max" else delta < 0) if comparable else None,
        "same_artifact": (
            before["artifact"]["sha256"] == after["artifact"]["sha256"]
            if before["artifact"] is not None and after["artifact"] is not None else None
        ),
        "observation_ids": [before["observation_id"], after["observation_id"]],
    }
