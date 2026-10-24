"""Lossless, explicit archival of terminal queue details."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .fingerprint import canonical_json_digest, sha256_file
from .scheduler import plan_work_queue


class StateError(ValueError):
    """Raised when state archival would overwrite or discard evidence."""


def compact_state(source: Path, archive: Path, output: Path) -> dict[str, Any]:
    """Archive terminal jobs while retaining dependency tombstones.

    The source is never edited. Both destinations must be new files, so a
    failed or concurrent invocation cannot silently replace prior history.
    Active jobs keep their terminal prerequisites in compact form; their IDs
    remain resolvable by the same scheduler.
    """
    paths = [path.resolve() for path in (source, archive, output)]
    if len(set(paths)) != 3 or archive.exists() or output.exists():
        raise StateError("source, new archive and new output must be distinct")
    state = json.loads(source.read_text(encoding="utf-8"))
    plan_work_queue(state)
    terminal = {"completed", "failed", "cancelled"}
    jobs = state["jobs"]
    by_id = {job["id"]: job for job in jobs}
    needed = {job["id"] for job in jobs if job["status"] not in terminal}
    frontier = list(needed)
    while frontier:
        for dependency in by_id[frontier.pop()].get("depends_on", []):
            if dependency not in needed:
                needed.add(dependency)
                frontier.append(dependency)
    archived = [job for job in jobs if job["status"] in terminal]
    compact = []
    for job in jobs:
        if job["status"] not in terminal:
            compact.append(job)
        elif job["id"] in needed:
            compact.append(
                {
                    "id": job["id"],
                    "status": job["status"],
                    "depends_on": [],
                    "resources": {},
                    "estimated_seconds": 0,
                    "priority": 0,
                }
            )
    result = {**state, "jobs": compact}
    if result.get("daily_plan"):
        result["daily_plan"] = {
            **result["daily_plan"],
            "candidate_job_ids": [
                value
                for value in result["daily_plan"].get("candidate_job_ids", [])
                if value in needed
            ],
        }
    plan_work_queue(result)
    payload = {
        "schema_version": 1,
        "source_sha256": sha256_file(source),
        "jobs": archived,
        "compact_digest": canonical_json_digest(result),
    }
    for path, value in ((archive, payload), (output, result)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    return {
        "status": "PASS",
        "archived_count": len(archived),
        "active_count": len(compact),
        "source_unchanged": sha256_file(source) == payload["source_sha256"],
        "archive": {"path": str(archive), "sha256": sha256_file(archive)},
        "output": {"path": str(output), "sha256": sha256_file(output)},
    }
