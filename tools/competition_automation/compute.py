"""Deterministic planning and audit controls for external compute workers."""

from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import re
from typing import Any

from .fingerprint import canonical_json_digest, fingerprint_path


class ComputeError(ValueError):
    """Raised when a compute profile, request, job, or transition is unsafe."""


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
BACKEND_KINDS = frozenset({"local", "kaggle", "remote_gpu"})
RULE_SOURCE_KINDS = frozenset({"official_rules", "official_api", "official_ui_snapshot"})
ROLES = frozenset({"smoke", "eda", "preprocess", "train", "evaluate", "inference", "package"})
JOB_STATUSES = frozenset(
    {
        "planned",
        "dispatched",
        "running",
        "succeeded",
        "validated",
        "infra_failed",
        "code_failed",
        "preempted",
        "budget_blocked",
        "artifact_invalid",
    }
)
ACTIVE_STATUSES = frozenset({"planned", "dispatched", "running"})
RETRYABLE_STATUSES = frozenset({"infra_failed", "preempted"})
TRANSITIONS = {
    "planned": frozenset({"dispatched", "budget_blocked"}),
    "dispatched": frozenset({"running", "infra_failed", "preempted", "budget_blocked"}),
    "running": frozenset({"succeeded", "infra_failed", "code_failed", "preempted", "budget_blocked"}),
    "succeeded": frozenset({"validated", "artifact_invalid"}),
    "budget_blocked": frozenset({"planned"}),
}
RESOURCE_FIELDS = (
    "gpu_count",
    "vram_gb",
    "ram_gb",
    "storage_gb",
    "estimated_runtime_minutes",
    "max_cost",
)
SNAPSHOT_FIELDS = ("data", "code", "config", "split", "controller")


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(mapping).difference(allowed))
    if unknown:
        raise ComputeError(f"unknown {name} field(s): {unknown}")


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ComputeError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComputeError(f"{name} must be a non-empty string")
    return value.strip()


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ComputeError(f"{name} must be a lowercase SHA-256")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ComputeError(f"{name} must be boolean")
    return value


