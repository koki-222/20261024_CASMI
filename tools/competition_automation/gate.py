"""Deterministic evidence gate for experiment and submission decisions."""

from __future__ import annotations

import math
import json
import re
from pathlib import Path
from typing import Any

from .contract import ContractError, validate_contract
from .fingerprint import fingerprint_path
from .record import RecordError, validate_record
from .research import DEFAULT_REQUIRED_SECTIONS, ResearchError, validate_research_manifest
from .completion import CompletionError, validate_completion_state
from .selection import SelectionError, load_selection_plan


EVIDENCE_KINDS = frozenset(
    {"local", "proxy", "official", "user_reported_official", "unverified", "blocked"}
)
HARD_CHECKS = (
    "contract",
    "leakage",
    "reproducibility",
    "submission",
)


def _check_bool(
    checks: dict[str, Any],
    name: str,
    record: dict[str, Any],
    root: Path | None,
) -> bool:
    """Accept PASS only when the hashed evidence file exists below ``root``."""
    value = checks.get(name)
    if not isinstance(value, dict) or value.get("status") != "PASS":
        return False
    command = value.get("command")
    digest = value.get("evidence_sha256")
    evidence_path = value.get("evidence_path")
    if (
        not isinstance(command, str)
        or not command.strip()
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(evidence_path, str)
        or not evidence_path.strip()
        or root is None
    ):
        return False
    artifacts = record.get("artifacts", [])
    listed = any(
        isinstance(artifact, dict)
        and artifact.get("path") == evidence_path
        and artifact.get("sha256") == digest
        for artifact in artifacts
    )
    if not listed:
        return False
    root_resolved = root.expanduser().resolve()
    candidate = (root_resolved / evidence_path).resolve()
    if not candidate.is_file() or (root_resolved / evidence_path).is_symlink():
        return False
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return False
    return fingerprint_path(candidate)["sha256"] == digest


def _valid_official_source(
    item: dict[str, Any], record: dict[str, Any], root: Path | None
) -> bool:
    """Require a hashed raw platform artifact before accepting official evidence."""
    source = item.get("source")
    if not isinstance(source, dict):
        return False
    required = ("platform", "submission_id", "observed_at", "verification")
    if any(not isinstance(source.get(name), str) or not source[name].strip() for name in required):
        return False
    if source["verification"] not in {"official_page_snapshot", "platform_api_response"}:
        return False
    artifact_path = source.get("raw_artifact_path")
    artifact_digest = source.get("raw_artifact_sha256")
    if not isinstance(artifact_path, str) or not artifact_path.strip():
        return False
    if not isinstance(artifact_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_digest):
        return False
    artifacts = record.get("artifacts", [])
    listed = any(
        isinstance(artifact, dict)
        and artifact.get("path") == artifact_path
        and artifact.get("sha256") == artifact_digest
        for artifact in artifacts
    )
    if not listed or root is None:
        return False
    root_resolved = root.expanduser().resolve()
    candidate = (root_resolved / artifact_path).resolve()
    if not candidate.is_file() or (root_resolved / artifact_path).is_symlink():
        return False
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return False
    return fingerprint_path(candidate)["sha256"] == artifact_digest


