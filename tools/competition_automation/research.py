"""Deterministic research-preflight validation before analysis starts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .fingerprint import fingerprint_path


class ResearchError(ValueError):
    """Raised when the required pre-analysis research evidence is incomplete."""


DEFAULT_REQUIRED_SECTIONS = (
    "rules",
    "data",
    "metric",
    "submission",
    "timeline_quota",
    "leakage",
    "history",
)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ResearchError(f"{field} must be a non-empty string")


def _sha(value: object, field: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ResearchError(f"{field} must be a lowercase SHA-256")


def _safe_artifact(root: Path, relative: str, digest: str) -> None:
    root_resolved = root.expanduser().resolve()
    candidate_raw = root_resolved / relative
    if candidate_raw.is_symlink():
        raise ResearchError(f"research artifact must not be a symlink: {relative}")
    candidate = candidate_raw.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ResearchError(f"research artifact escapes root: {relative}") from exc
    if not candidate.is_file():
        raise ResearchError(f"research artifact is missing: {relative}")
    if fingerprint_path(candidate)["sha256"] != digest:
        raise ResearchError(f"research artifact hash mismatch: {relative}")


def validate_research_manifest(
    path: Path,
    root: Path,
    required_sections: list[str] | tuple[str, ...] = DEFAULT_REQUIRED_SECTIONS,
    minimum_sources: int = 1,
) -> dict[str, Any]:
    """Validate a research manifest and re-hash every saved source snapshot.

    URLs or LLM summaries alone are not evidence. Each section must point to a
    local snapshot below ``root`` with an observed timestamp and SHA-256.
    """
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResearchError(f"research manifest is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ResearchError(f"invalid research manifest: {path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ResearchError("research manifest schema_version must be 1")
    sections = manifest.get("sections")
    if not isinstance(sections, dict):
        raise ResearchError("research manifest sections must be an object")
    required = tuple(required_sections)
    missing = [section for section in required if section not in sections]
    if missing:
        raise ResearchError(f"research sections are missing: {missing}")
    source_count = 0
    checked_sections: dict[str, int] = {}
    for section_name in required:
        section = sections[section_name]
        if not isinstance(section, dict):
            raise ResearchError(f"research section {section_name} must be an object")
        sources = section.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ResearchError(f"research section {section_name} needs sources")
        checked = 0
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                raise ResearchError(f"{section_name}.sources[{index}] must be an object")
            _text(source.get("kind"), f"{section_name}.sources[{index}].kind")
            _text(source.get("uri"), f"{section_name}.sources[{index}].uri")
            _text(source.get("observed_at"), f"{section_name}.sources[{index}].observed_at")
            artifact_path = source.get("artifact_path")
            _text(artifact_path, f"{section_name}.sources[{index}].artifact_path")
            digest = source.get("sha256")
            _sha(digest, f"{section_name}.sources[{index}].sha256")
            _safe_artifact(root, str(artifact_path), str(digest))
            checked += 1
        source_count += checked
        checked_sections[section_name] = checked
    if source_count < minimum_sources:
        raise ResearchError(f"research source count {source_count} is below minimum {minimum_sources}")
    return {
        "status": "PASS",
        "manifest": str(path.expanduser().resolve()),
        "required_sections": list(required),
        "checked_sections": checked_sections,
        "source_count": source_count,
    }
