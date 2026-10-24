"""Generic experiment-record validation shared by ledger and evidence gates."""

from __future__ import annotations

import math
import re
from typing import Any


class RecordError(ValueError):
    """Raised when a generic experiment record is structurally invalid."""


DECISIONS = frozenset({"candidate", "adopted", "rejected", "blocked"})
LIFECYCLE_STATUSES = frozenset({"planned", "running", "succeeded", "failed", "blocked", "aborted"})
CHANGE_SCOPES = frozenset({"hypothesis", "model", "feature", "data", "split", "postprocess", "seed_only", "control", "reproduction"})
SUBMISSION_STATUSES = frozenset({"not_ready", "ready", "submitted", "scored", "failed", "blocked"})
EXTERNAL_SUBMISSION_STATUSES = frozenset(
    {"not_requested", "manual_pending", "submitted", "scored", "failed", "blocked"}
)
EVIDENCE_KINDS = frozenset(
    {"local", "proxy", "official", "user_reported_official", "unverified", "blocked"}
)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _non_empty_string(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RecordError(f"{field} must be a non-empty string")


def _sha256(value: object, field: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise RecordError(f"{field} must be a lowercase SHA-256")


def _validate_artifacts(record: dict[str, Any]) -> None:
    artifacts = record.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise RecordError("artifacts must be a list")
    seen_paths: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise RecordError(f"artifacts[{index}] must be an object")
        _non_empty_string(artifact.get("path"), f"artifacts[{index}].path")
        path = str(artifact["path"])
        if path in seen_paths:
            raise RecordError(f"duplicate artifact path: {path}")
        seen_paths.add(path)
        _sha256(artifact.get("sha256"), f"artifacts[{index}].sha256")


def _validate_checks(record: dict[str, Any]) -> None:
    checks = record.get("checks", {})
    if not isinstance(checks, dict):
        raise RecordError("checks must be an object")
    for name, check in checks.items():
        _non_empty_string(name, "check name")
        if not isinstance(check, dict):
            raise RecordError(f"checks.{name} must be an object")
        status = check.get("status")
        if status not in {"PASS", "FAIL", "WARN", "SKIP"}:
            raise RecordError(f"checks.{name}.status is invalid")
        if status == "PASS":
            _non_empty_string(check.get("command"), f"checks.{name}.command")
            _non_empty_string(check.get("evidence_path"), f"checks.{name}.evidence_path")
            _sha256(check.get("evidence_sha256"), f"checks.{name}.evidence_sha256")


def _validate_metrics(record: dict[str, Any]) -> None:
    metrics = record.get("metrics", [])
    if not isinstance(metrics, list):
        raise RecordError("metrics must be a list")
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict):
            raise RecordError(f"metrics[{index}] must be an object")
        _non_empty_string(metric.get("name"), f"metrics[{index}].name")
        value = metric.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise RecordError(f"metrics[{index}].value must be finite numeric")
        kind = metric.get("evidence_kind", "unverified")
        if kind not in EVIDENCE_KINDS:
            raise RecordError(f"metrics[{index}].evidence_kind is invalid")


def validate_record(record: dict[str, Any]) -> dict[str, Any]:
    """Validate generic record structure without interpreting task semantics.

    Competition contracts decide which checks are required and what a metric
    means. This function only prevents malformed or ambiguous records from
    entering the shared ledger.
    """
    if not isinstance(record, dict):
        raise RecordError("record must be an object")
    key = record.get("record_id") or record.get("experiment_id")
    _non_empty_string(key, "record_id/experiment_id")
    requested = record.get("requested_decision", "candidate")
    if requested not in DECISIONS:
        raise RecordError(f"requested_decision is invalid: {requested!r}")
    if "status" in record and record["status"] not in LIFECYCLE_STATUSES:
        raise RecordError(f"status is invalid: {record['status']!r}")
    if "change_scope" in record and record["change_scope"] not in CHANGE_SCOPES:
        raise RecordError(f"change_scope is invalid: {record['change_scope']!r}")
    if "seed_averaging" in record and not isinstance(record["seed_averaging"], bool):
        raise RecordError("seed_averaging must be boolean")
    if (
        record.get("change_scope") == "seed_only"
        or record.get("seed_averaging") is True
    ) and "base_signature" not in record:
        raise RecordError("seed-only changes require base_signature")
    for field in ("experiment_signature", "base_signature"):
        if field in record:
            _sha256(record[field], field)
    if "seed" in record and (not isinstance(record["seed"], int) or isinstance(record["seed"], bool)):
        raise RecordError("seed must be an integer")
    if "submission_status" in record and record["submission_status"] not in SUBMISSION_STATUSES:
        raise RecordError(f"submission_status is invalid: {record['submission_status']!r}")
    if (
        "external_submission_status" in record
        and record["external_submission_status"] not in EXTERNAL_SUBMISSION_STATUSES
    ):
        raise RecordError(
            f"external_submission_status is invalid: {record['external_submission_status']!r}"
        )
    if record.get("submission_status") in {"ready", "submitted", "scored"}:
        _non_empty_string(record.get("ready_at"), "ready_at")
        submission_check = record.get("checks", {}).get("submission") if isinstance(record.get("checks", {}), dict) else None
        if not isinstance(submission_check, dict) or submission_check.get("status") != "PASS":
            raise RecordError("ready/submitted/scored records require checks.submission.status='PASS'")
        hashes = record.get("submission_artifact_sha256")
        if not isinstance(hashes, list) or not hashes:
            raise RecordError("ready/submitted/scored records require submission_artifact_sha256")
    if record.get("submission_status") in {"submitted", "scored", "failed"}:
        _non_empty_string(record.get("submitted_at"), "submitted_at")
    if record.get("submission_status") in {"submitted", "scored"}:
        _non_empty_string(record.get("platform_submission_id"), "platform_submission_id")
    if "submission_artifact_sha256" in record:
        hashes = record["submission_artifact_sha256"]
        if not isinstance(hashes, list) or not hashes:
            raise RecordError("submission_artifact_sha256 must be a non-empty list")
        for index, digest in enumerate(hashes):
            _sha256(digest, f"submission_artifact_sha256[{index}]")
    if "decision_source" in record:
        _non_empty_string(record["decision_source"], "decision_source")
    if "family" in record:
        _non_empty_string(record["family"], "family")
    if "parent_record_id" in record and record["parent_record_id"] is not None:
        _non_empty_string(record["parent_record_id"], "parent_record_id")
    if "proposal" in record and not isinstance(record["proposal"], (str, dict, list)):
        raise RecordError("proposal must be text or JSON-compatible structure")
    if isinstance(record.get("source_issue"), str) and "computation_spec" not in record:
        # Legacy text remains unverified provenance; Issue-required gates and
        # experiment-run still require a materialized structured snapshot.
        _non_empty_string(record["source_issue"], "source_issue")
    elif "source_issue" in record:
        issue = record["source_issue"]
        if not isinstance(issue, dict) or set(issue) != {"number", "snapshot"}:
            raise RecordError("source_issue needs number and snapshot")
        number = issue["number"]
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise RecordError("source_issue.number must be a positive integer")
        snapshot = issue["snapshot"]
        if not isinstance(snapshot, dict) or set(snapshot) != {"path", "sha256"}:
            raise RecordError("source_issue.snapshot needs path and sha256")
        _non_empty_string(snapshot["path"], "source_issue.snapshot.path")
        _sha256(snapshot["sha256"], "source_issue.snapshot.sha256")
    if "research_manifest_path" in record:
        _non_empty_string(record["research_manifest_path"], "research_manifest_path")
    if "research_manifest_sha256" in record:
        _sha256(record["research_manifest_sha256"], "research_manifest_sha256")
    if "completion_state_path" in record:
        _non_empty_string(record["completion_state_path"], "completion_state_path")
    if "completion_state_sha256" in record:
        _sha256(record["completion_state_sha256"], "completion_state_sha256")
    control = record.get("control", False)
    if not isinstance(control, bool):
        raise RecordError("control must be boolean")
    if control:
        reference = record.get("control_of") or record.get("reference_record_id")
        reason = record.get("control_reason") or record.get("reason")
        _non_empty_string(reference, "control_of/reference_record_id")
        _non_empty_string(reason, "control_reason/reason")
    _validate_artifacts(record)
    _validate_checks(record)
    _validate_metrics(record)
    return record