def _metric_evidence(
    record: dict[str, Any], contract: dict[str, Any], root: Path | None
) -> tuple[str, list[str], list[str]]:
    evaluation = contract["evaluation"]
    metric_name = evaluation["metric"]
    metrics = record.get("metrics", [])
    if not isinstance(metrics, list):
        return "unverified", ["metrics must be a list"], []
    matching = [item for item in metrics if isinstance(item, dict) and item.get("name") == metric_name]
    if not matching:
        return "unverified", [f"objective metric {metric_name!r} is missing"], []
    kinds: list[str] = []
    reasons: list[str] = []
    warnings: list[str] = []
    for item in matching:
        kind = item.get("evidence_kind", "unverified")
        if kind not in EVIDENCE_KINDS:
            reasons.append(f"unknown evidence_kind: {kind!r}")
            continue
        effective_kind = kind
        if kind == "official" and not _valid_official_source(item, record, root):
            effective_kind = "user_reported_official"
            warnings.append(
                f"metric {metric_name!r}: official self-report downgraded to user_reported_official"
            )
        kinds.append(effective_kind)
        value = item.get("value")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            reasons.append(f"metric {metric_name!r} is not finite")
        else:
            domain = contract["evaluation"].get("metric_domain") or {}
            if "minimum" in domain and float(value) < float(domain["minimum"]):
                reasons.append(f"metric {metric_name!r} is below its declared domain")
            if "maximum" in domain and float(value) > float(domain["maximum"]):
                reasons.append(f"metric {metric_name!r} is above its declared domain")
    if not kinds:
        return "unverified", reasons, warnings
    rank = {
        "official": 5,
        "user_reported_official": 4,
        "local": 2,
        "proxy": 1,
        "unverified": 0,
        "blocked": -1,
    }
    return max(kinds, key=lambda value: rank[value]), reasons, warnings


def _provenance_reasons(record: dict[str, Any]) -> list[str]:
    """Require immutable data/code/config/split references for a decision."""
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        return ["provenance must contain data, code, config, and split"]
    reasons: list[str] = []
    for name in ("data", "code", "config", "split"):
        value = provenance.get(name)
        if isinstance(value, str) and value.strip():
            continue
        if isinstance(value, dict) and isinstance(value.get("sha256"), str):
            digest = value["sha256"]
            if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
                continue
        reasons.append(f"provenance.{name} must be a non-empty reference or SHA-256")
    return reasons


def _research_reasons(record: dict[str, Any], contract: dict[str, Any], root: Path | None) -> list[str]:
    """Require a re-hashed pre-analysis research manifest when the profile says so."""
    if not contract.get("policy", {}).get("research_preflight_required", True):
        return []
    manifest_path = record.get("research_manifest_path")
    manifest_digest = record.get("research_manifest_sha256")
    if not isinstance(manifest_path, str) or not manifest_path.strip():
        return ["research preflight is required but research_manifest_path is missing"]
    if not isinstance(manifest_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_digest):
        return ["research preflight is required but research_manifest_sha256 is invalid"]
    if root is None:
        return ["research preflight requires --root for manifest re-hash"]
    artifacts = record.get("artifacts", [])
    if not any(
        isinstance(artifact, dict)
        and artifact.get("path") == manifest_path
        and artifact.get("sha256") == manifest_digest
        for artifact in artifacts
    ):
        return ["research manifest must be listed in record.artifacts"]
    root_resolved = root.expanduser().resolve()
    manifest = (root_resolved / manifest_path).resolve()
    try:
        manifest.relative_to(root_resolved)
    except ValueError:
        return ["research manifest escapes root"]
    if not manifest.is_file() or (root_resolved / manifest_path).is_symlink():
        return ["research manifest file is missing or symlinked"]
    if fingerprint_path(manifest)["sha256"] != manifest_digest:
        return ["research manifest hash mismatch"]
    profile = contract.get("research", {})
    try:
        validate_research_manifest(
            manifest,
            root,
            required_sections=profile.get("required_sections") or DEFAULT_REQUIRED_SECTIONS,
            minimum_sources=int(profile.get("minimum_sources", len(DEFAULT_REQUIRED_SECTIONS))),
        )
    except ResearchError as exc:
        return [f"research preflight failed: {exc}"]
    return []


