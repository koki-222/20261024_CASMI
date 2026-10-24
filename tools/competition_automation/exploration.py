"""Deterministic controls for Issue-authorized mechanism exploration."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ExplorationError(ValueError):
    """Raised when an exploration plan is not finite or Issue-bound."""


_COSMETIC_KINDS = frozenset({"cosmetic", "seed_only", "formatting"})
_MECHANISM_KINDS = frozenset(
    {
        "mechanism",
        "model",
        "feature",
        "data",
        "split",
        "training",
        "objective",
        "policy",
        "evaluation",
        "input",
    }
)
_CAPABILITIES = frozenset(
    {"ml", "dl", "rl", "simulation", "retrieval", "tabular", "vision", "audio", "nlp"}
)


def _as_dict(value: object, field: str) -> dict[str, Any]:
    """Require a JSON object."""
    if not isinstance(value, dict):
        raise ExplorationError(f"{field} must be an object")
    return value


def _keys(
    value: dict[str, Any], allowed: set[str], required: set[str], field: str
) -> None:
    """Reject unknown or missing schema keys."""
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise ExplorationError(f"{field} has unknown keys: {','.join(unknown)}")
    if missing:
        raise ExplorationError(f"{field} is missing keys: {','.join(missing)}")


def _positive_int(value: object, field: str, *, allow_zero: bool = False) -> int:
    """Parse a finite integer budget or attempt count."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or (value < 0 if allow_zero else value < 1)
    ):
        raise ExplorationError(f"{field} must be a valid integer")
    return value


def _finite(value: object, field: str) -> float:
    """Parse a finite numeric diagnostic value."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ExplorationError(f"{field} must be finite numeric")
    return float(value)


def _text(value: object, field: str) -> str:
    """Parse a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        raise ExplorationError(f"{field} must be a non-empty string")
    return value


def _sha256(path: Path) -> str:
    """Hash a file in bounded memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _issue_snapshot(plan: dict[str, Any], root: Path) -> dict[str, Any]:
    """Bind the plan to an Issue snapshot and check its JSON number."""
    issue = _as_dict(plan["source_issue"], "source_issue")
    _keys(issue, {"number", "snapshot"}, {"number", "snapshot"}, "source_issue")
    number = _positive_int(issue["number"], "source_issue.number")
    snapshot = _as_dict(issue["snapshot"], "source_issue.snapshot")
    _keys(snapshot, {"path", "sha256"}, {"path", "sha256"}, "source_issue.snapshot")
    raw_path = _text(snapshot["path"], "source_issue.snapshot.path")
    expected = snapshot["sha256"]
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(char not in "0123456789abcdef" for char in expected)
    ):
        raise ExplorationError("source_issue.snapshot.sha256 must be lowercase SHA-256")
    path = (
        (root / raw_path).resolve()
        if not Path(raw_path).is_absolute()
        else Path(raw_path).resolve()
    )
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ExplorationError("source_issue.snapshot.path must be below root") from exc
    if not path.is_file() or _sha256(path) != expected:
        raise ExplorationError("source Issue snapshot hash mismatch")
    try:
        snapshot_data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExplorationError(f"source Issue snapshot must be JSON: {exc}") from exc
    snapshot_data = _as_dict(snapshot_data, "source Issue snapshot")
    if snapshot_data.get("number") != number:
        raise ExplorationError("source Issue snapshot number mismatch")
    if (
        "authorized_axes" in snapshot_data
        and snapshot_data["authorized_axes"] != plan["authorized_axes"]
    ):
        raise ExplorationError("authorized_axes do not match Issue snapshot")
    return {
        "number": number,
        "path": path.relative_to(root.resolve()).as_posix(),
        "sha256": expected,
        "axes_bound": "authorized_axes" in snapshot_data,
    }


def _objective(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate the task metric and mechanism-to-metric declaration."""
    objective = _as_dict(plan["objective"], "objective")
    _keys(
        objective,
        {"metric", "direction", "mechanism"},
        {"metric", "direction", "mechanism"},
        "objective",
    )
    mechanism = objective["mechanism"]
    if (
        not isinstance(mechanism, list)
        or not mechanism
        or any(not isinstance(value, str) or not value.strip() for value in mechanism)
    ):
        raise ExplorationError(
            "objective.mechanism must name the mechanism-to-metric link"
        )
    if objective["direction"] not in {"min", "max"}:
        raise ExplorationError("objective.direction must be min or max")
    return {
        "metric": _text(objective["metric"], "objective.metric"),
        "direction": objective["direction"],
        "mechanism": list(mechanism),
    }


