"""Dependency-aware validation planning and receipt checks.

This module plans validation work; it deliberately does not execute arbitrary
commands.  A receipt is evidence produced by the runner and is accepted only
when it is bound to the exact check specification and to current dependency
content.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fingerprint import canonical_json_digest, sha256_file


class ValidationError(ValueError):
    """Raised when a validation plan or receipt violates its schema."""


_PLAN_FIELDS = {
    "schema_version",
    "checks",
    "receipts",
    "canary",
    "runtime",
    "metadata",
    "full_run_policy",
}
_CHECK_FIELDS = {
    "id",
    "name",
    "kind",
    "command",
    "dependencies",
    "scope",
    "config",
    "changed_branches",
    "depends_on",
    "required",
    "fanout",
    "run_mode",
    "resource",
    "full_run_reason",
    "semantic",
}
_RECEIPT_FIELDS = {
    "check_id",
    "status",
    "command",
    "dependencies",
    "scope",
    "config",
    "process",
    "report",
    "artifacts",
    "branch_coverage",
    "canary",
    "runtime_class",
    "official",
    "started_at",
    "finished_at",
    "executable_fingerprint",
    "prediction_fingerprint",
    "environment",
    "reason",
    "binding",
    "attempt_id",
    "dependency_changed",
}
_DEPENDENCY_FIELDS = {"path", "sha256", "kind", "role"}
_ARTIFACT_FIELDS = {"path", "sha256"}
_CANARY_FIELDS = {
    "required",
    "runtime_class",
    "environment",
    "attempts",
    "official_receipt",
}
_CANARY_RECEIPT_FIELDS = {
    "status",
    "submission_id",
    "runtime_class",
    "environment",
    "source",
    "raw_snapshot",
    "observation",
}
_PROCESS_FIELDS = {"pid", "returncode", "started_at", "finished_at", "evidence"}
_TERMINAL_SUCCESS = {"PASS", "SUCCESS", "COMPLETE"}
_WAITING_STATUSES = {"PENDING", "SCORING", "Scoring", "pending", "scoring"}
_PATH_KEYS = {
    "path",
    "paths",
    "name",
    "names",
    "file",
    "files",
    "directory",
    "directories",
}
_TIME_KEYS = {
    "timestamp",
    "timestamps",
    "created_at",
    "started_at",
    "finished_at",
    "updated_at",
}


def _ensure_finite(value: Any, location: str = "plan") -> None:
    """Reject non-JSON values and non-finite floating point values."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(f"{location} contains a non-finite number")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{location} has a non-string key")
            _ensure_finite(item, f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _ensure_finite(item, f"{location}[{index}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValidationError(
            f"{location} contains unsupported value {type(value).__name__}"
        )


def _strict_fields(value: dict[str, Any], allowed: set[str], location: str) -> None:
    """Reject unexpected schema fields."""
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(f"{location} has unknown fields: {', '.join(unknown)}")


def _as_sha(value: Any, location: str) -> str:
    """Require a lowercase content digest."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValidationError(f"{location}.sha256 must be a lowercase SHA-256 digest")
    return value


def _resolve_dependency(path_value: str, root: Path) -> Path:
    """Resolve a dependency beneath the evidence root."""
    path = Path(path_value)
    resolved = (path if path.is_absolute() else root / path).expanduser().resolve()
    root_resolved = root.expanduser().resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValidationError(f"dependency path escapes root: {path_value}") from exc
    return resolved


def _validate_dependencies(
    dependencies: Any, root: Path, location: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rehash inputs and identify changed or missing dependencies."""
    if not isinstance(dependencies, list):
        raise ValidationError(f"{location} must be a list")
    normalized: list[dict[str, Any]] = []
    stale: list[str] = []
    for index, dependency in enumerate(dependencies):
        if not isinstance(dependency, dict):
            raise ValidationError(f"{location}[{index}] must be an object")
        _strict_fields(dependency, _DEPENDENCY_FIELDS, f"{location}[{index}]")
        if not isinstance(dependency.get("path"), str):
            raise ValidationError(f"{location}[{index}].path is required")
        expected = _as_sha(dependency.get("sha256"), f"{location}[{index}]")
        path = _resolve_dependency(dependency["path"], root)
        actual = sha256_file(path) if path.is_file() else None
        if actual != expected:
            stale.append(dependency["path"])
        normalized.append(
            {
                **dependency,
                "path": dependency["path"],
                "sha256": expected,
                "current_sha256": actual,
            }
        )
    return normalized, stale


def _semantic_clean(value: Any, key: str | None = None) -> Any:
    """Normalize explicitly operational metadata while preserving semantics."""
    if isinstance(value, dict):
        if key not in {"operational_metadata", "snapshot", "snapshots"}:
            return {name: _semantic_clean(item, name) for name, item in value.items()}
        return {
            name: _semantic_clean(item, name)
            for name, item in value.items()
            if name.lower() not in _PATH_KEYS
            and name.lower() not in _TIME_KEYS
            and not name.lower().endswith("_path")
            and not name.lower().endswith("_at")
        }
    if isinstance(value, list):
        return [_semantic_clean(item, key) for item in value]
    if isinstance(value, tuple):
        return [_semantic_clean(item, key) for item in value]
    return value


def semantic_check_signature(
    check: dict[str, Any], dependencies: list[dict[str, Any]] | None = None
) -> str:
    """Return a check signature independent of names, paths, and timestamps."""
    dependency_values = (
        dependencies if dependencies is not None else check.get("dependencies", [])
    )
    semantic = {
        "kind": check.get("kind"),
        "scope": _semantic_clean(check.get("scope", {})),
        "config": _semantic_clean(check.get("config", {})),
        "semantic": _semantic_clean(check.get("semantic", {})),
        "changed_branches": sorted(check.get("changed_branches", [])),
        "dependencies": sorted(
            (
                {
                    "sha256": item.get("sha256"),
                    "kind": item.get("kind"),
                    "role": item.get("role"),
                }
                for item in dependency_values
            ),
            key=lambda item: (
                str(item.get("role")),
                str(item.get("kind")),
                str(item.get("sha256")),
            ),
        ),
    }
    return canonical_json_digest(semantic)


def _receipt_binding(check: dict[str, Any], dependencies: list[dict[str, Any]]) -> str:
    """Bind execution evidence to exact arguments, inputs and evaluation scope."""
    return canonical_json_digest(
        {
            "command": check.get("command"),
            "dependencies": dependencies,
            "scope": check.get("scope", {}),
            "config": check.get("config", {}),
        }
    )


def _artifact_ok(artifact: Any, root: Path) -> bool:
    """Verify one saved artifact without treating a missing file as evidence."""
    if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
        return False
    try:
        path = _resolve_dependency(artifact["path"], root)
        expected = _as_sha(artifact.get("sha256"), "artifact")
    except (ValidationError, TypeError):
        return False
    return path.is_file() and sha256_file(path) == expected


def _read_json_artifact(artifact: Any, root: Path) -> dict[str, Any] | None:
    """Read a root-bound JSON artifact only after verifying its declared hash."""
    if not _artifact_ok(artifact, root):
        return None
    try:
        path = _resolve_dependency(str(artifact["path"]), root)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _report_or_artifact_ok(receipt: dict[str, Any], root: Path) -> bool:
    """Require a current, hashed PASS report and every declared artifact."""
    report = receipt.get("report")
    if report is None or not _artifact_ok(report, root):
        return False
    report_payload = _read_json_artifact(report, root)
    if report_payload is None or report_payload.get("status") != "PASS":
        return False
    artifacts = receipt.get("artifacts", [])
    if not isinstance(artifacts, list) or any(
        not _artifact_ok(item, root) for item in artifacts
    ):
        return False
    return report is not None or bool(artifacts)


def _process_ok(check: dict[str, Any], receipt: dict[str, Any], root: Path) -> bool:
    """Verify process evidence from a hashed JSON record, not a boolean claim."""
    process = receipt.get("process")
    if not isinstance(process, dict):
        return False
    _strict_fields(process, _PROCESS_FIELDS, "receipt.process")
    if (
        not isinstance(process.get("pid"), int)
        or isinstance(process.get("pid"), bool)
        or process["pid"] <= 0
        or process.get("returncode") != 0
        or not isinstance(process.get("started_at"), str)
        or not isinstance(process.get("finished_at"), str)
    ):
        return False
    evidence = _read_json_artifact(process.get("evidence"), root)
    if evidence is None:
        return False
    return (
        evidence.get("command") == check.get("command")
        and evidence.get("pid") == process.get("pid")
        and evidence.get("returncode") == 0
        and evidence.get("started_at") == process.get("started_at")
        and evidence.get("finished_at") == process.get("finished_at")
    )


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    """Write JSON through a same-directory temporary file without overwriting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ValidationError(f"refusing to overwrite evidence: {path}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def run_check(
    check: dict[str, Any],
    root: Path,
    *,
    report_path: Path | None = None,
    evidence_path: Path | None = None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Run one declared check and materialize an evidence-bound receipt.

    The runner records the real child PID, command, return code, and timestamps.
    A successful receipt still requires the command to produce a JSON report;
    this helper never turns a textual success message into validation evidence.
    """
    command = check.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise ValidationError("check.command must be a non-empty string list")
    root = root.expanduser().resolve()
    _ensure_finite(check, "check")
    validated = _validate_check(check, 0, root)
    if validated["stale_dependencies"]:
        raise ValidationError("dependencies changed before execution")
    report = (
        report_path or Path("validation/reports") / f"{check.get('id', 'check')}.json"
    )
    evidence = (
        evidence_path or Path("validation/process") / f"{check.get('id', 'check')}.json"
    )
    report = report if report.is_absolute() else root / report
    evidence = evidence if evidence.is_absolute() else root / evidence
    try:
        report = report.resolve()
        report.relative_to(root)
        evidence = evidence.resolve()
        evidence.relative_to(root)
    except ValueError as exc:
        raise ValidationError("report/evidence path escapes root") from exc
    if report == evidence:
        raise ValidationError("report and process evidence must use different paths")
    if report.exists() or evidence.exists():
        raise ValidationError(
            "refusing to overwrite pre-existing report or process evidence"
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValidationError("timeout_seconds must be a positive finite number")
    report.parent.mkdir(parents=True, exist_ok=True)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    process = subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    finished = datetime.now(timezone.utc).isoformat()
    _, stale_after = _validate_dependencies(
        check.get("dependencies", []), root, "check.dependencies"
    )
    evidence_payload = {
        "command": command,
        "pid": process.pid,
        "returncode": process.returncode,
        "started_at": started,
        "finished_at": finished,
        "stdout_sha256": canonical_json_digest(stdout),
        "stderr_sha256": canonical_json_digest(stderr),
        "timed_out": timed_out,
        "dependency_changed": bool(stale_after),
    }
    _write_json_exclusive(evidence, evidence_payload)
    if process.returncode == 0 and not report.exists():
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            _write_json_exclusive(report, parsed)
    report_payload = (
        _read_json_artifact(
            {"path": str(report.relative_to(root)), "sha256": sha256_file(report)}, root
        )
        if report.is_file()
        else None
    )
    receipt: dict[str, Any] = {
        "check_id": check.get("id"),
        "status": "PASS"
        if process.returncode == 0
        and not timed_out
        and not stale_after
        and report_payload is not None
        and report_payload.get("status") == "PASS"
        else "FAILED",
        "command": command,
        "dependencies": check.get("dependencies", []),
        "scope": check.get("scope", {}),
        "config": check.get("config", {}),
        "process": {
            "pid": process.pid,
            "returncode": process.returncode,
            "started_at": started,
            "finished_at": finished,
            "evidence": {
                "path": str(evidence.relative_to(root)),
                "sha256": sha256_file(evidence),
            },
        },
        "report": {"path": str(report.relative_to(root)), "sha256": sha256_file(report)}
        if report.is_file()
        else None,
        "artifacts": [],
        "dependency_changed": bool(stale_after),
    }
    return receipt


def _branch_status(
    check: dict[str, Any], receipt: dict[str, Any] | None, root: Path
) -> tuple[str, list[str]]:
    """Read branch counters from the hash-verified JSON report."""
    required = check.get("changed_branches", [])
    if not required:
        return "PASS", []
    if receipt is None:
        return "NEEDS_TARGETED_INPUT", list(required)
    report = _read_json_artifact(receipt.get("report"), root)
    coverage = report.get("branch_coverage") if report is not None else None
    if not isinstance(coverage, dict):
        return "NEEDS_TARGETED_INPUT", list(required)
    for branch, count in coverage.items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValidationError(
                f"report.branch_coverage.{branch} must be a non-negative integer"
            )
    missing = [branch for branch in required if coverage.get(branch, 0) <= 0]
    return ("PASS", []) if not missing else ("NEEDS_TARGETED_INPUT", missing)


def _run_scope(check: dict[str, Any], reason: str) -> str:
    """Choose the smallest rerun scope supported by the declared change."""
    declared = str(check.get("full_run_reason", "")).lower()
    if any(
        token in declared
        for token in ("runtime", "model", "input", "schema", "new_path")
    ):
        return "full"
    if "coefficient" in declared or "threshold" in declared or "seed" in declared:
        return "targeted"
    if check.get("run_mode") == "full":
        return "full"
    if reason == "dependency changed":
        # An unclassified input change is unsafe to reuse, but does not grant a
        # blanket full replay to unrelated checks.
        return "targeted"
    return "targeted"


def _official_canary_ok(
    canary: dict[str, Any], receipt: dict[str, Any], root: Path
) -> bool:
    """Verify two hashed official observations before opening remote fanout."""
    _strict_fields(receipt, _CANARY_RECEIPT_FIELDS, "canary.official_receipt")
    if (
        receipt.get("status") != "COMPLETE"
        or receipt.get("source") not in {"official", "remote"}
        or not isinstance(receipt.get("submission_id"), str)
        or not receipt["submission_id"]
        or not isinstance(canary.get("runtime_class"), str)
        or not canary.get("runtime_class")
        or canary.get("environment") is None
        or receipt.get("runtime_class") != canary.get("runtime_class")
        or receipt.get("environment") != canary.get("environment")
    ):
        return False
    raw = _read_json_artifact(receipt.get("raw_snapshot"), root)
    observation = _read_json_artifact(receipt.get("observation"), root)
    if raw is None or observation is None:
        return False
    if receipt["raw_snapshot"].get("path") == receipt["observation"].get("path"):
        return False
    expected = {
        "status": "COMPLETE",
        "submission_id": receipt["submission_id"],
        "runtime_class": canary["runtime_class"],
        "environment": canary["environment"],
    }
    return all(
        raw.get(key) == value and observation.get(key) == value
        for key, value in expected.items()
    )


def _check_canary(plan: dict[str, Any], root: Path) -> dict[str, Any]:
    """Allow fanout only after a matching completed remote observation."""
    canary = plan.get("canary") or {}
    if not isinstance(canary, dict):
        raise ValidationError("canary must be an object")
    _strict_fields(canary, _CANARY_FIELDS, "canary")
    required_value = canary.get("required", False)
    if not isinstance(required_value, bool):
        raise ValidationError("canary.required must be boolean")
    required = required_value
    if not required:
        return {"status": "OPEN", "fanout_allowed": True, "reason": None}
    attempts = canary.get("attempts", 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ValidationError("canary.attempts must be a non-negative integer")
    receipt = canary.get("official_receipt")
    if isinstance(receipt, dict) and receipt.get("status") in _WAITING_STATUSES:
        return {
            "status": "PENDING_CANARY",
            "fanout_allowed": False,
            "reason": "official receipt is pending",
        }
    if isinstance(receipt, dict) and receipt.get("status") in _TERMINAL_SUCCESS:
        if _official_canary_ok(canary, receipt, root):
            return {"status": "OPEN", "fanout_allowed": True, "reason": None}
        return {
            "status": "CANARY_REQUIRED",
            "fanout_allowed": False,
            "reason": "official COMPLETE receipt is missing or mismatched",
        }
    if attempts >= 1:
        return {
            "status": "CANARY_FAILED",
            "fanout_allowed": False,
            "reason": "canary allowance exhausted",
        }
    return {
        "status": "CANARY_REQUIRED",
        "fanout_allowed": False,
        "reason": "one remote canary is required before fanout",
    }


def _validate_check(check: Any, index: int, root: Path) -> dict[str, Any]:
    """Validate and snapshot one check declaration."""
    if not isinstance(check, dict):
        raise ValidationError(f"checks[{index}] must be an object")
    _strict_fields(check, _CHECK_FIELDS, f"checks[{index}]")
    if not isinstance(check.get("id"), str) or not check["id"]:
        raise ValidationError(f"checks[{index}].id is required")
    command = check.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise ValidationError(
            f"checks[{index}].command must be a non-empty string list"
        )
    dependencies, stale = _validate_dependencies(
        check.get("dependencies", []), root, f"checks[{index}].dependencies"
    )
    changed = check.get("changed_branches", [])
    if not isinstance(changed, list) or not all(
        isinstance(item, str) and item for item in changed
    ):
        raise ValidationError(
            f"checks[{index}].changed_branches must be a list of strings"
        )
    depends_on = check.get("depends_on", [])
    if not isinstance(depends_on, list) or not all(
        isinstance(item, str) for item in depends_on
    ):
        raise ValidationError(f"checks[{index}].depends_on must be a list of strings")
    for boolean_field in ("required", "fanout"):
        if boolean_field in check and not isinstance(check[boolean_field], bool):
            raise ValidationError(f"checks[{index}].{boolean_field} must be boolean")
    return {
        **check,
        "dependencies": dependencies,
        "stale_dependencies": stale,
        "semantic_signature": semantic_check_signature(check, dependencies),
        "binding": _receipt_binding(check, dependencies),
    }


def _validate_receipt_shape(receipt: Any, index: int) -> None:
    """Validate the structural portion of one runner receipt."""
    if not isinstance(receipt, dict):
        raise ValidationError(f"receipts[{index}] must be an object")
    _strict_fields(receipt, _RECEIPT_FIELDS, f"receipts[{index}]")
    if not isinstance(receipt.get("check_id"), str) or not isinstance(
        receipt.get("status"), str
    ):
        raise ValidationError(f"receipts[{index}] requires check_id and status")
    if receipt.get("report") is not None:
        if not isinstance(receipt["report"], dict):
            raise ValidationError(f"receipts[{index}].report must be an object")
        _strict_fields(receipt["report"], _ARTIFACT_FIELDS, f"receipts[{index}].report")
    artifacts = receipt.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ValidationError(f"receipts[{index}].artifacts must be a list")
    for artifact_index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise ValidationError(
                f"receipts[{index}].artifacts[{artifact_index}] must be an object"
            )
        _strict_fields(
            artifact, _ARTIFACT_FIELDS, f"receipts[{index}].artifacts[{artifact_index}]"
        )


def _assert_acyclic(checks: list[dict[str, Any]]) -> None:
    """Reject dependency cycles before producing a runnable queue."""
    graph = {check["id"]: check.get("depends_on", []) for check in checks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        """Check one dependency subtree for cycles."""
        if node in visiting:
            raise ValidationError(f"check dependency cycle includes {node}")
        if node in visited:
            return
        visiting.add(node)
        for parent in graph[node]:
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def _receipt_matches(
    check: dict[str, Any], receipt: dict[str, Any], root: Path
) -> tuple[bool, str]:
    """Check exact execution binding and evidence freshness for cache reuse."""
    if receipt.get("dependency_changed") is True:
        return False, "dependency changed during execution"
    if receipt.get("command") != check.get("command"):
        return False, "command changed"
    if receipt.get("scope", {}) != check.get("scope", {}):
        return False, "scope changed"
    if receipt.get("config", {}) != check.get("config", {}):
        return False, "config changed"
    declared, stale = _validate_dependencies(
        receipt.get("dependencies", []), root, "receipt.dependencies"
    )
    if stale or declared != check.get("dependencies"):
        return False, "dependency snapshot changed"
    if receipt.get("binding") not in (None, check.get("binding")):
        return False, "binding mismatch"
    if not _process_ok(check, receipt, root):
        return False, "missing successful process evidence"
    if not _report_or_artifact_ok(receipt, root):
        return False, "missing report or artifact evidence"
    return True, "compatible PASS receipt"


def plan_validation(plan: dict[str, Any], root: Path) -> dict[str, Any]:
    """Plan reusable validation work from dependency snapshots and receipts.

    No command is executed.  The result explicitly distinguishes cached PASS,
    required work, targeted branch input, and a remote canary gate.
    """
    if not isinstance(plan, dict):
        raise ValidationError("plan must be an object")
    _ensure_finite(plan)
    _strict_fields(plan, _PLAN_FIELDS, "plan")
    if plan.get("schema_version", 1) != 1:
        raise ValidationError("schema_version must be 1")
    if not isinstance(root, Path):
        raise ValidationError("root must be a pathlib.Path")
    checks_value = plan.get("checks")
    if not isinstance(checks_value, list) or not checks_value:
        raise ValidationError("checks must be a non-empty list")
    checks = [
        _validate_check(check, index, root) for index, check in enumerate(checks_value)
    ]
    check_map = {check["id"]: check for check in checks}
    if len(check_map) != len(checks):
        raise ValidationError("check ids must be unique")
    for check in checks:
        unknown = sorted(set(check.get("depends_on", [])) - set(check_map))
        if unknown:
            raise ValidationError(
                f"{check['id']} depends on unknown checks: {', '.join(unknown)}"
            )
    _assert_acyclic(checks)
    receipts_value = plan.get("receipts", [])
    if not isinstance(receipts_value, list):
        raise ValidationError("receipts must be a list")
    for index, receipt in enumerate(receipts_value):
        _validate_receipt_shape(receipt, index)
    receipt_ids = [receipt["check_id"] for receipt in receipts_value]
    if len(set(receipt_ids)) != len(receipt_ids):
        raise ValidationError("receipt check_id values must be unique")
    receipts = {receipt["check_id"]: receipt for receipt in receipts_value}
    canary = _check_canary(plan, root)
    decisions: list[dict[str, Any]] = []
    invalidated: set[str] = set()
    for check in checks:
        receipt = receipts.get(check["id"])
        if check["stale_dependencies"]:
            invalidated.add(check["id"])
            reason = "dependency changed"
            decisions.append(
                {
                    "check_id": check["id"],
                    "status": "RUN_REQUIRED",
                    "reason": reason,
                    "run_scope": _run_scope(check, reason),
                    "stale_dependencies": check["stale_dependencies"],
                }
            )
            continue
        branch_status, missing = _branch_status(check, receipt, root)
        if branch_status != "PASS":
            invalidated.add(check["id"])
            decisions.append(
                {
                    "check_id": check["id"],
                    "status": branch_status,
                    "reason": "changed branch has no observed coverage",
                    "missing_branches": missing,
                }
            )
            continue
        if receipt is not None and receipt.get("status") in _TERMINAL_SUCCESS:
            matched, reason = _receipt_matches(check, receipt, root)
            if matched:
                decisions.append(
                    {
                        "check_id": check["id"],
                        "status": "CACHED_PASS",
                        "reason": reason,
                        "semantic_signature": check["semantic_signature"],
                    }
                )
                continue
            decisions.append(
                {
                    "check_id": check["id"],
                    "status": "RUN_REQUIRED",
                    "reason": reason,
                    "run_scope": _run_scope(check, reason),
                }
            )
            invalidated.add(check["id"])
            continue
        reason = "no compatible PASS receipt"
        decisions.append(
            {
                "check_id": check["id"],
                "status": "RUN_REQUIRED",
                "reason": reason,
                "run_scope": _run_scope(check, reason),
            }
        )
        invalidated.add(check["id"])
    # A stale prerequisite invalidates only its transitive dependants.
    changed = True
    while changed:
        changed = False
        for check in checks:
            if check["id"] in invalidated or not any(
                parent in invalidated for parent in check.get("depends_on", [])
            ):
                continue
            invalidated.add(check["id"])
            for decision in decisions:
                if (
                    decision["check_id"] == check["id"]
                    and decision["status"] == "CACHED_PASS"
                ):
                    decision.update(
                        status="RUN_REQUIRED",
                        reason="upstream check invalidated",
                        run_scope=_run_scope(check, "upstream check invalidated"),
                    )
            changed = True
    if not canary["fanout_allowed"]:
        for decision in decisions:
            if decision["status"] in {"RUN_REQUIRED", "CACHED_PASS"} and any(
                check["id"] == decision["check_id"] and check.get("fanout", False)
                for check in checks
            ):
                decision.update(status="PENDING_CANARY", reason=canary["reason"])
    runnable = [
        item["check_id"] for item in decisions if item["status"] == "RUN_REQUIRED"
    ]
    decision_map = {item["check_id"]: item for item in decisions}
    runnable = [
        item["check_id"]
        for item in decisions
        if item["status"] == "RUN_REQUIRED"
        and all(
            decision_map[parent]["status"] in {"CACHED_PASS", "PASS"}
            for parent in check_map[item["check_id"]].get("depends_on", [])
        )
    ]
    allowed_runnable = [
        check_id
        for check_id in runnable
        if canary["fanout_allowed"] or not check_map[check_id].get("fanout", False)
    ]
    targeted_input_checks = [
        {
            "check_id": item["check_id"],
            "missing_branches": item.get("missing_branches", []),
            "action": "provide targeted input and rerun",
        }
        for item in decisions
        if item["status"] == "NEEDS_TARGETED_INPUT"
    ]
    return {
        "status": "PASS"
        if canary["fanout_allowed"]
        and not runnable
        and all(item["status"] in {"CACHED_PASS", "PASS"} for item in decisions)
        else "ACTION_REQUIRED",
        "canary": canary,
        "checks": decisions,
        "invalidated_checks": sorted(invalidated),
        "runnable_checks": allowed_runnable,
        "targeted_input_checks": targeted_input_checks,
        "full_run_required": any(
            item.get("run_scope") == "full"
            for item in decisions
            if item["status"] == "RUN_REQUIRED"
        ),
        "prediction_identity_is_executable_identity": False,
    }