def _issue_reasons(record: dict[str, Any], contract: dict[str, Any], root: Path | None) -> list[str]:
    """Bind an experiment decision to its frozen user Issue source."""
    if not contract.get("policy", {}).get("issue_evidence_required", False):
        return []
    issue = record.get("source_issue")
    if not isinstance(issue, dict):
        return ["user Issue source_issue is required"]
    snapshot = issue["snapshot"]
    path_value = snapshot["path"]
    digest = snapshot["sha256"]
    if not any(
        isinstance(artifact, dict)
        and artifact.get("path") == path_value
        and artifact.get("sha256") == digest
        for artifact in record.get("artifacts", [])
    ):
        return ["user Issue snapshot must be listed in record.artifacts"]
    if root is None:
        return ["user Issue snapshot requires --root for re-hash"]
    root_resolved = root.expanduser().resolve()
    raw = root_resolved / path_value
    if raw.is_symlink():
        return ["user Issue snapshot must not be a symlink"]
    saved_path = raw.resolve()
    try:
        saved_path.relative_to(root_resolved)
    except ValueError:
        return ["user Issue snapshot escapes root"]
    if not saved_path.is_file() or fingerprint_path(saved_path)["sha256"] != digest:
        return ["user Issue snapshot is missing or its hash does not match"]
    try:
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["user Issue snapshot is not valid JSON"]
    if not isinstance(saved, dict) or saved.get("number") != issue["number"]:
        return ["user Issue snapshot number does not match record"]
    return []


