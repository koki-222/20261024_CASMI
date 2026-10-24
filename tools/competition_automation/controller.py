"""Deterministic controller gate for the next automation action.

The controller does not generate hypotheses.  It prevents an orchestrating
agent from repeating an unchanged audit, retrying a known provider failure,
re-running an existing hypothesis, or reopening an exhausted family without
new machine-verifiable input.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fingerprint import canonical_json_digest, fingerprint_path
from .signature import computation_payload, computation_signature


class ControllerError(ValueError):
    """Raised when a controller state is malformed or its evidence is stale."""


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ACTION_TYPES = frozenset({"audit", "experiment", "provider_dispatch", "resume_check", "wait"})
FAMILY_STATUSES = frozenset({"open", "closed"})
CHANGE_SCOPES = frozenset(
    {"hypothesis", "model", "feature", "data", "split", "postprocess", "seed_only", "control", "reproduction"}
)
TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "state_inputs",
        "previous_audit_state_fingerprint",
        "blocked_state",
        "policy",
        "providers",
        "families",
        "experiment_history",
        "evaluation_policy",
        "hypothesis_source",
        "action",
    }
)
ACTION_KEYS = {
    "audit": frozenset({"type", "provider"}),
    "provider_dispatch": frozenset({"type", "provider"}),
    "resume_check": frozenset({"type"}),
    "wait": frozenset({"type"}),
    "experiment": frozenset(
        {
            "type",
            "provider",
            "family",
            "hypothesis_signature",
            "mechanism_signature",
            "input_signature",
            "change_scope",
            "seed_averaging",
            "control",
            "run_snapshot",
            "external_input",
            "coverage_preflight",
            "batch_plan",
            "reopen",
            "evaluation_plan",
            "source_issue",
            "computation_spec",
        }
    ),
}
GITHUB_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControllerError(f"{field} must be a non-empty string")
    return value.strip()


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ControllerError(f"{field} must be a lowercase SHA-256")
    return value


def _integer(value: object, field: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ControllerError(f"{field} must be an integer >= {minimum}")
    return value


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControllerError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ControllerError(f"{field} must include a timezone")
    return parsed


def _snapshot(root: Path, value: object, field: str) -> dict[str, str]:
    """Validate one immutable file snapshot below ``root``."""
    if not isinstance(value, dict):
        raise ControllerError(f"{field} must be an object")
    unknown = sorted(set(value).difference({"path", "sha256"}))
    if unknown:
        raise ControllerError(f"unknown {field} field(s): {unknown}")
    relative = _text(value.get("path"), f"{field}.path")
    digest = _sha(value.get("sha256"), f"{field}.sha256")
    root_resolved = root.expanduser().resolve()
    raw = root_resolved / relative
    if raw.is_symlink():
        raise ControllerError(f"{field} must not be a symlink")
    resolved = raw.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ControllerError(f"{field} escapes root") from exc
    if not resolved.is_file():
        raise ControllerError(f"{field} is missing: {relative}")
    actual = str(fingerprint_path(resolved)["sha256"])
    if actual != digest:
        raise ControllerError(f"{field} hash mismatch")
    return {"path": relative, "sha256": digest}


def _hypothesis_source(state: dict[str, Any]) -> dict[str, Any] | None:
    """Validate an optional user-authored GitHub Issue intake policy."""
    policy = state.get("hypothesis_source")
    if policy is None:
        return None
    if not isinstance(policy, dict) or set(policy) != {"mode", "repository", "allowed_authors"}:
        raise ControllerError("hypothesis_source needs mode, repository, and allowed_authors")
    if policy["mode"] != "user_github_issue":
        raise ControllerError("hypothesis_source.mode must be user_github_issue")
    repository = _text(policy["repository"], "hypothesis_source.repository")
    if not GITHUB_REPOSITORY_RE.fullmatch(repository):
        raise ControllerError("hypothesis_source.repository must be owner/repo")
    authors = policy["allowed_authors"]
    if not isinstance(authors, list) or not authors:
        raise ControllerError("hypothesis_source.allowed_authors must be non-empty")
    if any(not isinstance(author, str) or not author.strip() for author in authors):
        raise ControllerError("hypothesis_source.allowed_authors must contain logins")
    if len(authors) != len(set(authors)):
        raise ControllerError("hypothesis_source.allowed_authors contains duplicates")
    return policy


def _issue_decision(
    state: dict[str, Any], action: dict[str, Any], root: Path, policy: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Bind an experiment to a frozen, user-authored GitHub Issue snapshot."""
    if policy is None:
        return None
    issue = action.get("source_issue")
    if issue is None:
        return {
            "allowed": False,
            "decision": "WAITING_FOR_USER_ISSUE",
            "reason": "a user-authored GitHub Issue is required before experiment PDCA",
        }
    if not isinstance(issue, dict) or set(issue) != {"number", "snapshot"}:
        raise ControllerError("action.source_issue needs number and snapshot")
    number = _integer(issue["number"], "action.source_issue.number", 1)
    snapshot = _snapshot(root, issue["snapshot"], "action.source_issue.snapshot")
    if snapshot not in state["state_inputs"]:
        raise ControllerError("source_issue.snapshot must be listed in state_inputs")
    path = root.expanduser().resolve() / snapshot["path"]
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ControllerError("source_issue snapshot is invalid JSON") from exc
    if not isinstance(saved, dict):
        raise ControllerError("source_issue snapshot must be an object")
    expected_url = f"https://github.com/{policy['repository']}/issues/{number}"
    author = saved.get("author")
    if (
        not isinstance(saved.get("number"), int)
        or isinstance(saved.get("number"), bool)
        or saved["number"] != number
        or saved.get("url") != expected_url
        or saved.get("state") != "OPEN"
        or not isinstance(author, dict)
        or author.get("login") not in policy["allowed_authors"]
    ):
        raise ControllerError("source_issue identity, state, or author does not match policy")
    _text(saved.get("title"), "source_issue.title")
    _text(saved.get("body"), "source_issue.body")
    _timestamp(saved.get("updatedAt"), "source_issue.updatedAt")
    return None


