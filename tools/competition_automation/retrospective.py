"""Evaluate final Public/Private results against the frozen selection plan."""

from __future__ import annotations

from datetime import datetime
import json
import math
import re
from pathlib import Path
from typing import Any

from .fingerprint import fingerprint_path
from .selection import SelectionError, load_selection_plan, validate_selection_plan


class RetrospectiveError(ValueError):
    """Raised when final competition evidence is incomplete or inconsistent."""


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetrospectiveError(f"{field} must be a non-empty string")
    return value.strip()


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise RetrospectiveError(f"{field} must be a lowercase SHA-256")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RetrospectiveError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise RetrospectiveError(f"{field} must be finite")
    return result


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetrospectiveError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise RetrospectiveError(f"{field} must include a timezone")
    return parsed


def _reject_unknown(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise RetrospectiveError(f"unknown {field} field(s): {unknown}")


def _snapshot(root: Path, value: object, field: str) -> Path:
    if not isinstance(value, dict):
        raise RetrospectiveError(f"{field} must be an object")
    _reject_unknown(value, {"path", "sha256"}, field)
    relative = _text(value.get("path"), f"{field}.path")
    digest = _sha(value.get("sha256"), f"{field}.sha256")
    root_resolved = root.expanduser().resolve()
    raw = root_resolved / relative
    if raw.is_symlink():
        raise RetrospectiveError(f"{field} must not be a symlink")
    path = raw.resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise RetrospectiveError(f"{field} escapes root") from exc
    if not path.is_file():
        raise RetrospectiveError(f"{field} is missing: {relative}")
    if fingerprint_path(path)["sha256"] != digest:
        raise RetrospectiveError(f"{field} hash mismatch")
    return path


def _ordered(values: dict[str, float], direction: str) -> list[str]:
    """Return record IDs ordered from best to worst."""
    sign = 1.0 if direction == "min" else -1.0
    return sorted(values, key=lambda record_id: (sign * values[record_id], record_id))


def _regret(direction: str, best: float, value: float) -> float:
    """Return non-negative objective regret relative to the best result."""
    return value - best if direction == "min" else best - value


def _average_ranks(values: dict[str, float], direction: str) -> dict[str, float]:
    """Return one-based average ranks, preserving ties."""
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=direction == "max")
    result: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = ((index + 1) + end) / 2.0
        for record_id, _ in ordered[index:end]:
            result[record_id] = rank
        index = end
    return result


def _pearson(left: dict[str, float], right: dict[str, float]) -> float | None:
    """Return Pearson correlation on identical keys, or ``None`` if undefined."""
    keys = sorted(set(left).intersection(right))
    if len(keys) < 2:
        return None
    left_mean = sum(left[key] for key in keys) / len(keys)
    right_mean = sum(right[key] for key in keys) / len(keys)
    numerator = sum(
        (left[key] - left_mean) * (right[key] - right_mean) for key in keys
    )
    left_scale = math.sqrt(sum((left[key] - left_mean) ** 2 for key in keys))
    right_scale = math.sqrt(sum((right[key] - right_mean) ** 2 for key in keys))
    if left_scale == 0.0 or right_scale == 0.0:
        return None
    return numerator / (left_scale * right_scale)


def _rank_correlation(
    left: dict[str, float], right: dict[str, float], direction: str
) -> float | None:
    """Return tie-aware Spearman rank correlation."""
    keys = set(left).intersection(right)
    return _pearson(
        _average_ranks({key: left[key] for key in keys}, direction),
        _average_ranks({key: right[key] for key in keys}, direction),
    )


def _pairwise_concordance(
    left: dict[str, float], right: dict[str, float], direction: str
) -> float | None:
    """Return the fraction of non-tied candidate pairs ordered consistently."""
    keys = sorted(set(left).intersection(right))
    concordant = 0
    comparable = 0
    sign = 1.0 if direction == "min" else -1.0
    for index, first in enumerate(keys):
        for second in keys[index + 1 :]:
            left_delta = sign * (left[first] - left[second])
            right_delta = sign * (right[first] - right[second])
            if left_delta == 0.0 or right_delta == 0.0:
                continue
            comparable += 1
            concordant += int(left_delta * right_delta > 0.0)
    return concordant / comparable if comparable else None