def _completion_reasons(record: dict[str, Any], contract: dict[str, Any], root: Path | None) -> list[str]:
    """Prevent an unachieved or LLM-declared terminal task from passing."""
    if not contract.get("policy", {}).get("completion_proof_required", True):
        return []
    state_path = record.get("completion_state_path")
    state_digest = record.get("completion_state_sha256")
    if not isinstance(state_path, str) or not state_path.strip():
        return ["completion proof is required but completion_state_path is missing"]
    if not isinstance(state_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", state_digest):
        return ["completion proof is required but completion_state_sha256 is invalid"]
    if root is None:
        return ["completion proof requires --root for state re-hash"]
    artifacts = record.get("artifacts", [])
    if not any(
        isinstance(artifact, dict)
        and artifact.get("path") == state_path
        and artifact.get("sha256") == state_digest
        for artifact in artifacts
    ):
        return ["completion state must be listed in record.artifacts"]
    root_resolved = root.expanduser().resolve()
    state = (root_resolved / state_path).resolve()
    try:
        state.relative_to(root_resolved)
    except ValueError:
        return ["completion state escapes root"]
    if not state.is_file() or (root_resolved / state_path).is_symlink():
        return ["completion state file is missing or symlinked"]
    if fingerprint_path(state)["sha256"] != state_digest:
        return ["completion state hash mismatch"]
    try:
        validate_completion_state(state, root)
    except CompletionError as exc:
        return [f"completion proof failed: {exc}"]
    return []


def _selection_reasons(
    record: dict[str, Any], contract: dict[str, Any], root: Path | None
) -> list[str]:
    """Require adoption to match a hash-bound, eligible selection-plan entry."""
    if not contract.get("policy", {}).get("selection_proof_required_for_adoption", False):
        return []
    path_value = record.get("selection_plan_path")
    digest = record.get("selection_plan_sha256")
    if not isinstance(path_value, str) or not path_value.strip():
        return ["adoption requires selection_plan_path"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return ["adoption requires a valid selection_plan_sha256"]
    if root is None:
        return ["selection proof requires --root"]
    if not any(
        isinstance(artifact, dict)
        and artifact.get("path") == path_value
        and artifact.get("sha256") == digest
        for artifact in record.get("artifacts", [])
    ):
        return ["selection plan must be listed in record.artifacts"]
    root_resolved = root.expanduser().resolve()
    raw = root_resolved / path_value
    if raw.is_symlink():
        return ["selection plan must not be a symlink"]
    plan_path = raw.resolve()
    try:
        plan_path.relative_to(root_resolved)
    except ValueError:
        return ["selection plan escapes root"]
    if not plan_path.is_file() or fingerprint_path(plan_path)["sha256"] != digest:
        return ["selection plan is missing or its hash does not match"]
    try:
        plan = load_selection_plan(plan_path, root=root)
    except SelectionError as exc:
        return [f"selection proof failed: {exc}"]
    record_id = record.get("record_id") or record.get("experiment_id")
    if record_id not in plan.get("selected", []):
        return ["record is not selected by the frozen selection plan"]
    candidate = next(
        (value for value in plan.get("candidates", []) if value.get("record_id") == record_id),
        None,
    )
    if not isinstance(candidate, dict):
        return ["selected record is missing from selection-plan candidates"]
    artifacts = record.get("submission_artifact_sha256", [])
    if candidate.get("artifact_sha256") not in artifacts:
        return ["selected candidate artifact does not match the submitted artifact"]
    if record.get("family") != candidate.get("family"):
        return ["record family does not match the selected candidate"]
    return []


def evaluate_gate(
    record: dict[str, Any], contract: dict[str, Any], root: Path | None = None
) -> dict[str, Any]:
    """Return a machine-checkable decision; never infer success from prose.

    ``adopted`` requires an official score and all hard checks.  A local or
    proxy result can only become ``candidate``; it cannot be reported as an
    external improvement.
    """
    reasons: list[str] = []
    try:
        validate_contract(contract)
    except ContractError as exc:
        return {"decision": "blocked", "reasons": [f"invalid contract: {exc}"], "checks": {}}
    try:
        validate_record(record)
    except RecordError as exc:
        return {"decision": "blocked", "reasons": [f"invalid record: {exc}"], "checks": {}}
    checks = record.get("checks", {})
    if not isinstance(checks, dict):
        return {"decision": "blocked", "reasons": ["checks must be an object"], "checks": {}}
    required_checks = contract.get("policy", {}).get("required_checks", HARD_CHECKS)
    check_result = {
        name: _check_bool(checks, name, record, root)
        for name in required_checks
    }
    reasons.extend(name + " check failed" for name, passed in check_result.items() if not passed)
    evidence_kind, evidence_reasons, evidence_warnings = _metric_evidence(record, contract, root)
    reasons.extend(evidence_reasons)
    reasons.extend(_provenance_reasons(record))
    reasons.extend(_research_reasons(record, contract, root))
    reasons.extend(_issue_reasons(record, contract, root))
    reasons.extend(_completion_reasons(record, contract, root))
    requested = record.get("requested_decision", "candidate")
    if requested not in {"candidate", "adopted", "rejected"}:
        reasons.append(f"unknown requested_decision: {requested!r}")
        requested = "candidate"
    if requested == "adopted":
        reasons.extend(_selection_reasons(record, contract, root))
    lifecycle = record.get("status")
    if lifecycle in {"planned", "running", "failed", "blocked", "aborted"} and requested in {
        "candidate",
        "adopted",
    }:
        reasons.append(f"lifecycle status {lifecycle!r} is not a completed experiment")
    decision_source = record.get("decision_source")
    llm_requested = isinstance(decision_source, str) and "llm" in decision_source.lower()
    if llm_requested:
        # LLM output may propose a candidate, but it can never request adoption.
        # Keep the deterministic checks usable for an LLM-proposed candidate.
        requested = "candidate"
    policy = contract.get("policy", {})
    seed_only_forbidden = policy.get("seed_only_adoption_forbidden", True)
    is_seed_only = record.get("change_scope") == "seed_only" or record.get("seed_averaging") is True
    if is_seed_only and seed_only_forbidden and record.get("control") is not True and requested != "rejected":
        reasons.append(
            "seed-only/seed-averaging change requires an explicit control record and cannot replace a new hypothesis"
        )
    hard_pass = not reasons and all(check_result.values())
    if not hard_pass:
        decision = "blocked"
    elif requested == "rejected":
        decision = "rejected"
    elif requested == "adopted" and evidence_kind == "official":
        decision = "adopted"
    elif requested == "adopted":
        reasons.append("adoption requires evidence_kind='official'")
        decision = "candidate" if evidence_kind in {"local", "proxy", "user_reported_official"} else "blocked"
    elif evidence_kind == "blocked":
        decision = "blocked"
    else:
        decision = "candidate"
    return {
        "decision": decision,
        "evidence_kind": evidence_kind,
        "checks": check_result,
        "reasons": reasons,
        "warnings": evidence_warnings,
        "llm_decision_ignored": llm_requested,
    }