def bottleneck_analysis(
    candidate_recall: object, conditional_rank_quality: object
) -> dict[str, Any]:
    """Return separate coverage and conditional quality without ranking them."""
    recall = _finite(candidate_recall, "candidate_recall")
    quality = _finite(conditional_rank_quality, "conditional_rank_quality")
    if not 0 <= recall <= 1 or not 0 <= quality <= 1:
        raise ExplorationError("bottleneck values must be in [0, 1]")
    return {
        "candidate_recall": recall,
        "conditional_rank_quality": quality,
        "dominant": None,
        "overall_metric": None,
        "overall_metric_inference": "not_performed",
    }


def _axes(
    plan: dict[str, Any], objective: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Validate authorized mechanism axes and classify their coverage."""
    raw = plan["authorized_axes"]
    if not isinstance(raw, list) or not raw:
        raise ExplorationError("authorized_axes must be a non-empty list")
    seen: set[str] = set()
    axes: list[dict[str, Any]] = []
    uncovered: list[str] = []
    exhausted: list[str] = []
    for index, item in enumerate(raw):
        axis = _as_dict(item, f"authorized_axes[{index}]")
        _keys(
            axis,
            {"id", "kind", "budget", "families", "status", "objective_link"},
            {"id", "kind", "budget", "families", "status"},
            f"authorized_axes[{index}]",
        )
        axis_id = _text(axis["id"], f"authorized_axes[{index}].id")
        if axis_id in seen:
            raise ExplorationError(f"duplicate authorized axis: {axis_id}")
        seen.add(axis_id)
        kind = axis["kind"]
        if kind not in _MECHANISM_KINDS | _COSMETIC_KINDS:
            raise ExplorationError(f"authorized_axes[{index}].kind is invalid")
        _positive_int(axis["budget"], f"authorized_axes[{index}].budget")
        families = axis["families"]
        if (
            not isinstance(families, list)
            or len(set(families)) != len(families)
            or any(
                not isinstance(value, str) or not value.strip() for value in families
            )
        ):
            raise ExplorationError(
                f"authorized_axes[{index}].families must be unique names"
            )
        if (
            kind in _MECHANISM_KINDS
            and not axis.get("objective_link")
            and axis_id not in objective["mechanism"]
        ):
            raise ExplorationError(f"mechanism axis lacks objective_link: {axis_id}")
        copied = dict(axis)
        copied["classification"] = (
            "non_mechanism" if kind in _COSMETIC_KINDS else "mechanism"
        )
        axes.append(copied)
        if axis["status"] in {"exhausted", "closed"}:
            exhausted.append(axis_id)
        elif axis["status"] not in {"untested", "tested", "covered", "complete"}:
            raise ExplorationError(f"authorized_axes[{index}].status is invalid")
        if axis["status"] in {"untested"}:
            uncovered.append(axis_id)
    return axes, uncovered, exhausted


def _families(plan: dict[str, Any], axes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate family budgets, axis membership, and observed attempt counts."""
    raw = plan["families"]
    if not isinstance(raw, list):
        raise ExplorationError("families must be a list")
    axis_map = {axis["id"]: axis for axis in axes}
    seen: set[str] = set()
    families: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        family = _as_dict(item, f"families[{index}]")
        _keys(
            family,
            {"id", "axis", "budget", "observed_attempts"},
            {"id", "axis", "budget", "observed_attempts"},
            f"families[{index}]",
        )
        family_id = _text(family["id"], f"families[{index}].id")
        if family_id in seen:
            raise ExplorationError(f"duplicate family: {family_id}")
        seen.add(family_id)
        axis = _text(family["axis"], f"families[{index}].axis")
        if axis not in axis_map:
            raise ExplorationError(f"family references undeclared axis: {axis}")
        budget = _positive_int(family["budget"], f"families[{index}].budget")
        if budget > axis_map[axis]["budget"]:
            raise ExplorationError(f"family budget exceeds axis budget: {family_id}")
        attempts = _positive_int(
            family["observed_attempts"],
            f"families[{index}].observed_attempts",
            allow_zero=True,
        )
        if attempts > budget:
            raise ExplorationError(
                f"observed attempts exceed family budget: {family_id}"
            )
        families.append({**family, "id": family_id, "axis": axis, "budget": budget})
    listed = {axis: set(item["families"]) for axis, item in axis_map.items()}
    actual = {axis: set() for axis in axis_map}
    for family in families:
        actual[family["axis"]].add(family["id"])
    for axis, names in listed.items():
        if names - actual[axis]:
            raise ExplorationError(
                f"authorized axis family is not budgeted: {','.join(sorted(names - actual[axis]))}"
            )
    return families


def _restrictions(plan: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Validate restrictions and expose expired entries for human review."""
    if now.tzinfo is None:
        raise ExplorationError("now must be timezone-aware")
    restrictions = plan["restrictions"]
    if not isinstance(restrictions, list):
        raise ExplorationError("restrictions must be a list")
    expired: list[str] = []
    active: list[dict[str, Any]] = []
    for index, item in enumerate(restrictions):
        restriction = _as_dict(item, f"restrictions[{index}]")
        _keys(
            restriction,
            {"owner", "reason", "review_at"},
            {"owner", "reason", "review_at"},
            f"restrictions[{index}]",
        )
        raw_review = _text(restriction["review_at"], f"restrictions[{index}].review_at")
        try:
            review_at = datetime.fromisoformat(raw_review.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ExplorationError(
                f"restrictions[{index}].review_at must be ISO-8601"
            ) from exc
        if review_at.tzinfo is None:
            raise ExplorationError(
                f"restrictions[{index}].review_at must include timezone"
            )
        review_at = review_at.astimezone(timezone.utc)
        if review_at <= now.astimezone(timezone.utc):
            expired.append(str(restriction["reason"]))
        active.append({**restriction, "review_at": review_at.isoformat()})
    return {
        "active": active,
        "expired": expired,
        "review_needed": bool(expired),
        "automatic_extension": False,
    }


def _rl(plan: dict[str, Any]) -> dict[str, Any]:
    """Assess RL eligibility without making RL mandatory."""
    rl = plan["rl"]
    if rl is None:
        return {"eligible": False, "reasons": ["rl_not_declared"]}
    rl = _as_dict(rl, "rl")
    _keys(
        rl,
        {"state", "action", "reward", "evaluation", "budget", "cheap_baseline"},
        set(),
        "rl",
    )
    missing = [
        field
        for field in ("state", "action", "reward", "evaluation", "cheap_baseline")
        if rl.get(field) in (None, "", [], {})
    ]
    if (
        missing
        or not isinstance(rl["budget"], (int, float))
        or isinstance(rl["budget"], bool)
        or not math.isfinite(float(rl["budget"]))
        or float(rl["budget"]) <= 0
    ):
        return {
            "eligible": False,
            "reasons": [f"missing_{field}" for field in missing]
            or ["budget_must_be_finite_positive"],
        }
    return {"eligible": True, "reasons": [], **rl}


def evaluate_exploration(
    plan: dict[str, Any], root: Path, now: datetime | None = None
) -> dict[str, Any]:
    """Validate a strict finite Issue-bound exploration plan."""
    plan = _as_dict(plan, "plan")
    _keys(
        plan,
        {
            "schema_version",
            "source_issue",
            "capabilities",
            "objective",
            "authorized_axes",
            "families",
            "restrictions",
            "rl",
            "bottleneck",
        },
        {
            "schema_version",
            "source_issue",
            "capabilities",
            "objective",
            "authorized_axes",
            "families",
            "restrictions",
            "rl",
        },
        "plan",
    )
    if isinstance(plan["schema_version"], bool) or plan["schema_version"] != 1:
        raise ExplorationError("schema_version must be 1")
    capabilities = plan["capabilities"]
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or any(value not in _CAPABILITIES for value in capabilities)
    ):
        raise ExplorationError(
            "capabilities must be a non-empty list of known capabilities"
        )
    if len(set(capabilities)) != len(capabilities):
        raise ExplorationError("capabilities must be unique")
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ExplorationError(f"root does not exist: {root}")
    issue = _issue_snapshot(plan, root)
    objective = _objective(plan)
    axes, uncovered, exhausted = _axes(plan, objective)
    families = _families(plan, axes)
    restrictions = _restrictions(plan, now or datetime.now(timezone.utc))
    result: dict[str, Any] = {
        "status": "review_needed" if restrictions["review_needed"] else "ready",
        "source_issue": issue,
        "objective": objective,
        "capabilities": capabilities,
        "axes": axes,
        "families": families,
        "uncovered_axes": uncovered,
        "exhausted_axes": exhausted,
        "restrictions": restrictions,
        "rl": _rl(plan),
        "automatic_hypotheses": [],
        "automatic_model_delegates": [],
    }
    if "bottleneck" in plan:
        bottleneck = _as_dict(plan["bottleneck"], "bottleneck")
        _keys(
            bottleneck,
            {"candidate_recall", "conditional_rank_quality"},
            {"candidate_recall", "conditional_rank_quality"},
            "bottleneck",
        )
        result["bottleneck"] = bottleneck_analysis(
            bottleneck["candidate_recall"], bottleneck["conditional_rank_quality"]
        )
    else:
        result["bottleneck"] = None
    return result