def _state_fingerprint(state: dict[str, Any], root: Path) -> tuple[str, list[dict[str, str]]]:
    values = state.get("state_inputs")
    if not isinstance(values, list) or not values:
        raise ControllerError("state_inputs must be a non-empty list")
    snapshots = [_snapshot(root, value, f"state_inputs[{index}]") for index, value in enumerate(values)]
    paths = [item["path"] for item in snapshots]
    if len(paths) != len(set(paths)):
        raise ControllerError("state_inputs contains duplicate paths")
    snapshots.sort(key=lambda item: item["path"])
    return canonical_json_digest(snapshots), snapshots


def provider_error_fingerprint(provider: str, code: str, message: str) -> str:
    """Return a stable provider-error identity without volatile request IDs."""
    normalized_message = " ".join(_text(message, "message").lower().split())
    return canonical_json_digest(
        {
            "provider": _text(provider, "provider").lower(),
            "code": _text(code, "code").lower(),
            "message": normalized_message,
        }
    )


def _policy(state: dict[str, Any]) -> dict[str, int]:
    raw = state.get("policy", {})
    if not isinstance(raw, dict):
        raise ControllerError("policy must be an object")
    unknown = sorted(set(raw).difference({"provider_failure_threshold", "family_failure_threshold"}))
    if unknown:
        raise ControllerError(f"unknown policy field(s): {unknown}")
    return {
        "provider_failure_threshold": _integer(
            raw.get("provider_failure_threshold", 1),
            "policy.provider_failure_threshold",
            1,
        ),
        "family_failure_threshold": _integer(
            raw.get("family_failure_threshold", 3),
            "policy.family_failure_threshold",
            1,
        ),
    }