def _load_official_snapshot(path: Path, root: Path) -> dict[str, Any]:
    """Load a normalized final leaderboard snapshot."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RetrospectiveError("official_snapshot is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RetrospectiveError("official_snapshot content must be an object")
    _reject_unknown(
        value,
        {"schema_version", "scope", "evidence_kind", "source", "submissions"},
        "official_snapshot content",
    )
    if value.get("schema_version") != 1:
        raise RetrospectiveError("official_snapshot content schema_version must be 1")
    if value.get("scope") not in {"all_user_submissions", "frozen_portfolio"}:
        raise RetrospectiveError("official_snapshot.scope is invalid")
    if value.get("evidence_kind") not in {"official", "user_reported_official"}:
        raise RetrospectiveError("official_snapshot.evidence_kind is invalid")
    if value.get("evidence_kind") == "official":
        _snapshot(root, value.get("source"), "official_snapshot.source")
    elif value.get("source") is not None:
        raise RetrospectiveError(
            "user_reported_official snapshot must not claim an official source"
        )
    if not isinstance(value.get("submissions"), list) or not value["submissions"]:
        raise RetrospectiveError("official_snapshot.submissions must be non-empty")
    return value


def _verify_official_scores(
    snapshot: dict[str, Any], metadata: dict[str, dict[str, Any]]
) -> None:
    """Match manifest scores to normalized official rows by submission ID."""
    official: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(snapshot["submissions"]):
        if not isinstance(row, dict):
            raise RetrospectiveError(f"official_snapshot.submissions[{index}] must be an object")
        _reject_unknown(
            row,
            {"submission_id", "public_score", "private_score"},
            f"official_snapshot.submissions[{index}]",
        )
        submission_id = _text(
            row.get("submission_id"),
            f"official_snapshot.submissions[{index}].submission_id",
        )
        if submission_id in official:
            raise RetrospectiveError("official_snapshot submission IDs must be unique")
        _number(row.get("public_score"), f"official_snapshot.submissions[{index}].public_score")
        _number(row.get("private_score"), f"official_snapshot.submissions[{index}].private_score")
        official[submission_id] = row
    manifest_by_id = {value["submission_id"]: value for value in metadata.values()}
    if set(official) != set(manifest_by_id):
        raise RetrospectiveError("official snapshot and manifest submission IDs differ")
    for submission_id, row in official.items():
        declared = manifest_by_id[submission_id]
        if not math.isclose(
            float(row["public_score"]),
            float(declared["public_score"]),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise RetrospectiveError(f"public score mismatch for {submission_id}")
        if not math.isclose(
            float(row["private_score"]),
            float(declared["private_score"]),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise RetrospectiveError(f"private score mismatch for {submission_id}")


def evaluate_retrospective(manifest: dict[str, Any], root: Path) -> dict[str, Any]:
    """Reconcile final leaderboard evidence with the frozen selection portfolio."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in {1, 2}:
        raise RetrospectiveError("retrospective schema_version must be 1 or 2")
    schema_version = int(manifest["schema_version"])
    _reject_unknown(
        manifest,
        {
            "schema_version",
            "competition_ended_at",
            "metric",
            "official_snapshot",
            "selection_plan",
            "submissions",
        },
        "retrospective",
    )
    ended_at = _timestamp(manifest.get("competition_ended_at"), "competition_ended_at")
    metric = manifest.get("metric")
    if not isinstance(metric, dict):
        raise RetrospectiveError("metric must be an object")
    _reject_unknown(metric, {"name", "direction", "target"}, "metric")
    metric_name = _text(metric.get("name"), "metric.name")
    direction = metric.get("direction")
    if direction not in {"min", "max"}:
        raise RetrospectiveError("metric.direction must be 'min' or 'max'")
    target = _number(metric.get("target"), "metric.target")
    official_path = _snapshot(root, manifest.get("official_snapshot"), "official_snapshot")
    selection_path = _snapshot(root, manifest.get("selection_plan"), "selection_plan")
    try:
        selection = load_selection_plan(selection_path, root=root)
        selection_result = validate_selection_plan(selection, root=root)
    except SelectionError as exc:
        raise RetrospectiveError(f"selection plan failed: {exc}") from exc
    if schema_version == 2 and selection.get("schema_version") != 2:
        raise RetrospectiveError("retrospective schema v2 requires selection plan schema v2")
    if selection_result["metric"] != metric_name or selection_result["direction"] != direction:
        raise RetrospectiveError("selection plan metric does not match retrospective metric")
    selection_frozen_at = _timestamp(
        selection.get("frozen_at"), "selection_plan.frozen_at"
    )
    if selection_frozen_at >= ended_at:
        raise RetrospectiveError("selection plan must be frozen before competition end")

    raw_submissions = manifest.get("submissions")
    if not isinstance(raw_submissions, list) or not raw_submissions:
        raise RetrospectiveError("submissions must be a non-empty list")
    public: dict[str, float] = {}
    private: dict[str, float] = {}
    metadata: dict[str, dict[str, Any]] = {}
    submission_ids: set[str] = set()
    artifacts: set[str] = set()
    for index, value in enumerate(raw_submissions):
        if not isinstance(value, dict):
            raise RetrospectiveError(f"submissions[{index}] must be an object")
        _reject_unknown(
            value,
            {
                "record_id",
                "submission_id",
                "submitted_at",
                "artifact_sha256",
                "family",
                "complexity_rank",
                "public_score",
                "private_score",
            },
            f"submissions[{index}]",
        )
        record_id = _text(value.get("record_id"), f"submissions[{index}].record_id")
        submission_id = _text(
            value.get("submission_id"), f"submissions[{index}].submission_id"
        )
        artifact = _sha(value.get("artifact_sha256"), f"submissions[{index}].artifact_sha256")
        if record_id in metadata or submission_id in submission_ids or artifact in artifacts:
            raise RetrospectiveError("record, submission, and artifact identities must be unique")
        submitted_raw = value.get("submitted_at")
        submitted_at = (
            None
            if schema_version == 2 and submitted_raw is None
            else _timestamp(submitted_raw, f"submissions[{index}].submitted_at")
        )
        if submitted_at is not None and submitted_at > ended_at:
            raise RetrospectiveError(f"submission {submission_id} occurred after competition end")
        complexity = value.get("complexity_rank")
        if not isinstance(complexity, int) or isinstance(complexity, bool) or complexity < 0:
            raise RetrospectiveError(f"submissions[{index}].complexity_rank must be >= 0")
        public[record_id] = _number(value.get("public_score"), f"submissions[{index}].public_score")
        private[record_id] = _number(
            value.get("private_score"), f"submissions[{index}].private_score"
        )
        metadata[record_id] = value
        submission_ids.add(submission_id)
        artifacts.add(artifact)
    if schema_version == 2:
        snapshot = _load_official_snapshot(official_path, root)
        _verify_official_scores(snapshot, metadata)
    else:
        snapshot = {"scope": "manifest_only"}
    selection_candidates = {
        value["record_id"]: value for value in selection.get("candidates", [])
    }
    if schema_version == 2:
        missing_plan_rows = sorted(set(metadata).difference(selection_candidates))
        if missing_plan_rows:
            raise RetrospectiveError(
                f"final results are missing from the frozen selection plan: {missing_plan_rows}"
            )
        for record_id, value in metadata.items():
            candidate = selection_candidates[record_id]
            for key in ("artifact_sha256", "family", "complexity_rank"):
                if value[key] != candidate[key]:
                    raise RetrospectiveError(
                        f"submission {record_id} {key} does not match selection plan"
                    )
    selected = set(selection_result["selected"])
    missing_selected = sorted(selected.difference(metadata))
    if missing_selected:
        raise RetrospectiveError(f"selected candidates lack final results: {missing_selected}")
    new_selected = selected.difference(selection_result["preexisting_submissions"])
    selection_freeze_timing_verified = all(
        metadata[record_id].get("submitted_at") is not None for record_id in new_selected
    )
    if new_selected and selection_freeze_timing_verified:
        first_selected_submission = min(
            _timestamp(
                metadata[record_id]["submitted_at"],
                f"submission {record_id}.submitted_at",
            )
            for record_id in new_selected
        )
        if selection_frozen_at > first_selected_submission:
            raise RetrospectiveError(
                "selection plan was frozen after a newly selected submission"
            )

    public_order = _ordered(public, direction)
    private_order = _ordered(private, direction)
    public_rank = _average_ranks(public, direction)
    private_rank = _average_ranks(private, direction)
    rank_changes = {
        record_id: private_rank[record_id] - public_rank[record_id]
        for record_id in metadata
    }
    best_private = private_order[0]
    best_private_value = private[best_private]
    champion = selection_result["champion_after"]
    margin = float(selection["policy"]["equivalence_margin"])
    equivalent = [
        record_id
        for record_id, value in private.items()
        if _regret(direction, best_private_value, value) <= margin
    ]
    simplest_equivalent = min(
        equivalent,
        key=lambda record_id: (int(metadata[record_id]["complexity_rank"]), record_id),
    )
    selected_private_best = (
        min(selected, key=lambda record_id: private[record_id])
        if direction == "min"
        else max(selected, key=lambda record_id: private[record_id])
    )
    eligible_ids = {
        record_id
        for record_id, candidate in selection_candidates.items()
        if record_id in private
        and isinstance(candidate.get("eligibility"), dict)
        and candidate["eligibility"].get("status") == "verified"
    }
    best_eligible = (
        min(eligible_ids, key=lambda record_id: private[record_id])
        if direction == "min" and eligible_ids
        else max(eligible_ids, key=lambda record_id: private[record_id])
        if eligible_ids
        else None
    )
    objective_value = (
        private[best_eligible]
        if schema_version == 2 and best_eligible is not None
        else best_private_value
    )
    target_achieved = (
        objective_value <= target if direction == "min" else objective_value >= target
    )
    scoreboard_target_achieved = (
        best_private_value <= target if direction == "min" else best_private_value >= target
    )
    selection_scores = {
        record_id: float(candidate["metric_values"][selection_result["selection_profile"]])
        for record_id, candidate in selection_candidates.items()
        if record_id in private
    }
    gaps = [private[record_id] - public[record_id] for record_id in metadata]
    return {
        "status": "PASS",
        "metric": metric_name,
        "direction": direction,
        "target": target,
        "target_achieved": target_achieved,
        "target_gap": _regret(direction, target, objective_value),
        "scoreboard_target_achieved": scoreboard_target_achieved,
        "scoreboard_target_gap": _regret(direction, target, best_private_value),
        "result_scope": snapshot["scope"],
        "result_evidence_kind": snapshot.get("evidence_kind", "legacy_unclassified"),
        "selection_freeze_timing_verified": selection_freeze_timing_verified,
        "best_public": {"record_id": public_order[0], "value": public[public_order[0]]},
        "best_private": {"record_id": best_private, "value": best_private_value},
        "public_private_winner_changed": public_order[0] != best_private,
        "public_private_rank_correlation": _rank_correlation(public, private, direction),
        "private_minus_public": {
            "mean": sum(gaps) / len(gaps),
            "minimum": min(gaps),
            "maximum": max(gaps),
        },
        "rank_changes_private_minus_public": rank_changes,
        "champion_after": champion,
        "champion_private_regret": _regret(
            direction, best_private_value, private[champion]
        ),
        "equivalent_private_candidates": sorted(equivalent),
        "simplest_equivalent_private_candidate": simplest_equivalent,
        "selected_private_best": selected_private_best,
        "selected_private_regret": _regret(
            direction, best_private_value, private[selected_private_best]
        ),
        "best_verified_eligible_private": (
            {"record_id": best_eligible, "value": private[best_eligible]}
            if best_eligible is not None
            else None
        ),
        "selection_private_rank_correlation": _rank_correlation(
            selection_scores, private, direction
        ),
        "selection_public_rank_correlation": _rank_correlation(
            selection_scores, public, direction
        ),
        "selection_private_pairwise_concordance": _pairwise_concordance(
            selection_scores, private, direction
        ),
        "selection_public_pairwise_concordance": _pairwise_concordance(
            selection_scores, public, direction
        ),
    }


def load_retrospective(path: Path) -> dict[str, Any]:
    """Load one retrospective manifest without evaluating filesystem evidence."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RetrospectiveError(f"retrospective is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RetrospectiveError(f"invalid retrospective: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RetrospectiveError("retrospective must be an object")
    return value