def _number(value: object, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComputeError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ComputeError(f"{name} must be finite and >= {minimum}")
    return number


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ComputeError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ComputeError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ComputeError(f"{name} must include a timezone")
    return text


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ComputeError(f"{name} must be a non-empty list")
    return [_text(item, f"{name}[{index}]") for index, item in enumerate(value)]


def _snapshot(value: object, root: Path, name: str) -> dict[str, str]:
    snapshot = _mapping(value, name)
    _reject_unknown(snapshot, {"path", "sha256"}, name)
    relative = _text(snapshot.get("path"), f"{name}.path")
    digest = _sha(snapshot.get("sha256"), f"{name}.sha256")
    root_resolved = root.expanduser().resolve()
    candidate = root_resolved / relative
    if candidate.is_symlink():
        raise ComputeError(f"{name} must not be a symlink")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ComputeError(f"{name} escapes root") from exc
    if not resolved.exists():
        raise ComputeError(f"{name} is missing: {relative}")
    actual = str(fingerprint_path(resolved)["sha256"])
    if actual != digest:
        raise ComputeError(f"{name} hash mismatch")
    return {"path": relative, "sha256": digest}


def _validate_resources(value: object, name: str) -> dict[str, float | int]:
    resources = _mapping(value, name)
    _reject_unknown(resources, set(RESOURCE_FIELDS), name)
    validated: dict[str, float | int] = {
        "gpu_count": _integer(resources.get("gpu_count"), f"{name}.gpu_count"),
    }
    for field in RESOURCE_FIELDS[1:]:
        validated[field] = _number(resources.get(field), f"{name}.{field}")
    return validated


def validate_compute_profile(profile: dict[str, Any], root: Path) -> dict[str, Any]:
    """Validate a competition-specific compute policy and its rules snapshot."""
    if not isinstance(profile, dict):
        raise ComputeError("compute profile must be an object")
    _reject_unknown(
        profile,
        {"schema_version", "competition", "rules", "budgets", "backends", "dispatch_policy"},
        "compute profile",
    )
    if profile.get("schema_version") != 1:
        raise ComputeError("schema_version must be 1")
    competition = _mapping(profile.get("competition"), "competition")
    _reject_unknown(competition, {"slug"}, "competition")
    _text(competition.get("slug"), "competition.slug")

    rules = _mapping(profile.get("rules"), "rules")
    _reject_unknown(
        rules,
        {
            "source",
            "external_compute_allowed",
            "raw_competition_data_external_allowed",
            "final_inference",
        },
        "rules",
    )
    source = _mapping(rules.get("source"), "rules.source")
    _reject_unknown(source, {"kind", "uri", "observed_at", "artifact_path", "sha256"}, "rules.source")
    source_kind = _text(source.get("kind"), "rules.source.kind")
    if source_kind not in RULE_SOURCE_KINDS:
        raise ComputeError(f"rules.source.kind is not official: {source_kind}")
    _text(source.get("uri"), "rules.source.uri")
    _timestamp(source.get("observed_at"), "rules.source.observed_at")
    _snapshot(
        {"path": source.get("artifact_path"), "sha256": source.get("sha256")},
        root,
        "rules.source",
    )
    _boolean(rules.get("external_compute_allowed"), "rules.external_compute_allowed")
    _boolean(
        rules.get("raw_competition_data_external_allowed"),
        "rules.raw_competition_data_external_allowed",
    )
    final_backend = None
    if rules.get("final_inference") is not None:
        final_inference = _mapping(rules["final_inference"], "rules.final_inference")
        _reject_unknown(
            final_inference,
            {"backend", "max_runtime_minutes", "internet_enabled", "required_output"},
            "rules.final_inference",
        )
        final_backend = _text(final_inference.get("backend"), "rules.final_inference.backend")
        _number(
            final_inference.get("max_runtime_minutes"),
            "rules.final_inference.max_runtime_minutes",
            1.0,
        )
        _boolean(final_inference.get("internet_enabled"), "rules.final_inference.internet_enabled")
        _text(final_inference.get("required_output"), "rules.final_inference.required_output")

    budgets = _mapping(profile.get("budgets"), "budgets")
    _reject_unknown(
        budgets,
        {"max_external_cost_total", "max_external_cost_per_job"},
        "budgets",
    )
    for field in budgets:
        _number(budgets[field], f"budgets.{field}")

    backends = profile.get("backends")
    if not isinstance(backends, list) or not backends:
        raise ComputeError("backends must be a non-empty list")
    names: set[str] = set()
    for index, value in enumerate(backends):
        backend = _mapping(value, f"backends[{index}]")
        _reject_unknown(
            backend,
            {
                "name",
                "kind",
                "priority",
                "allowed_roles",
                "gpu_count",
                "vram_gb",
                "ram_gb",
                "storage_gb",
                "max_runtime_minutes",
                "max_cost_per_job",
                "availability",
            },
            f"backends[{index}]",
        )
        name = _text(backend.get("name"), f"backends[{index}].name")
        if name in names:
            raise ComputeError(f"duplicate backend name: {name}")
        names.add(name)
        kind = _text(backend.get("kind"), f"backends[{index}].kind")
        if kind not in BACKEND_KINDS:
            raise ComputeError(f"backends[{index}].kind is invalid: {kind}")
        _integer(backend.get("priority"), f"backends[{index}].priority")
        roles = _string_list(backend.get("allowed_roles"), f"backends[{index}].allowed_roles")
        invalid_roles = sorted(set(roles).difference(ROLES))
        if invalid_roles:
            raise ComputeError(f"backends[{index}].allowed_roles contains invalid roles: {invalid_roles}")
        _validate_resources(
            {
                "gpu_count": backend.get("gpu_count"),
                "vram_gb": backend.get("vram_gb"),
                "ram_gb": backend.get("ram_gb"),
                "storage_gb": backend.get("storage_gb"),
                "estimated_runtime_minutes": backend.get("max_runtime_minutes"),
                "max_cost": backend.get("max_cost_per_job"),
            },
            f"backends[{index}].capacity",
        )
        availability = _mapping(backend.get("availability"), f"backends[{index}].availability")
        _reject_unknown(
            availability,
            {"available", "remaining_runtime_minutes", "observed_at", "artifact_path", "sha256"},
            f"backends[{index}].availability",
        )
        _boolean(availability.get("available"), f"backends[{index}].availability.available")
        _number(
            availability.get("remaining_runtime_minutes"),
            f"backends[{index}].availability.remaining_runtime_minutes",
        )
        _timestamp(availability.get("observed_at"), f"backends[{index}].availability.observed_at")
        _snapshot(
            {"path": availability.get("artifact_path"), "sha256": availability.get("sha256")},
            root,
            f"backends[{index}].availability",
        )
    if final_backend is not None and final_backend not in names:
        raise ComputeError("rules.final_inference.backend is not declared in backends")

    policy = _mapping(profile.get("dispatch_policy"), "dispatch_policy")
    _reject_unknown(
        policy,
        {
            "microbenchmark_required_roles",
            "utilization_limit",
            "duplicate_job_forbidden",
            "checkpoint_required_after_minutes",
            "artifact_hash_required",
        },
        "dispatch_policy",
    )
    benchmark_roles = _string_list(
        policy.get("microbenchmark_required_roles"),
        "dispatch_policy.microbenchmark_required_roles",
    )
    invalid_roles = sorted(set(benchmark_roles).difference(ROLES))
    if invalid_roles:
        raise ComputeError(f"invalid microbenchmark roles: {invalid_roles}")
    utilization = _number(policy.get("utilization_limit"), "dispatch_policy.utilization_limit")
    if utilization <= 0.0 or utilization > 1.0:
        raise ComputeError("dispatch_policy.utilization_limit must be in (0, 1]")
    if not _boolean(policy.get("duplicate_job_forbidden"), "dispatch_policy.duplicate_job_forbidden"):
        raise ComputeError("dispatch_policy.duplicate_job_forbidden must be true")
    _number(
        policy.get("checkpoint_required_after_minutes"),
        "dispatch_policy.checkpoint_required_after_minutes",
        1.0,
    )
    if not _boolean(policy.get("artifact_hash_required"), "dispatch_policy.artifact_hash_required"):
        raise ComputeError("dispatch_policy.artifact_hash_required must be true")
    return profile


def load_compute_profile(path: Path, root: Path) -> dict[str, Any]:
    """Load and validate a compute profile."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ComputeError(f"invalid compute profile JSON: {path}: {exc}") from exc
    return validate_compute_profile(value, root)


def validate_compute_request(request: dict[str, Any], root: Path) -> dict[str, Any]:
    """Validate a resource request before backend selection."""
    if not isinstance(request, dict):
        raise ComputeError("compute request must be an object")
    _reject_unknown(
        request,
        {
            "schema_version",
            "role",
            "resources",
            "data_access",
            "final_submission_inference",
            "network",
            "expected_output",
            "microbenchmark",
            "timeline",
        },
        "compute request",
    )
    if request.get("schema_version") != 1:
        raise ComputeError("compute request schema_version must be 1")
    role = _text(request.get("role"), "role")
    if role not in ROLES:
        raise ComputeError(f"role is invalid: {role}")
    _validate_resources(request.get("resources"), "resources")
    data_access = _mapping(request.get("data_access"), "data_access")
    _reject_unknown(data_access, {"raw_competition_data", "input_manifest"}, "data_access")
    _boolean(data_access.get("raw_competition_data"), "data_access.raw_competition_data")
    _snapshot(data_access.get("input_manifest"), root, "data_access.input_manifest")
    final_submission = _boolean(
        request.get("final_submission_inference"),
        "final_submission_inference",
    )
    if final_submission and role != "inference":
        raise ComputeError("final_submission_inference requires role='inference'")
    network = _mapping(request.get("network"), "network")
    _reject_unknown(network, {"internet_enabled"}, "network")
    _boolean(network.get("internet_enabled"), "network.internet_enabled")
    _text(request.get("expected_output"), "expected_output")
    if "timeline" in request:
        timeline = _mapping(request["timeline"], "timeline")
        _reject_unknown(
            timeline,
            {"observed_at", "deadline_at", "downstream_minutes", "safety_minutes", "deadline_source"},
            "timeline",
        )
        observed_text = _timestamp(timeline.get("observed_at"), "timeline.observed_at")
        deadline_text = _timestamp(timeline.get("deadline_at"), "timeline.deadline_at")
        observed = datetime.fromisoformat(observed_text.replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(deadline_text.replace("Z", "+00:00"))
        downstream = _number(timeline.get("downstream_minutes"), "timeline.downstream_minutes")
        safety = _number(timeline.get("safety_minutes"), "timeline.safety_minutes")
        _snapshot(timeline.get("deadline_source"), root, "timeline.deadline_source")
        critical_path = (
            float(request["resources"]["estimated_runtime_minutes"]) + downstream + safety
        )
        available = (deadline - observed).total_seconds() / 60
        if critical_path > available:
            raise ComputeError("critical_path_exceeds_deadline")
    if "microbenchmark" in request:
        benchmark = _mapping(request["microbenchmark"], "microbenchmark")
        _reject_unknown(benchmark, {"sample_count", "evidence"}, "microbenchmark")
        _integer(benchmark.get("sample_count"), "microbenchmark.sample_count", 1)
        _snapshot(benchmark.get("evidence"), root, "microbenchmark.evidence")
    return request


def load_compute_request(path: Path, root: Path) -> dict[str, Any]:
    """Load and validate a compute request."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ComputeError(f"invalid compute request JSON: {path}: {exc}") from exc
    return validate_compute_request(value, root)


def _backend_rejection_reasons(
    backend: dict[str, Any],
    profile: dict[str, Any],
    request: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    role = str(request["role"])
    resources = request["resources"]
    rules = profile["rules"]
    kind = str(backend["kind"])
    availability = backend["availability"]
    if not availability["available"]:
        reasons.append("backend_unavailable")
    if float(resources["estimated_runtime_minutes"]) > float(availability["remaining_runtime_minutes"]):
        reasons.append("remaining_runtime_minutes_exceeded")
    if role not in backend["allowed_roles"]:
        reasons.append("role_not_allowed")
    if kind == "remote_gpu" and not rules["external_compute_allowed"]:
        reasons.append("external_compute_forbidden")
    if (
        kind == "remote_gpu"
        and request["data_access"]["raw_competition_data"]
        and not rules["raw_competition_data_external_allowed"]
    ):
        reasons.append("raw_competition_data_external_forbidden")
    final = rules["final_inference"]
    if request["final_submission_inference"]:
        if backend["name"] != final["backend"]:
            reasons.append("final_inference_backend_mismatch")
        if request["network"]["internet_enabled"] != final["internet_enabled"]:
            reasons.append("final_inference_network_mismatch")
        if Path(str(request["expected_output"])).name != final["required_output"]:
            reasons.append("final_inference_output_mismatch")
        if float(resources["estimated_runtime_minutes"]) > float(final["max_runtime_minutes"]):
            reasons.append("final_inference_runtime_exceeded")
    utilization = float(profile["dispatch_policy"]["utilization_limit"])
    if int(resources["gpu_count"]) > int(backend["gpu_count"]):
        reasons.append("gpu_count_capacity_exceeded")
    capacity_pairs = (
        ("vram_gb", "vram_gb"),
        ("ram_gb", "ram_gb"),
        ("storage_gb", "storage_gb"),
        ("estimated_runtime_minutes", "max_runtime_minutes"),
    )
    for request_field, backend_field in capacity_pairs:
        capacity = float(backend[backend_field])
        required = float(resources[request_field])
        if required > capacity * utilization:
            reasons.append(f"{request_field}_capacity_exceeded")
    if kind == "remote_gpu":
        budget = float(profile["budgets"].get("max_external_cost_per_job", math.inf))
        backend_budget = float(backend["max_cost_per_job"])
        if float(resources["max_cost"]) > min(budget, backend_budget):
            reasons.append("external_cost_budget_exceeded")
    return sorted(set(reasons))


def plan_compute_request(
    profile: dict[str, Any],
    request: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    """Select the first feasible backend using fixed profile priority."""
    validate_compute_profile(profile, root)
    validate_compute_request(request, root)
    if request["final_submission_inference"] and profile["rules"]["final_inference"] is None:
        raise ComputeError("final_inference_not_configured")
    required_roles = set(profile["dispatch_policy"]["microbenchmark_required_roles"])
    if request["role"] in required_roles and "microbenchmark" not in request:
        raise ComputeError(f"role={request['role']} requires a materialized microbenchmark")
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for backend in profile["backends"]:
        reasons = _backend_rejection_reasons(backend, profile, request)
        item = {"name": backend["name"], "kind": backend["kind"], "priority": backend["priority"]}
        if reasons:
            rejected.append({**item, "reasons": reasons})
        else:
            eligible.append(item)
    eligible.sort(key=lambda item: (int(item["priority"]), str(item["name"])))
    rejected.sort(key=lambda item: (int(item["priority"]), str(item["name"])))
    if not eligible:
        raise ComputeError(f"no eligible compute backend: {rejected}")
    return {
        "status": "PASS",
        "selected_backend": eligible[0]["name"],
        "eligible_backends": eligible,
        "rejected_backends": rejected,
    }


def _validate_checkpoint(
    value: object,
    estimated_runtime: float,
    threshold: float,
    root: Path,
) -> dict[str, Any]:
    checkpoint = _mapping(value, "checkpoint")
    _reject_unknown(checkpoint, {"enabled", "interval_minutes", "path", "resume_from"}, "checkpoint")
    enabled = _boolean(checkpoint.get("enabled"), "checkpoint.enabled")
    interval = _number(checkpoint.get("interval_minutes"), "checkpoint.interval_minutes")
    path = _text(checkpoint.get("path"), "checkpoint.path")
    if estimated_runtime > threshold and not enabled:
        raise ComputeError("long-running jobs require checkpoint.enabled=true")
    if enabled and (interval <= 0.0 or interval > threshold):
        raise ComputeError("checkpoint.interval_minutes must be in (0, checkpoint threshold]")
    if not enabled and interval != 0.0:
        raise ComputeError("disabled checkpoints require interval_minutes=0")
    if Path(path).is_absolute() or ".." in Path(path).parts:
        raise ComputeError("checkpoint.path must be a safe relative path")
    if checkpoint.get("resume_from") is not None:
        _snapshot(checkpoint["resume_from"], root, "checkpoint.resume_from")
    return checkpoint


def compute_job_signature(job: dict[str, Any]) -> str:
    """Return the stable identity of one backend work unit."""
    checkpoint = job["checkpoint"]
    payload = {
        "experiment_signature": job["experiment_signature"],
        "work_unit": job["work_unit"],
        "command": job["command"],
        "request": job["request"],
        "profile": job["profile"],
        "snapshots": job["snapshots"],
        "checkpoint": {
            "enabled": checkpoint["enabled"],
            "interval_minutes": checkpoint["interval_minutes"],
            "path": checkpoint["path"],
        },
    }
    return canonical_json_digest(payload)


def _validate_artifacts(value: object, root: Path) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ComputeError("artifacts must be a non-empty list")
    artifacts = [_snapshot(item, root, f"artifacts[{index}]") for index, item in enumerate(value)]
    paths = [item["path"] for item in artifacts]
    if len(paths) != len(set(paths)):
        raise ComputeError("artifacts contains duplicate paths")
    return artifacts


def _validate_runtime(value: object, status: str) -> dict[str, Any]:
    runtime = _mapping(value, "runtime")
    _reject_unknown(
        runtime,
        {"backend_job_id", "started_at", "finished_at", "actual_runtime_minutes", "actual_cost"},
        "runtime",
    )
    if status not in {"planned", "budget_blocked"}:
        _text(runtime.get("backend_job_id"), "runtime.backend_job_id")
    if status not in ACTIVE_STATUSES and status != "budget_blocked":
        started = _timestamp(runtime.get("started_at"), "runtime.started_at")
        finished = _timestamp(runtime.get("finished_at"), "runtime.finished_at")
        if datetime.fromisoformat(finished.replace("Z", "+00:00")) < datetime.fromisoformat(
            started.replace("Z", "+00:00")
        ):
            raise ComputeError("runtime.finished_at precedes started_at")
        _number(runtime.get("actual_runtime_minutes"), "runtime.actual_runtime_minutes")
        _number(runtime.get("actual_cost"), "runtime.actual_cost")
    return runtime


def _validate_checks(value: object, root: Path) -> dict[str, Any]:
    checks = _mapping(value, "checks")
    for required in ("artifact", "reproducibility"):
        check = _mapping(checks.get(required), f"checks.{required}")
        _reject_unknown(check, {"status", "evidence"}, f"checks.{required}")
        if check.get("status") != "PASS":
            raise ComputeError(f"checks.{required}.status must be PASS")
        _snapshot(check.get("evidence"), root, f"checks.{required}.evidence")
    return checks


def validate_compute_job(
    job: dict[str, Any],
    profile: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    """Validate one immutable compute job state against a compute profile."""
    if not isinstance(job, dict):
        raise ComputeError("compute job must be an object")
    _reject_unknown(
        job,
        {
            "schema_version",
            "job_id",
            "recorded_at",
            "experiment_signature",
            "job_signature",
            "work_unit",
            "backend",
            "status",
            "command",
            "request",
            "profile",
            "snapshots",
            "checkpoint",
            "retry_of",
            "retry_reason",
            "runtime",
            "artifacts",
            "checks",
        },
        "compute job",
    )
    if job.get("schema_version") != 1:
        raise ComputeError("compute job schema_version must be 1")
    _text(job.get("job_id"), "job_id")
    _timestamp(job.get("recorded_at"), "recorded_at")
    _sha(job.get("experiment_signature"), "experiment_signature")
    _text(job.get("work_unit"), "work_unit")
    backend = _text(job.get("backend"), "backend")
    status = _text(job.get("status"), "status")
    if status not in JOB_STATUSES:
        raise ComputeError(f"invalid job status: {status}")
    command = job.get("command")
    if not isinstance(command, list) or not command:
        raise ComputeError("command must be a non-empty argv list")
    for index, item in enumerate(command):
        _text(item, f"command[{index}]")
    request = _mapping(job.get("request"), "request")
    profile_snapshot = _snapshot(job.get("profile"), root, "profile")
    profile_path = root.expanduser().resolve() / profile_snapshot["path"]
    try:
        stored_profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ComputeError("profile snapshot is not valid JSON") from exc
    validate_compute_profile(stored_profile, root)
    if canonical_json_digest(stored_profile) != canonical_json_digest(profile):
        raise ComputeError("passed compute profile does not match the job profile snapshot")
    plan = plan_compute_request(profile, request, root)
    if backend != plan["selected_backend"]:
        raise ComputeError(
            f"backend={backend!r} is not the deterministic selected backend {plan['selected_backend']!r}"
        )
    snapshots = _mapping(job.get("snapshots"), "snapshots")
    _reject_unknown(snapshots, set(SNAPSHOT_FIELDS), "snapshots")
    validated_snapshots = [
        _snapshot(snapshots.get(field), root, f"snapshots.{field}") for field in SNAPSHOT_FIELDS
    ]
    paths = [item["path"] for item in validated_snapshots]
    if len(paths) != len(set(paths)):
        raise ComputeError("data/code/config/split/controller snapshots must use distinct paths")
    controller_path = root.expanduser().resolve() / snapshots["controller"]["path"]
    try:
        controller_result = json.loads(controller_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ComputeError("snapshots.controller is not valid JSON") from exc
    if not isinstance(controller_result, dict) or controller_result.get("allowed") is not True:
        raise ComputeError("snapshots.controller must contain allowed=true")
    estimated_runtime = float(request["resources"]["estimated_runtime_minutes"])
    threshold = float(profile["dispatch_policy"]["checkpoint_required_after_minutes"])
    _validate_checkpoint(job.get("checkpoint"), estimated_runtime, threshold, root)
    if job.get("retry_of") is not None:
        _text(job["retry_of"], "retry_of")
        _text(job.get("retry_reason"), "retry_reason")
    elif job.get("retry_reason") is not None:
        raise ComputeError("retry_reason requires retry_of")
    _validate_runtime(job.get("runtime"), status)
    if status in {"succeeded", "validated"}:
        artifacts = _validate_artifacts(job.get("artifacts"), root)
        expected_output = request["expected_output"]
        output_paths = {item["path"] for item in artifacts}
        present = (
            expected_output in output_paths
            if Path(expected_output).name != expected_output
            else any(Path(path).name == expected_output for path in output_paths)
        )
        if status == "validated" and not present:
            raise ComputeError(
                f"validated job is missing expected_output artifact: {expected_output}"
            )
    elif job.get("artifacts") not in (None, []):
        _validate_artifacts(job["artifacts"], root)
    if status == "validated":
        _validate_checks(job.get("checks"), root)
    elif job.get("checks") not in (None, {}):
        _mapping(job["checks"], "checks")
    computed = compute_job_signature(job)
    supplied = job.get("job_signature")
    if supplied is not None and supplied != computed:
        raise ComputeError("job_signature does not match the canonical job payload")
    return {
        "status": "PASS",
        "job_id": job["job_id"],
        "job_status": status,
        "job_signature": computed,
        "selected_backend": backend,
    }


def load_compute_job(path: Path) -> dict[str, Any]:
    """Load a compute job without accepting prose as validation evidence."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ComputeError(f"invalid compute job JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ComputeError("compute job root must be an object")
    return value


def read_compute_ledger(path: Path) -> list[dict[str, Any]]:
    """Read an append-only compute job state ledger."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ComputeError(f"invalid JSON at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ComputeError(f"compute ledger record at {path}:{line_number} is not an object")
        records.append(value)
    return records


def _validate_history_transition(job: dict[str, Any], records: list[dict[str, Any]]) -> None:
    job_id = str(job["job_id"])
    signature = compute_job_signature(job)
    same_id = [item for item in records if item.get("job_id") == job_id]
    if same_id:
        previous = same_id[-1]
        if compute_job_signature(previous) != signature:
            raise ComputeError("job payload changed across status events")
        if previous.get("backend") != job.get("backend"):
            raise ComputeError("backend changed within one job attempt")
        previous_status = str(previous["status"])
        allowed = TRANSITIONS.get(previous_status, frozenset())
        if job["status"] not in allowed:
            raise ComputeError(f"invalid compute job transition: {previous_status} -> {job['status']}")
        return

    latest_by_id: dict[str, dict[str, Any]] = {}
    for item in records:
        latest_by_id[str(item["job_id"])] = item
    collisions = [
        item for item in latest_by_id.values() if compute_job_signature(item) == signature
    ]
    if not collisions:
        if job.get("retry_of") is not None:
            raise ComputeError("retry_of does not reference an existing matching job")
        return
    retry_of = job.get("retry_of")
    retry_matches = [item for item in collisions if item.get("job_id") == retry_of]
    non_retryable = [item for item in collisions if item.get("status") not in RETRYABLE_STATUSES]
    if (
        non_retryable
        or
        retry_of is None
        or not retry_matches
        or retry_matches[-1].get("status") not in RETRYABLE_STATUSES
    ):
        raise ComputeError("duplicate compute job is forbidden unless retrying infra_failed/preempted work")


def _validate_ledger_integrity(records: list[dict[str, Any]]) -> None:
    """Verify stored signatures and the complete append-only transition history."""
    history: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        if item.get("schema_version") != 1:
            raise ComputeError(f"compute ledger record {index} has an invalid schema_version")
        _text(item.get("job_id"), f"compute ledger record {index}.job_id")
        status = _text(item.get("status"), f"compute ledger record {index}.status")
        if status not in JOB_STATUSES:
            raise ComputeError(f"compute ledger record {index} has an invalid status")
        computed = compute_job_signature(item)
        supplied = _sha(item.get("job_signature"), f"compute ledger record {index}.job_signature")
        if supplied != computed:
            raise ComputeError(f"compute ledger record {index} job_signature mismatch")
        _validate_history_transition(item, history)
        history.append(item)


def append_compute_job(
    ledger_path: Path,
    job: dict[str, Any],
    profile: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    """Append a validated compute state event after transition and duplicate checks."""
    result = validate_compute_job(job, profile, root)
    records = read_compute_ledger(ledger_path)
    _validate_ledger_integrity(records)
    _validate_history_transition(job, records)
    latest_by_id: dict[str, dict[str, Any]] = {
        str(item["job_id"]): item for item in records
    }
    latest_by_id[str(job["job_id"])] = job
    backend_kinds = {str(item["name"]): str(item["kind"]) for item in profile["backends"]}
    committed_cost = 0.0
    for item in latest_by_id.values():
        if backend_kinds.get(str(item.get("backend"))) != "remote_gpu":
            continue
        runtime = item.get("runtime", {})
        if item.get("status") in ACTIVE_STATUSES:
            committed_cost += float(item["request"]["resources"]["max_cost"])
        elif isinstance(runtime, dict) and isinstance(runtime.get("actual_cost"), (int, float)):
            committed_cost += float(runtime["actual_cost"])
    total_budget = float(profile["budgets"].get("max_external_cost_total", math.inf))
    budget_exceeded = committed_cost > total_budget
    if budget_exceeded and job["status"] in ACTIVE_STATUSES:
        raise ComputeError(
            f"external compute cost commitment {committed_cost} exceeds total budget {total_budget}"
        )
    record = dict(job)
    record["job_signature"] = result["job_signature"]
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(encoded + "\n")
    return {
        **result,
        "ledger": str(ledger_path.expanduser().resolve()),
        "record_digest": canonical_json_digest(record),
        "external_cost_committed": committed_cost,
        "external_cost_budget_status": "EXCEEDED" if budget_exceeded else "WITHIN_BUDGET",
    }