def _provider_block(
    state: dict[str, Any],
    provider: str | None,
    now: datetime,
    threshold: int,
) -> dict[str, Any] | None:
    values = state.get("providers", [])
    if not isinstance(values, list):
        raise ControllerError("providers must be a list")
    matches: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ControllerError(f"providers[{index}] must be an object")
        unknown = sorted(
            set(value).difference(
                {"name", "error_fingerprint", "consecutive_failures", "retry_after"}
            )
        )
        if unknown:
            raise ControllerError(f"unknown providers[{index}] field(s): {unknown}")
        name = _text(value.get("name"), f"providers[{index}].name")
        failures = _integer(value.get("consecutive_failures", 0), f"providers[{index}].consecutive_failures")
        if "error_fingerprint" in value:
            _sha(value["error_fingerprint"], f"providers[{index}].error_fingerprint")
        retry_after = None
        if value.get("retry_after") is not None:
            retry_after = _timestamp(value["retry_after"], f"providers[{index}].retry_after")
        if name == provider and failures >= threshold and (retry_after is None or now < retry_after):
            matches.append({"name": name, "consecutive_failures": failures, "retry_after": value.get("retry_after")})
    if not matches:
        return None
    return {
        "allowed": False,
        "decision": "PROVIDER_CIRCUIT_OPEN",
        "reason": "the provider has reached the configured identical-failure threshold",
        "provider": provider,
        "provider_state": matches,
    }


def _blocked_decision(
    state: dict[str, Any],
    action_type: str,
    current_fingerprint: str,
    now: datetime,
    job_id: str | None = None,
) -> dict[str, Any] | None:
    value = state.get("blocked_state")
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("active"), bool):
        raise ControllerError("blocked_state.active must be boolean")
    unknown = sorted(
        set(value).difference(
            {
                "active", "reason", "resume_condition", "state_fingerprint",
                "next_check_at", "affected_job_ids",
            }
        )
    )
    if unknown:
        raise ControllerError(f"unknown blocked_state field(s): {unknown}")
    if not value["active"]:
        return None
    _text(value.get("reason"), "blocked_state.reason")
    _text(value.get("resume_condition"), "blocked_state.resume_condition")
    blocked_fingerprint = _sha(value.get("state_fingerprint"), "blocked_state.state_fingerprint")
    affected = value.get("affected_job_ids")
    if affected is not None:
        if (
            not isinstance(affected, list) or not affected
            or any(not isinstance(key, str) or not key.strip() for key in affected)
            or len(affected) != len(set(affected))
        ):
            raise ControllerError("affected_job_ids must be distinct nonempty identifiers")
        if action_type == "experiment":
            if job_id is None:
                return {"allowed": False, "decision": "JOB_CONTEXT_REQUIRED"}
            if job_id not in affected:
                return None
    next_check = None
    if value.get("next_check_at") is not None:
        next_check = _timestamp(value["next_check_at"], "blocked_state.next_check_at")
    if action_type != "resume_check":
        return {
            "allowed": False,
            "decision": "RESUME_CHECK_REQUIRED",
            "reason": "blocked work must pass a resume_check before another audit or experiment",
        }
    state_changed = current_fingerprint != blocked_fingerprint
    time_due = next_check is not None and now >= next_check
    if state_changed or time_due:
        return {
            "allowed": True,
            "decision": "RESUME_DUE",
            "state_changed": state_changed,
            "time_due": time_due,
        }
    return {
        "allowed": False,
        "decision": "WAITING_FOR_RESUME",
        "reason": "neither the evidence fingerprint nor the scheduled resume time changed",
        "next_check_at": value.get("next_check_at"),
    }


def _history(state: dict[str, Any]) -> set[str]:
    values = state.get("experiment_history", [])
    if not isinstance(values, list):
        raise ControllerError("experiment_history must be a list")
    checked = [_sha(value, f"experiment_history[{index}]") for index, value in enumerate(values)]
    if len(checked) != len(set(checked)):
        raise ControllerError("experiment_history contains duplicate signatures")
    return set(checked)


