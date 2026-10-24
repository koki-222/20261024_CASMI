"""Content fingerprints for reproducible experiment and submission records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable


DEFAULT_IGNORED_PARTS = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest of a file without loading it all in memory."""
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_files(root: Path, ignored_parts: Iterable[str]) -> list[Path]:
    """Return deterministic, relative-file ordered paths below ``root``."""
    ignored = set(ignored_parts)
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not any(part in ignored for part in path.relative_to(root).parts)
    )


def sha256_tree(root: Path, ignored_parts: Iterable[str] = DEFAULT_IGNORED_PARTS) -> str:
    """Hash a directory from relative names and file content hashes.

    Modification times, permissions, and traversal order are intentionally not
    included.  A copied checkout therefore has the same identity as the
    original checkout when its tracked content is the same.
    """
    if not root.is_dir():
        raise NotADirectoryError(root)
    digest = hashlib.sha256()
    for path in _iter_files(root, ignored_parts):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\n")
    return digest.hexdigest()


def fingerprint_path(
    path: Path,
    ignored_parts: Iterable[str] = DEFAULT_IGNORED_PARTS,
) -> dict[str, object]:
    """Return a stable fingerprint record for a file or directory."""
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        return {
            "path": str(resolved),
            "kind": "file",
            "size_bytes": resolved.stat().st_size,
            "sha256": sha256_file(resolved),
        }
    if resolved.is_dir():
        files = _iter_files(resolved, ignored_parts)
        return {
            "path": str(resolved),
            "kind": "directory",
            "file_count": len(files),
            "sha256": sha256_tree(resolved, ignored_parts),
        }
    raise FileNotFoundError(resolved)


def canonical_json_digest(value: object) -> str:
    """Hash JSON-compatible data with sorted keys and stable separators."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def artifact_record(path: Path, root: Path | None = None) -> dict[str, object]:
    """Create a manifest entry with a repository-relative path when possible."""
    record = fingerprint_path(path)
    if root is not None:
        root_resolved = root.expanduser().resolve()
        path_resolved = path.expanduser().resolve()
        try:
            record["relative_path"] = path_resolved.relative_to(root_resolved).as_posix()
        except ValueError:
            record["relative_path"] = None
    return record
