"""Repository audit for experiment completeness and duplicate artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict
import json
from pathlib import Path
import subprocess
from typing import Any

from .fingerprint import sha256_file


DEFAULT_LAYOUT: dict[str, Any] = {
    "schema_version": 1,
    "experiment_roots": ["workspace", "experiments"],
    "experiment_prefixes": ["exp"],
    "submission_roots": ["submit"],
    "required_experiment_files": {
        "session_notes": "SESSION_NOTES.md",
        "run_script": "run.sh",
        "results_dir": "results",
    },
    "payload_extensions": [".csv", ".zip", ".ipynb"],
    "payload_names": ["agent.py"],
    "ignored_parts": ["__pycache__", ".git", ".venv", ".pytest_cache", ".ruff_cache"],
    "ignored_extensions": [".pyc", ".pyo"],
    "ignored_names": [".DS_Store"],
    "history_paths": ["daily_reports", "submit/SUBMISSIONS.md", "claudeSummary.md"],
}


class AuditError(ValueError):
    """Raised when a layout profile is malformed."""


def _relative_layout_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuditError(f"{name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise AuditError(f"{name} must stay below the repository root")
    return path.as_posix()


def _validate_layout(layout: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version", "experiment_roots", "experiment_prefixes", "submission_roots",
        "required_experiment_files", "payload_extensions", "payload_names", "ignored_parts",
        "ignored_extensions", "ignored_names", "history_paths",
    }
    unknown = sorted(set(layout).difference(allowed))
    if unknown:
        raise AuditError(f"unknown layout field(s): {unknown}")
    if layout.get("schema_version") != 1:
        raise AuditError("layout schema_version must be 1")
    for field in ("experiment_roots", "submission_roots", "history_paths"):
        values = layout.get(field)
        if not isinstance(values, list):
            raise AuditError(f"layout.{field} must be a list")
        layout[field] = [_relative_layout_path(value, f"layout.{field}") for value in values]
    for field in ("experiment_prefixes", "payload_names", "ignored_parts"):
        values = layout.get(field)
        if not isinstance(values, list) or not all(isinstance(value, str) and value.strip() for value in values):
            raise AuditError(f"layout.{field} must be a list of non-empty strings")
    for field in ("ignored_extensions", "ignored_names"):
        values = layout.get(field, [])
        if not isinstance(values, list) or not all(isinstance(value, str) and value.strip() for value in values):
            raise AuditError(f"layout.{field} must be a list of non-empty strings")
        layout[field] = values
    extensions = layout.get("payload_extensions")
    if not isinstance(extensions, list) or not all(isinstance(value, str) and value.startswith(".") for value in extensions):
        raise AuditError("layout.payload_extensions must be dot-prefixed strings")
    required = layout.get("required_experiment_files")
    if not isinstance(required, dict):
        raise AuditError("layout.required_experiment_files must be an object")
    layout["required_experiment_files"] = {
        str(key): _relative_layout_path(value, f"layout.required_experiment_files.{key}")
        for key, value in required.items()
    }
    return layout


def _load_layout(root: Path, layout_path: Path | None) -> dict[str, Any]:
    candidate = layout_path or (root / "automation" / "layout.json")
    if not candidate.is_file():
        return {key: value.copy() if isinstance(value, dict) else list(value) if isinstance(value, list) else value for key, value in DEFAULT_LAYOUT.items()}
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuditError(f"invalid layout JSON: {candidate}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AuditError("layout root must be an object")
    return _validate_layout(raw)


def _experiment_dirs(root: Path, layout: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    prefixes = tuple(layout["experiment_prefixes"])
    for relative in layout["experiment_roots"]:
        base = root / relative
        if not base.is_dir():
            continue
        candidates.extend(path for path in base.iterdir() if path.is_dir() and path.name.startswith(prefixes))
    return sorted(set(candidates))


def _git_snapshot(root: Path) -> dict[str, str | None]:
    """Return a best-effort immutable-ish Git snapshot for audit reproducibility."""
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--short"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return {"git_head": None, "git_status_short": None}
    return {
        "git_head": head.stdout.strip() if head.returncode == 0 else None,
        "git_status_short": status.stdout[:4000] if status.returncode == 0 else None,
    }


def audit_repository(root: Path, layout_path: Path | None = None) -> dict[str, Any]:
    """Inspect experiment and submission conventions without executing models."""
    root = root.expanduser().resolve()
    layout = _load_layout(root, layout_path.expanduser().resolve() if layout_path else None)
    experiments: list[dict[str, Any]] = []
    required_files = layout["required_experiment_files"]
    for path in _experiment_dirs(root, layout):
        required = {
            key: (path / relative).is_dir() if key.endswith("_dir") else (path / relative).is_file()
            for key, relative in required_files.items()
        }
        experiments.append(
            {
                "path": path.relative_to(root).as_posix(),
                "checks": required,
                "status": "PASS" if all(required.values()) else "WARN",
            }
        )
    submission_hashes: defaultdict[str, list[str]] = defaultdict(list)
    support_file_count = 0
    payload_extensions = frozenset(value.lower() for value in layout["payload_extensions"])
    payload_names = frozenset(layout["payload_names"])
    ignored_parts = frozenset(layout["ignored_parts"])
    ignored_extensions = frozenset(value.lower() for value in layout.get("ignored_extensions", []))
    ignored_names = frozenset(layout.get("ignored_names", []))
    for relative_submit_root in layout["submission_roots"]:
        submit_root = root / relative_submit_root
        if not submit_root.is_dir():
            continue
        for path in sorted(submit_root.rglob("*")):
            if (
                not path.is_file()
                or any(part in ignored_parts for part in path.relative_to(root).parts)
                or path.suffix.lower() in ignored_extensions
                or path.name in ignored_names
            ):
                continue
            if path.suffix.lower() not in payload_extensions and path.name not in payload_names:
                support_file_count += 1
                continue
            digest = sha256_file(path)
            submission_hashes[digest].append(path.relative_to(root).as_posix())
    duplicate_groups = [paths for paths in submission_hashes.values() if len(paths) > 1]
    control_hashes: set[str] = set()
    ledger_path = root / "automation" / "ledger.jsonl"
    if ledger_path.is_file():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("control") is True:
                control_hashes.update(
                    artifact.get("sha256")
                    for artifact in record.get("artifacts", [])
                    if isinstance(artifact, dict) and isinstance(artifact.get("sha256"), str)
                )
    control_duplicate_groups = [
        paths
        for digest, paths in submission_hashes.items()
        if len(paths) > 1 and digest in control_hashes
    ]
    uncontrolled_duplicate_groups = [
        paths for paths in duplicate_groups if paths not in control_duplicate_groups
    ]
    result: dict[str, Any] = {
        "root": str(root),
        "layout": layout,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "experiment_count": len(experiments),
        "experiments": experiments,
        "submission_payload_count": sum(len(paths) for paths in submission_hashes.values()),
        "submission_artifact_count": sum(len(paths) for paths in submission_hashes.values()),
        "submission_support_file_count": support_file_count,
        "history_checks": {
            relative: (root / relative).exists() for relative in layout["history_paths"]
        },
        "duplicate_submission_groups": duplicate_groups,
        "control_duplicate_groups": control_duplicate_groups,
        "uncontrolled_duplicate_groups": uncontrolled_duplicate_groups,
        "warnings": [
            "duplicate submission content detected"
            for _ in duplicate_groups
        ],
    }
    result.update(_git_snapshot(root))
    return result