def _family(state: dict[str, Any], name: str | None) -> dict[str, Any] | None:
    values = state.get("families", [])
    if not isinstance(values, list):
        raise ControllerError("families must be a list")
    found = None
    seen: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ControllerError(f"families[{index}] must be an object")
        unknown = sorted(
            set(value).difference(
                {
                    "name",
                    "status",
                    "attempts",
                    "budget",
                    "consecutive_robust_failures",
                    "mechanism_signatures",
                    "input_signatures",
                    "batch_required",
                }
            )
        )
        if unknown:
            raise ControllerError(f"unknown families[{index}] field(s): {unknown}")
        family_name = _text(value.get("name"), f"families[{index}].name")
        if family_name in seen:
            raise ControllerError(f"duplicate family: {family_name}")
        seen.add(family_name)
        status = value.get("status")
        if status not in FAMILY_STATUSES:
            raise ControllerError(f"families[{index}].status is invalid")
        attempts = _integer(value.get("attempts"), f"families[{index}].attempts")
        budget = _integer(value.get("budget"), f"families[{index}].budget", 1)
        failures = _integer(
            value.get("consecutive_robust_failures", 0),
            f"families[{index}].consecutive_robust_failures",
        )
        for field in ("mechanism_signatures", "input_signatures"):
            signatures = value.get(field, [])
            if not isinstance(signatures, list):
                raise ControllerError(f"families[{index}].{field} must be a list")
            for signature_index, signature in enumerate(signatures):
                _sha(signature, f"families[{index}].{field}[{signature_index}]")
        if "batch_required" in value and not isinstance(value["batch_required"], bool):
            raise ControllerError(f"families[{index}].batch_required must be boolean")
        if family_name == name:
            found = {
                **value,
                "attempts": attempts,
                "budget": budget,
                "consecutive_robust_failures": failures,
            }
    return found


def _control(action: dict[str, Any]) -> bool:
    value = action.get("control")
    if not isinstance(value, dict):
        return False
    unknown = sorted(set(value).difference({"reference_record_id", "reason"}))
    if unknown:
        raise ControllerError(f"unknown action.control field(s): {unknown}")
    _text(value.get("reference_record_id"), "action.control.reference_record_id")
    _text(value.get("reason"), "action.control.reason")
    return True


def _batch(action: dict[str, Any], family: dict[str, Any], root: Path, history: set[str]) -> None:
    value = action.get("batch_plan")
    if value is None and family.get("batch_required") is not True:
        return
    if not isinstance(value, dict):
        raise ControllerError("this family requires action.batch_plan")
    unknown = sorted(
        set(value).difference({"candidate_signatures", "stop_rule", "shared_preflight"})
    )
    if unknown:
        raise ControllerError(f"unknown batch_plan field(s): {unknown}")
    signatures = value.get("candidate_signatures")
    if not isinstance(signatures, list) or len(signatures) < 2:
        raise ControllerError("batch_plan.candidate_signatures must contain at least two candidates")
    checked = [_sha(item, f"batch_plan.candidate_signatures[{index}]") for index, item in enumerate(signatures)]
    if len(checked) != len(set(checked)):
        raise ControllerError("batch_plan contains duplicate candidate signatures")
    if history.intersection(checked):
        raise ControllerError("batch_plan repeats an experiment_history signature")
    _text(value.get("stop_rule"), "batch_plan.stop_rule")
    _snapshot(root, value.get("shared_preflight"), "batch_plan.shared_preflight")


