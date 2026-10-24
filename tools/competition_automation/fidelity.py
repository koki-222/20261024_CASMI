"""Framework neutral multi-fidelity experiment accounting."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .fingerprint import canonical_json_digest


class FidelityError(ValueError):
    """Raised when a fidelity plan or its evidence is malformed."""


_UNITS = frozenset(
    {"steps", "epochs", "iterations", "samples", "tokens", "minutes", "hours"}
)
_STAGE_STATUSES = frozenset(
    {"planned", "running", "completed", "budget_exhausted", "paused", "failed"}
)
_RESUME_MODES = frozenset(
    {"new_run", "exact_resume", "weights_only", "feature_transfer"}
)
_REUSE_CLASSES = frozenset({"features", "frozen_head", "partial_ensemble"})
_REQUIRED_COMPONENTS = {
    "dl": ("model", "optimizer", "scheduler", "rng", "sampler", "step"),
    "deep_learning": ("model", "optimizer", "scheduler", "rng", "sampler", "step"),
    "ml": ("model", "native_state"),
    "classical_ml": ("model", "native_state"),
}


def _as_dict(value: object, field: str) -> dict[str, Any]:
    """Require a JSON object."""
    if not isinstance(value, dict):
        raise FidelityError(f"{field} must be an object")
    return value


def _keys(
    value: dict[str, Any], allowed: set[str], required: set[str], field: str
) -> None:
    """Reject unknown or missing keys in a schema object."""
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise FidelityError(f"{field} has unknown keys: {','.join(unknown)}")
    if missing:
        raise FidelityError(f"{field} is missing keys: {','.join(missing)}")


def _finite(value: object, field: str, *, nonnegative: bool = False) -> float:
    """Parse a finite numeric value."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise FidelityError(f"{field} must be finite numeric")
    number = float(value)
    if nonnegative and number < 0:
        raise FidelityError(f"{field} must be non-negative")
    return number


def _positive(value: object, field: str) -> float:
    """Parse a finite positive numeric value."""
    number = _finite(value, field)
    if number <= 0:
        raise FidelityError(f"{field} must be positive")
    return number


