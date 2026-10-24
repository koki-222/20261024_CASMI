"""Task completion and hypothesis-exhaustion guards."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from .fingerprint import fingerprint_path


class CompletionError(ValueError):
    """Raised when a task is marked complete without objective evidence."""


def _text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CompletionError(f"{field} must be a non-empty string")


def _timestamp(value: object, field: str) -> datetime:
    """Validate and return one timezone-aware ISO-8601 timestamp."""
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompletionError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise CompletionError(f"{field} must include a timezone")
    return parsed


def _snapshot(root: Path, relative: object, digest: object, field: str) -> Path:
    _text(relative, f"{field}.path")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise CompletionError(f"{field}.sha256 must be a lowercase SHA-256")
    root_resolved = root.expanduser().resolve()
    raw = root_resolved / str(relative)
    if raw.is_symlink():
        raise CompletionError(f"{field} snapshot must not be a symlink")
    path = raw.resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise CompletionError(f"{field} snapshot escapes root") from exc
    if not path.is_file():
        raise CompletionError(f"{field} snapshot is missing: {relative}")
    if fingerprint_path(path)["sha256"] != digest:
        raise CompletionError(f"{field} snapshot hash mismatch")
    return path


def validate_completion_state(
    path: Path,
    root: Path,
    quota_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate objective proof and keep unresolved work resumable."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CompletionError(f"completion state is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CompletionError(f"invalid completion state: {path}: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise CompletionError("completion state schema_version must be 1")
    status = state.get("status")
    if status not in {"running", "blocked", "completed", "failed", "aborted", "ended"}:
        raise CompletionError(f"invalid completion status: {status!r}")
    objective = state.get("objective")
    if not isinstance(objective, dict) or not isinstance(objective.get("achieved"), bool):
        raise CompletionError("objective.achieved must be boolean")
    proof = objective.get("proof")
    if objective["achieved"]:
        if not isinstance(proof, dict):
            raise CompletionError("objective proof is required when achieved=true")
        _snapshot(root, proof.get("artifact_path"), proof.get("sha256"), "objective.proof")
    if status == "completed" and not objective["achieved"]:
        raise CompletionError("unachieved objective cannot be marked completed")
    if status == "ended":
        if objective["achieved"]:
            raise CompletionError("achieved objectives must use completed, not ended")
        _text(state.get("reason"), "reason")
        _timestamp(state.get("ended_at"), "ended_at")
        terminal = state.get("terminal_evidence")
        if not isinstance(terminal, dict):
            raise CompletionError("ended status requires terminal_evidence")
        _snapshot(
            root,
            terminal.get("artifact_path"),
            terminal.get("sha256"),
            "terminal_evidence",
        )
    if state.get("daily_quota_required") is True and status != "ended":
        if not isinstance(quota_plan, dict) or quota_plan.get("status") != "COMPLETE":
            raise CompletionError("daily quota is required but distinct ready artifacts are incomplete")
    if status == "blocked":
        _text(state.get("reason"), "reason")
        _text(state.get("resume_condition"), "resume_condition")
    exhaustion = state.get("hypothesis_exhaustion")
    if exhaustion is not None:
        if not isinstance(exhaustion, dict) or exhaustion.get("claimed") is not True:
            raise CompletionError("hypothesis_exhaustion.claimed must be true when present")
        _text(exhaustion.get("search_space"), "hypothesis_exhaustion.search_space")
        families = exhaustion.get("families_checked")
        if not isinstance(families, list) or not families:
            raise CompletionError("hypothesis_exhaustion.families_checked is required")
        negative = exhaustion.get("negative_evidence")
        if not isinstance(negative, list) or not negative:
            raise CompletionError("hypothesis_exhaustion.negative_evidence is required")
        for index, item in enumerate(negative):
            if not isinstance(item, dict):
                raise CompletionError(f"hypothesis_exhaustion.negative_evidence[{index}] must be an object")
            _snapshot(root, item.get("artifact_path"), item.get("sha256"), f"negative_evidence[{index}]")
        _text(exhaustion.get("last_reviewed_at"), "hypothesis_exhaustion.last_reviewed_at")
        _text(exhaustion.get("resume_condition"), "hypothesis_exhaustion.resume_condition")
    return {
        "status": "PASS",
        "task_status": status,
        "objective_achieved": objective["achieved"],
        "hypothesis_exhaustion_verified": exhaustion is not None,
        "terminal_without_success": status == "ended",
    }