def _evaluation_policy(state: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """Validate the distribution-aware evaluation policy when configured."""
    value = state.get("evaluation_policy")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ControllerError("evaluation_policy must be an object")
    unknown = sorted(
        set(value).difference(
            {
                "profile_manifest",
                "selection_profile",
                "diagnostic_profiles",
                "required_stress_profiles",
                "max_official_feedback_rounds_per_family",
            }
        )
    )
    if unknown:
        raise ControllerError(f"unknown evaluation_policy field(s): {unknown}")
    manifest = _snapshot(root, value.get("profile_manifest"), "evaluation_policy.profile_manifest")
    selection_profile = _text(value.get("selection_profile"), "evaluation_policy.selection_profile")
    diagnostic = value.get("diagnostic_profiles", [])
    stress = value.get("required_stress_profiles", [])
    for field, profiles in (
        ("diagnostic_profiles", diagnostic),
        ("required_stress_profiles", stress),
    ):
        if not isinstance(profiles, list):
            raise ControllerError(f"evaluation_policy.{field} must be a list")
        checked = [
            _text(item, f"evaluation_policy.{field}[{index}]")
            for index, item in enumerate(profiles)
        ]
        if len(checked) != len(set(checked)):
            raise ControllerError(f"evaluation_policy.{field} contains duplicates")
    if selection_profile in set(diagnostic):
        raise ControllerError("selection_profile cannot also be diagnostic")
    manifest_path = root.expanduser().resolve() / manifest["path"]
    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ControllerError("evaluation profile manifest must be valid JSON") from exc
    if not isinstance(manifest_value, dict) or manifest_value.get("schema_version") != 1:
        raise ControllerError("evaluation profile manifest schema_version must be 1")
    unknown_manifest = sorted(set(manifest_value).difference({"schema_version", "profiles"}))
    if unknown_manifest:
        raise ControllerError(f"unknown evaluation profile manifest field(s): {unknown_manifest}")
    raw_profiles = manifest_value.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ControllerError("evaluation profile manifest profiles must be a non-empty list")
    profile_roles: dict[str, str] = {}
    for index, profile in enumerate(raw_profiles):
        if not isinstance(profile, dict):
            raise ControllerError(f"evaluation profiles[{index}] must be an object")
        unknown_profile = sorted(set(profile).difference({"name", "role", "distribution"}))
        if unknown_profile:
            raise ControllerError(
                f"unknown evaluation profiles[{index}] field(s): {unknown_profile}"
            )
        name = _text(profile.get("name"), f"evaluation profiles[{index}].name")
        role = profile.get("role")
        if role not in {"diagnostic", "selection", "stress", "official_feedback"}:
            raise ControllerError(f"evaluation profiles[{index}].role is invalid")
        _text(profile.get("distribution"), f"evaluation profiles[{index}].distribution")
        if name in profile_roles:
            raise ControllerError(f"duplicate evaluation profile: {name}")
        profile_roles[name] = str(role)
    if profile_roles.get(selection_profile) != "selection":
        raise ControllerError("evaluation_policy.selection_profile must have role selection")
    if any(profile_roles.get(name) != "diagnostic" for name in diagnostic):
        raise ControllerError("evaluation_policy.diagnostic_profiles must have role diagnostic")
    if any(profile_roles.get(name) != "stress" for name in stress):
        raise ControllerError("evaluation_policy.required_stress_profiles must have role stress")
    return {
        "profile_manifest": manifest,
        "selection_profile": selection_profile,
        "diagnostic_profiles": list(diagnostic),
        "required_stress_profiles": list(stress),
        "max_official_feedback_rounds_per_family": _integer(
            value.get("max_official_feedback_rounds_per_family", 0),
            "evaluation_policy.max_official_feedback_rounds_per_family",
        ),
    }


def _evaluation_decision(
    action: dict[str, Any], policy: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Require the registered selection distribution and stress profiles."""
    if policy is None:
        return None
    value = action.get("evaluation_plan")
    if not isinstance(value, dict):
        raise ControllerError("evaluation_policy requires action.evaluation_plan")
    unknown = sorted(
        set(value).difference(
            {"selection_profile", "stress_profiles", "official_feedback_round"}
        )
    )
    if unknown:
        raise ControllerError(f"unknown action.evaluation_plan field(s): {unknown}")
    selection_profile = _text(
        value.get("selection_profile"), "action.evaluation_plan.selection_profile"
    )
    if selection_profile != policy["selection_profile"]:
        return {
            "allowed": False,
            "decision": "SELECTION_DISTRIBUTION_MISMATCH",
            "reason": (
                "the experiment does not use the registered "
                "deployment-aligned selection profile"
            ),
        }
    stress = value.get("stress_profiles")
    if not isinstance(stress, list):
        raise ControllerError("action.evaluation_plan.stress_profiles must be a list")
    checked_stress = [
        _text(item, f"action.evaluation_plan.stress_profiles[{index}]")
        for index, item in enumerate(stress)
    ]
    missing = sorted(set(policy["required_stress_profiles"]).difference(checked_stress))
    if missing:
        return {
            "allowed": False,
            "decision": "STRESS_PROFILE_MISSING",
            "reason": "required stress evaluation profiles are missing",
            "missing_profiles": missing,
        }
    feedback_round = _integer(
        value.get("official_feedback_round", 0),
        "action.evaluation_plan.official_feedback_round",
    )
    if feedback_round > policy["max_official_feedback_rounds_per_family"]:
        return {
            "allowed": False,
            "decision": "OFFICIAL_FEEDBACK_BUDGET_EXCEEDED",
            "reason": "the family exceeded its registered adaptive official-feedback budget",
            "feedback_round": feedback_round,
        }
    return None


def _experiment_decision(
    state: dict[str, Any],
    action: dict[str, Any],
    root: Path,
    family_threshold: int,
    evaluation_policy: dict[str, Any] | None,
    issue_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    issue_block = _issue_decision(state, action, root, issue_policy)
    if issue_block is not None:
        return issue_block
    family_name = _text(action.get("family"), "action.family")
    hypothesis = _sha(action.get("hypothesis_signature"), "action.hypothesis_signature")
    spec = action.get("computation_spec")
    if spec is not None:
        payload = computation_payload(spec)
        expected = computation_signature(spec, ignore_seed=True)
        if hypothesis != expected:
            raise ControllerError("hypothesis_signature must match computation_spec")
        for group in ("code", "data", "split"):
            for role, snapshot in spec[group].items():
                _snapshot(root, snapshot, f"computation_spec.{group}.{role}")
        if action.get("mechanism_signature") != canonical_json_digest({key: payload[key] for key in ("task_type", "code", "config", "evaluation")}):
            raise ControllerError("mechanism_signature must match computation_spec")
        if action.get("input_signature") != canonical_json_digest({key: payload[key] for key in ("data", "split")}):
            raise ControllerError("input_signature must match computation_spec")
    mechanism = _sha(action.get("mechanism_signature"), "action.mechanism_signature")
    input_signature = _sha(action.get("input_signature"), "action.input_signature")
    change_scope = action.get("change_scope")
    if change_scope not in CHANGE_SCOPES:
        raise ControllerError("action.change_scope is invalid")
    if "seed_averaging" in action and not isinstance(action["seed_averaging"], bool):
        raise ControllerError("action.seed_averaging must be boolean")
    if "external_input" in action and not isinstance(action["external_input"], bool):
        raise ControllerError("action.external_input must be boolean")
    history = _history(state)
    if hypothesis in history:
        return {
            "allowed": False,
            "decision": "DUPLICATE_EXPERIMENT",
            "reason": "hypothesis_signature already exists in experiment_history",
        }
    if change_scope == "seed_only" or action.get("seed_averaging") is True:
        if not _control(action):
            return {
                "allowed": False,
                "decision": "SEED_ONLY_NOT_A_HYPOTHESIS",
                "reason": "seed changes require an explicit referenced control and never count as a new hypothesis",
            }
    evaluation_block = _evaluation_decision(action, evaluation_policy)
    if evaluation_block is not None:
        return evaluation_block
    provenance = action.get("run_snapshot")
    if not isinstance(provenance, dict):
        raise ControllerError("action.run_snapshot must freeze data/code/config/split before execution")
    unknown_provenance = sorted(set(provenance).difference({"data", "code", "config", "split"}))
    if unknown_provenance:
        raise ControllerError(f"unknown action.run_snapshot field(s): {unknown_provenance}")
    for name in ("data", "code", "config", "split"):
        saved = _snapshot(root, provenance.get(name), f"action.run_snapshot.{name}")
        if spec is not None:
            content = json.loads((root / saved["path"]).read_text(encoding="utf-8"))
            if content != payload[name]:
                raise ControllerError(f"action.run_snapshot.{name} is not the canonical computation payload")
    if action.get("external_input") is True:
        preflight = action.get("coverage_preflight")
        if not isinstance(preflight, dict) or preflight.get("status") != "PASS":
            raise ControllerError("external_input requires coverage_preflight.status='PASS'")
        unknown_preflight = sorted(set(preflight).difference({"status", "evidence"}))
        if unknown_preflight:
            raise ControllerError(
                f"unknown action.coverage_preflight field(s): {unknown_preflight}"
            )
        _snapshot(root, preflight.get("evidence"), "action.coverage_preflight.evidence")
    family = _family(state, family_name)
    if family is None:
        return {"allowed": True, "decision": "RUN_NEW_FAMILY", "counts_as_new_hypothesis": True}
    _batch(action, family, root, history)
    exhausted = (
        family["status"] == "closed"
        or family["attempts"] >= family["budget"]
        or family["consecutive_robust_failures"] >= family_threshold
    )
    if exhausted:
        reopen = action.get("reopen")
        if not isinstance(reopen, dict):
            return {
                "allowed": False,
                "decision": "FAMILY_CLOSED",
                "reason": "family status, budget, or consecutive robust-gate failures reached its stop rule",
            }
        unknown_reopen = sorted(set(reopen).difference({"reason"}))
        if unknown_reopen:
            raise ControllerError(f"unknown action.reopen field(s): {unknown_reopen}")
        _text(reopen.get("reason"), "action.reopen.reason")
        known_mechanisms = set(family.get("mechanism_signatures", []))
        known_inputs = set(family.get("input_signatures", []))
        if mechanism in known_mechanisms and input_signature in known_inputs:
            return {
                "allowed": False,
                "decision": "FAMILY_REOPEN_REQUIRES_NEW_EVIDENCE",
                "reason": "reopening requires a new mechanism signature or new input signature",
            }
        return {"allowed": True, "decision": "REOPEN_FAMILY", "counts_as_new_hypothesis": True}
    return {
        "allowed": True,
        "decision": "RUN_EXPERIMENT",
        "counts_as_new_hypothesis": change_scope != "seed_only" and action.get("seed_averaging") is not True,
    }


def evaluate_controller_state(
    state: dict[str, Any], root: Path, now: datetime | None = None,
    *, job_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate whether the proposed next action may run."""
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ControllerError("controller state schema_version must be 1")
    unknown = sorted(set(state).difference(TOP_LEVEL_KEYS))
    if unknown:
        raise ControllerError(f"unknown controller field(s): {unknown}")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ControllerError("now must include a timezone")
    fingerprint, snapshots = _state_fingerprint(state, root)
    policy = _policy(state)
    evaluation_policy = _evaluation_policy(state, root)
    issue_policy = _hypothesis_source(state)
    action = state.get("action")
    if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
        raise ControllerError("action.type is invalid")
    action_type = str(action["type"])
    unknown_action = sorted(set(action).difference(ACTION_KEYS[action_type]))
    if unknown_action:
        raise ControllerError(f"unknown action field(s) for {action_type}: {unknown_action}")
    if action_type == "provider_dispatch" and action.get("provider") is None:
        raise ControllerError("provider_dispatch requires action.provider")
    _history(state)
    _family(state, None)
    _provider_block(state, None, current_time, policy["provider_failure_threshold"])
    result_base = {
        "state_fingerprint": fingerprint,
        "state_inputs": snapshots,
        "action_type": action_type,
    }
    if action_type == "wait":
        from .continuation import plan_continuation

        path = root / "automation/continuation.json"
        if path.is_symlink() or path.parent.is_symlink():
            raise ControllerError("continuation policy must not use symlinks")
        if not path.is_file():
            raise ControllerError("waiting requires automation/continuation.json")
        continuation = plan_continuation(
            json.loads(path.read_text(encoding="utf-8")), root, current_time
        )
        return {
            **result_base, "allowed": continuation["can_wait"],
            "decision": continuation["decision"], "continuation": continuation,
        }
    blocked = _blocked_decision(state, action_type, fingerprint, current_time, job_id)
    if blocked is not None:
        return {**result_base, **blocked}
    if action_type == "audit":
        previous = state.get("previous_audit_state_fingerprint")
        if previous is not None and _sha(previous, "previous_audit_state_fingerprint") == fingerprint:
            return {
                **result_base,
                "allowed": False,
                "decision": "UNCHANGED_AUDIT_SKIPPED",
                "reason": "the contract, evidence, ledger, and official-state inputs are unchanged",
            }
    provider = action.get("provider")
    if provider is not None:
        provider = _text(provider, "action.provider")
    provider_block = _provider_block(
        state, provider, current_time, policy["provider_failure_threshold"]
    )
    if provider_block is not None:
        return {**result_base, **provider_block}
    if action_type == "experiment":
        return {
            **result_base,
            **_experiment_decision(
                state,
                action,
                root,
                policy["family_failure_threshold"],
                evaluation_policy,
                issue_policy,
            ),
        }
    decisions = {
        "audit": "RUN_AUDIT",
        "provider_dispatch": "DISPATCH_PROVIDER",
        "resume_check": "NOT_BLOCKED",
    }
    return {**result_base, "allowed": True, "decision": decisions[action_type]}


def load_controller_state(path: Path) -> dict[str, Any]:
    """Load a controller state JSON object."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ControllerError(f"controller state is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ControllerError(f"invalid controller state: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControllerError("controller state must be an object")
    return value