def _text(value: object, field: str) -> str:
    """Parse a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        raise FidelityError(f"{field} must be a non-empty string")
    return value


def _sha256(path: Path) -> str:
    """Hash a file in bounded memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _path(root: Path, value: object, field: str) -> Path:
    """Resolve a repository-relative path without allowing escape."""
    raw = _text(value, field)
    candidate = (
        (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    )
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise FidelityError(f"{field} must be below root") from exc
    return candidate


def _objective(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate the preregistered metric objective."""
    objective = _as_dict(plan["objective"], "objective")
    _keys(
        objective,
        {"metric", "direction", "min_delta"},
        {"metric", "direction", "min_delta"},
        "objective",
    )
    metric = _text(objective["metric"], "objective.metric")
    if objective["direction"] not in {"min", "max"}:
        raise FidelityError("objective.direction must be min or max")
    return {
        "metric": metric,
        "direction": objective["direction"],
        "min_delta": _finite(
            objective["min_delta"], "objective.min_delta", nonnegative=True
        ),
    }


def _stages(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], str, float]:
    """Validate unique, strictly increasing fidelity stages."""
    raw = plan["stages"]
    if not isinstance(raw, list) or not raw:
        raise FidelityError("stages must be a non-empty list")
    stages: list[dict[str, Any]] = []
    names: set[str] = set()
    previous = 0.0
    for index, item in enumerate(raw):
        stage = _as_dict(item, f"stages[{index}]")
        _keys(
            stage,
            {"name", "budget", "status", "cohort_id", "metric_snapshot"},
            {"name", "budget", "status", "cohort_id"},
            f"stages[{index}]",
        )
        name = _text(stage["name"], f"stages[{index}].name")
        if name in names:
            raise FidelityError(f"duplicate stage name: {name}")
        names.add(name)
        budget = _positive(stage["budget"], f"stages[{index}].budget")
        if budget <= previous:
            raise FidelityError("stage budgets must be strictly increasing")
        previous = budget
        if stage["status"] not in _STAGE_STATUSES:
            raise FidelityError(f"stages[{index}].status is invalid")
        _text(stage["cohort_id"], f"stages[{index}].cohort_id")
        copied = dict(stage)
        copied["budget"] = budget
        stages.append(copied)
    unit = plan["unit"]
    if unit not in _UNITS:
        raise FidelityError("unit is invalid")
    pending_seen = False
    for index, stage in enumerate(stages):
        if stage["status"] == "completed":
            if pending_seen:
                raise FidelityError(
                    f"stage {stage['name']} completes after an unfinished earlier stage"
                )
        elif stage["status"] in {
            "planned",
            "running",
            "budget_exhausted",
            "paused",
            "failed",
        }:
            pending_seen = True
    return stages, str(unit), previous


def _speed(plan: dict[str, Any], total: float, unit: str) -> dict[str, Any]:
    """Assess runtime feasibility from a measured speed and available time."""
    observed = _as_dict(plan["observed"], "observed")
    _keys(
        observed,
        {"speed", "speed_unit", "completed", "available_seconds"},
        {"speed", "speed_unit", "completed"},
        "observed",
    )
    completed = _finite(observed["completed"], "observed.completed", nonnegative=True)
    if completed > total:
        raise FidelityError("observed.completed exceeds total budget")
    next_stage = next(
        (
            float(stage["budget"])
            for stage in plan["stages"]
            if float(stage["budget"]) > completed
        ),
        None,
    )
    if observed["speed"] is None:
        return {
            "status": "unknown",
            "full_budget_status": "unknown",
            "next_stage_budget": next_stage,
            "next_stage_feasible": None,
            "allowed_budget": completed,
            "recommended_action": "measure_speed",
            "required_seconds": None,
            "available_seconds": None,
            "completed": completed,
        }
    speed = _positive(observed["speed"], "observed.speed")
    if observed["speed_unit"] != f"{unit}_per_second":
        raise FidelityError(f"observed.speed_unit must be {unit}_per_second")
    available = observed.get("available_seconds")
    if available is None:
        return {
            "status": "unknown",
            "full_budget_status": "unknown",
            "next_stage_budget": next_stage,
            "next_stage_feasible": None,
            "allowed_budget": completed,
            "recommended_action": "run_next_rung"
            if next_stage is not None
            else "complete",
            "required_seconds": (total - completed) / speed,
            "available_seconds": None,
            "completed": completed,
        }
    available_seconds = _finite(
        available, "observed.available_seconds", nonnegative=True
    )
    required_seconds = (total - completed) / speed
    capacity = min(total, completed + math.floor(available_seconds * speed))
    stage_budgets = [float(stage["budget"]) for stage in plan["stages"]]
    next_stage = next((budget for budget in stage_budgets if budget > completed), None)
    next_feasible = next_stage is not None and next_stage <= capacity
    full_status = "feasible" if required_seconds <= available_seconds else "infeasible"
    return {
        "status": full_status,
        "full_budget_status": full_status,
        "next_stage_budget": next_stage,
        "next_stage_feasible": next_feasible,
        "allowed_budget": capacity,
        "recommended_action": "run_next_rung" if next_feasible else "blocked_runtime",
        "required_seconds": required_seconds,
        "available_seconds": available_seconds,
        "completed": completed,
    }


def _lineage(plan: dict[str, Any], objective: dict[str, Any]) -> dict[str, Any]:
    """Validate immutable data/code/split/config lineage for a checkpoint."""
    lineage = _as_dict(plan["lineage"], "lineage")
    _keys(
        lineage,
        {"code_sha256", "data_sha256", "split_sha256", "config_sha256", "objective"},
        {"code_sha256", "data_sha256", "split_sha256", "config_sha256", "objective"},
        "lineage",
    )
    for key in ("code_sha256", "data_sha256", "split_sha256", "config_sha256"):
        value = lineage[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise FidelityError(f"lineage.{key} must be lowercase SHA-256")
    lineage_objective = _as_dict(lineage["objective"], "lineage.objective")
    if lineage_objective != objective:
        raise FidelityError("lineage.objective must equal objective")
    payload = _as_dict(plan["computation_payload"], "computation_payload")
    _keys(
        payload,
        {"code", "data", "split", "config"},
        {"code", "data", "split", "config"},
        "computation_payload",
    )
    for key in ("code", "data", "split", "config"):
        if lineage[f"{key}_sha256"] != canonical_json_digest(payload[key]):
            raise FidelityError(
                f"lineage.{key}_sha256 does not match computation_payload.{key}"
            )
    return lineage


def _manifest(
    plan: dict[str, Any],
    root: Path,
    required: list[str],
    mode: str,
    objective: dict[str, Any],
) -> dict[str, Any]:
    """Verify manifest hashes, required components, and immutable lineage."""
    checkpoint_value = plan["checkpoint"]
    if mode == "new_run" and (
        checkpoint_value is None or checkpoint_value == {"resume_mode": "new_run"}
    ):
        return {
            "status": "not_requested",
            "resume_mode": mode,
            "required_components": required,
            "verified_components": [],
            "errors": [],
        }
    checkpoint = _as_dict(checkpoint_value, "checkpoint")
    _keys(
        checkpoint,
        {"resume_mode", "manifest_path", "manifest_sha256", "required_components"},
        {"resume_mode", "manifest_path", "manifest_sha256", "required_components"},
        "checkpoint",
    )
    manifest_path = _path(root, checkpoint["manifest_path"], "checkpoint.manifest_path")
    expected_manifest_hash = checkpoint["manifest_sha256"]
    if not isinstance(expected_manifest_hash, str) or len(expected_manifest_hash) != 64:
        raise FidelityError("checkpoint.manifest_sha256 must be SHA-256")
    errors: list[str] = []
    if not manifest_path.is_file():
        return {
            "status": "invalid",
            "resume_mode": mode,
            "required_components": required,
            "verified_components": [],
            "errors": ["missing checkpoint manifest"],
        }
    if _sha256(manifest_path) != expected_manifest_hash:
        errors.append("checkpoint manifest hash mismatch")
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"checkpoint manifest unreadable: {exc}")
        loaded = {}
    manifest = _as_dict(loaded, "checkpoint manifest")
    _keys(
        manifest,
        {
            "schema_version",
            "capability",
            "resume_mode",
            "required_components",
            "components",
            "lineage",
        },
        {
            "schema_version",
            "capability",
            "resume_mode",
            "required_components",
            "components",
            "lineage",
        },
        "checkpoint manifest",
    )
    if (
        manifest["schema_version"] != 1
        or manifest["resume_mode"] != mode
        or manifest["capability"] != plan["capability"]
    ):
        errors.append("checkpoint manifest schema or resume mode mismatch")
    if manifest["required_components"] != required:
        errors.append("checkpoint required components mismatch")
    if manifest["lineage"] != _lineage(plan, objective):
        errors.append("checkpoint lineage mismatch")
    entries = manifest["components"]
    if not isinstance(entries, list):
        raise FidelityError("checkpoint manifest components must be a list")
    found: set[str] = set()
    verified: list[str] = []
    for index, entry in enumerate(entries):
        item = _as_dict(entry, f"checkpoint manifest.components[{index}]")
        _keys(
            item,
            {"component", "path", "sha256"},
            {"component", "path", "sha256"},
            f"checkpoint manifest.components[{index}]",
        )
        component = _text(
            item["component"], f"checkpoint manifest.components[{index}].component"
        )
        if component in found:
            errors.append(f"duplicate checkpoint component: {component}")
        found.add(component)
        component_path = _path(
            root, item["path"], f"checkpoint manifest.components[{index}].path"
        )
        if not component_path.is_file():
            errors.append(f"missing checkpoint component: {component}")
        elif _sha256(component_path) != item["sha256"]:
            errors.append(f"checkpoint component hash mismatch: {component}")
        else:
            verified.append(component)
    errors.extend(
        f"missing declared checkpoint component: {name}"
        for name in sorted(set(required) - found)
    )
    if errors:
        status = "invalid"
    elif mode == "exact_resume":
        status = "verified_exact_resume"
    elif mode in {"weights_only", "feature_transfer"}:
        status = "verified_weights_only"
    else:
        status = "not_requested"
    return {
        "status": status,
        "resume_mode": mode,
        "required_components": required,
        "verified_components": sorted(set(verified)),
        "errors": errors,
    }


def _metric_snapshot(
    stage: dict[str, Any], index: int, objective: dict[str, Any], root: Path
) -> dict[str, Any]:
    """Validate a completed stage's hash-bound metric evidence."""
    snapshot = _as_dict(
        stage.get("metric_snapshot"), f"stages[{index}].metric_snapshot"
    )
    _keys(
        snapshot,
        {
            "metric",
            "value",
            "cohort_id",
            "status",
            "stage_name",
            "budget",
            "path",
            "sha256",
        },
        {
            "metric",
            "value",
            "cohort_id",
            "status",
            "stage_name",
            "budget",
            "path",
            "sha256",
        },
        f"stages[{index}].metric_snapshot",
    )
    if (
        snapshot["metric"] != objective["metric"]
        or snapshot["cohort_id"] != stage["cohort_id"]
        or snapshot["status"] != "completed"
        or snapshot["stage_name"] != stage["name"]
        or float(snapshot["budget"]) != float(stage["budget"])
    ):
        raise FidelityError(
            f"stages[{index}].metric_snapshot does not match stage/objective"
        )
    _finite(snapshot["value"], f"stages[{index}].metric_snapshot.value")
    _finite(snapshot["budget"], f"stages[{index}].metric_snapshot.budget")
    evidence = _path(root, snapshot["path"], f"stages[{index}].metric_snapshot.path")
    if not evidence.is_file() or _sha256(evidence) != snapshot["sha256"]:
        raise FidelityError(f"stages[{index}].metric_snapshot evidence hash mismatch")
    try:
        evidence_data = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FidelityError(
            f"stages[{index}].metric_snapshot evidence must be JSON: {exc}"
        ) from exc
    evidence_data = _as_dict(evidence_data, f"stages[{index}].metric_snapshot evidence")
    _keys(
        evidence_data,
        {"metric", "value", "cohort_id", "status", "stage_name", "budget"},
        {"metric", "value", "cohort_id", "status", "stage_name", "budget"},
        f"stages[{index}].metric_snapshot evidence",
    )
    for key in ("metric", "cohort_id", "status", "stage_name"):
        if evidence_data[key] != snapshot[key]:
            raise FidelityError(
                f"stages[{index}].metric_snapshot evidence {key} mismatch"
            )
    if (
        not isinstance(evidence_data["value"], (int, float))
        or isinstance(evidence_data["value"], bool)
        or float(evidence_data["value"]) != float(snapshot["value"])
    ):
        raise FidelityError(f"stages[{index}].metric_snapshot evidence value mismatch")
    if (
        not isinstance(evidence_data["budget"], (int, float))
        or isinstance(evidence_data["budget"], bool)
        or float(evidence_data["budget"]) != float(snapshot["budget"])
    ):
        raise FidelityError(f"stages[{index}].metric_snapshot evidence budget mismatch")
    return snapshot


def _promotion(
    plan: dict[str, Any],
    stages: list[dict[str, Any]],
    objective: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    """Derive promotion from completed evidence in one preregistered cohort."""
    cohort = _text(plan["cohort_id"], "cohort_id")
    if any(stage["cohort_id"] != cohort for stage in stages):
        raise FidelityError("all stages must use plan.cohort_id")
    if any(stage["status"] == "budget_exhausted" for stage in stages):
        return {
            "decision": "inconclusive",
            "reason": "budget_exhausted",
            "cohort_id": cohort,
        }
    if any(stage["status"] == "paused" for stage in stages):
        return {"decision": "paused", "reason": "stage_paused", "cohort_id": cohort}
    completed: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        if stage["status"] == "completed":
            completed.append(_metric_snapshot(stage, index, objective, root))
        elif "metric_snapshot" in stage:
            raise FidelityError(
                f"stages[{index}] has metric evidence but is not completed"
            )
    if len(completed) < 2:
        return {
            "decision": "awaiting_metric",
            "reason": "two_completed_metric_snapshots_required",
            "cohort_id": cohort,
        }
    baseline = float(completed[0]["value"])
    candidate = float(completed[-1]["value"])
    delta = (
        candidate - baseline
        if objective["direction"] == "max"
        else baseline - candidate
    )
    decision = "promote" if delta >= objective["min_delta"] else "prune"
    return {
        "decision": decision,
        "rung_decision": decision,
        "scientific_decision": "not_evaluated",
        "basis": "within_run_learning_curve",
        "metric": objective["metric"],
        "direction": objective["direction"],
        "initial_stage_metric": baseline,
        "final_stage_metric": candidate,
        "delta": delta,
        "min_delta": objective["min_delta"],
        "cohort_id": cohort,
    }


def _reuse(plan: dict[str, Any], root: Path) -> dict[str, Any]:
    """Retain only hash-verified partial artifacts on approved Issue axes."""
    approved = plan["approved_issue_axes"]
    if not isinstance(approved, list) or any(
        not isinstance(value, str) for value in approved
    ):
        raise FidelityError("approved_issue_axes must be a list")
    records = plan["partial_artifacts"]
    if not isinstance(records, list):
        raise FidelityError("partial_artifacts must be a list")
    retained: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, record in enumerate(records):
        item = _as_dict(record, f"partial_artifacts[{index}]")
        _keys(
            item,
            {"reuse_class", "issue_axis", "path", "sha256"},
            {"reuse_class", "issue_axis", "path", "sha256"},
            f"partial_artifacts[{index}]",
        )
        if item["reuse_class"] not in _REUSE_CLASSES:
            raise FidelityError(f"partial_artifacts[{index}].reuse_class is invalid")
        axis = _text(item["issue_axis"], f"partial_artifacts[{index}].issue_axis")
        artifact = _path(root, item["path"], f"partial_artifacts[{index}].path")
        if axis not in approved:
            errors.append(f"reuse axis is not approved by Issue: {axis}")
        elif not artifact.is_file() or _sha256(artifact) != item["sha256"]:
            errors.append(f"partial artifact hash mismatch: {item['path']}")
        else:
            retained.append(
                {
                    "reuse_class": item["reuse_class"],
                    "issue_axis": axis,
                    "path": artifact.relative_to(root).as_posix(),
                    "sha256": item["sha256"],
                }
            )
    return {"retained": retained, "errors": errors, "automatic_expansion": []}


def evaluate_fidelity(plan: dict[str, Any], root: Path) -> dict[str, Any]:
    """Validate a strict multi-fidelity plan and return an evidence decision."""
    plan = _as_dict(plan, "plan")
    _keys(
        plan,
        {
            "schema_version",
            "capability",
            "cohort_id",
            "unit",
            "total_budget",
            "stages",
            "observed",
            "computation_payload",
            "lineage",
            "objective",
            "checkpoint",
            "approved_issue_axes",
            "partial_artifacts",
        },
        {
            "schema_version",
            "capability",
            "cohort_id",
            "unit",
            "total_budget",
            "stages",
            "observed",
            "computation_payload",
            "lineage",
            "objective",
            "checkpoint",
            "approved_issue_axes",
            "partial_artifacts",
        },
        "plan",
    )
    if plan["schema_version"] != 1 or isinstance(plan["schema_version"], bool):
        raise FidelityError("schema_version must be 1")
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FidelityError(f"root does not exist: {root}")
    stages, unit, last_budget = _stages(plan)
    objective = _objective(plan)
    total = _positive(plan["total_budget"], "total_budget")
    if total < last_budget:
        raise FidelityError("total_budget must cover the final fidelity stage")
    lineage = _lineage(plan, objective)
    checkpoint_value = plan["checkpoint"]
    mode = (
        "new_run"
        if checkpoint_value is None
        else _as_dict(checkpoint_value, "checkpoint")["resume_mode"]
    )
    if mode not in _RESUME_MODES:
        raise FidelityError("checkpoint.resume_mode is invalid")
    checkpoint_dict = (
        None if checkpoint_value is None else _as_dict(checkpoint_value, "checkpoint")
    )
    required = (
        []
        if checkpoint_dict is None
        else checkpoint_dict.get("required_components", [])
    )
    if (
        not isinstance(required, list)
        or len(set(required)) != len(required)
        or any(not isinstance(value, str) or not value for value in required)
    ):
        raise FidelityError(
            "checkpoint.required_components must be unique component names"
        )
    capability_required = set(
        _REQUIRED_COMPONENTS.get(str(plan["capability"]).lower(), ())
    )
    if mode == "exact_resume" and (
        not required or not capability_required.issubset(required)
    ):
        raise FidelityError(
            "checkpoint.required_components is incomplete for capability"
        )
    if (
        mode == "new_run"
        and _finite(
            plan["observed"]["completed"], "observed.completed", nonnegative=True
        )
        != 0
    ):
        raise FidelityError("new_run cannot have completed units")
    feasibility = _speed(plan, total, unit)
    checkpoint_result = _manifest(plan, root, required, mode, objective)
    reuse = _reuse(plan, root)
    promotion = _promotion(plan, stages, objective, root)
    if feasibility["status"] == "infeasible" and not feasibility.get(
        "next_stage_feasible", False
    ):
        status = "blocked"
    elif checkpoint_result["errors"] or reuse["errors"]:
        status = "blocked"
    elif promotion["decision"] in {"inconclusive", "paused", "promote", "prune"}:
        status = promotion["decision"]
    else:
        status = "ready"
    return {
        "status": status,
        "unit": unit,
        "total_budget": total,
        "lineage": lineage,
        "stages": stages,
        "feasibility": feasibility,
        "checkpoint": checkpoint_result,
        "promotion": promotion,
        "reuse": reuse,
        "hypothesis_result": "inconclusive"
        if promotion["decision"] == "inconclusive"
        else "not_evaluated"
        if promotion["decision"] in {"promote", "prune"}
        else promotion["decision"],
        "automatic_model_expansion": False,
    }


def create_checkpoint_manifest(
    root: Path,
    manifest_path: str,
    components: list[dict[str, Any]],
    *,
    capability: str,
    required_components: list[str],
    lineage: dict[str, Any],
    resume_mode: str = "exact_resume",
) -> dict[str, Any]:
    """Write a strict hash-bound checkpoint manifest without ML dependencies."""
    if resume_mode not in _RESUME_MODES:
        raise FidelityError("resume_mode is invalid")
    if not isinstance(components, list) or not components:
        raise FidelityError("components must be a non-empty list")
    if not isinstance(required_components, list) or len(
        set(required_components)
    ) != len(required_components):
        raise FidelityError("required_components must be a unique list")
    if resume_mode == "exact_resume" and not set(
        _REQUIRED_COMPONENTS.get(capability.lower(), ())
    ).issubset(required_components):
        raise FidelityError("required_components is incomplete for capability")
    _keys(
        lineage,
        {"code_sha256", "data_sha256", "split_sha256", "config_sha256", "objective"},
        {"code_sha256", "data_sha256", "split_sha256", "config_sha256", "objective"},
        "lineage",
    )
    manifest_components: list[dict[str, Any]] = []
    for index, item in enumerate(components):
        entry = _as_dict(item, f"components[{index}]")
        _keys(
            entry, {"component", "path"}, {"component", "path"}, f"components[{index}]"
        )
        component_path = _path(
            root.resolve(), entry["path"], f"components[{index}].path"
        )
        if not component_path.is_file():
            raise FidelityError(f"missing checkpoint component: {component_path}")
        manifest_components.append(
            {
                "component": _text(
                    entry["component"], f"components[{index}].component"
                ),
                "path": component_path.relative_to(root.resolve()).as_posix(),
                "sha256": _sha256(component_path),
            }
        )
    output = _path(root.resolve(), manifest_path, "manifest_path")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "capability": capability,
        "resume_mode": resume_mode,
        "required_components": required_components,
        "components": manifest_components,
        "lineage": lineage,
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        **payload,
        "manifest_path": output.relative_to(root.resolve()).as_posix(),
        "manifest_sha256": _sha256(output),
    }


def verify_checkpoint_manifest(root: Path, manifest_path: str) -> dict[str, Any]:
    """Verify every component hash in a strict checkpoint manifest."""
    path = _path(root.resolve(), manifest_path, "manifest_path")
    if not path.is_file():
        raise FidelityError(f"missing checkpoint manifest: {manifest_path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FidelityError(f"checkpoint manifest unreadable: {exc}") from exc
    manifest = _as_dict(manifest, "checkpoint manifest")
    _keys(
        manifest,
        {
            "schema_version",
            "capability",
            "resume_mode",
            "required_components",
            "components",
            "lineage",
        },
        {
            "schema_version",
            "capability",
            "resume_mode",
            "required_components",
            "components",
            "lineage",
        },
        "checkpoint manifest",
    )
    errors: list[str] = []
    verified: list[str] = []
    for index, item in enumerate(manifest["components"]):
        entry = _as_dict(item, f"manifest.components[{index}]")
        _keys(
            entry,
            {"component", "path", "sha256"},
            {"component", "path", "sha256"},
            f"manifest.components[{index}]",
        )
        component_path = _path(
            root.resolve(), entry["path"], f"manifest.components[{index}].path"
        )
        if not component_path.is_file() or _sha256(component_path) != entry["sha256"]:
            errors.append(f"checkpoint component hash mismatch: {entry['component']}")
        else:
            verified.append(str(entry["component"]))
    return {
        "status": "PASS" if not errors else "FAIL",
        "verified_components": verified,
        "errors": errors,
    }
