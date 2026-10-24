"""Verify every worker stage's materialized inputs before costly execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class WorkerInputError(ValueError):
    """Raised when a worker input contract or the actual input roster fails."""


def check_worker_inputs(manifest: dict[str, Any], root: Path) -> dict[str, Any]:
    """Count file or directory matches for all declared downstream stages."""
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "stages"}:
        raise WorkerInputError("worker input manifest requires schema_version and stages")
    if manifest["schema_version"] != 1:
        raise WorkerInputError("worker input schema_version must be 1")
    stages = manifest["stages"]
    if not isinstance(stages, list) or not stages:
        raise WorkerInputError("stages must be a non-empty list")
    base = root.expanduser().resolve()
    results = []
    seen = set()
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != {"name", "inputs"}:
            raise WorkerInputError("each stage requires name and inputs")
        name, inputs = stage["name"], stage["inputs"]
        if not isinstance(name, str) or not name or name in seen:
            raise WorkerInputError("stage names must be unique and nonempty")
        seen.add(name)
        if not isinstance(inputs, list) or not inputs:
            raise WorkerInputError(f"{name}.inputs must be nonempty")
        for item in inputs:
            if not isinstance(item, dict) or set(item) != {"pattern", "kind", "minimum_count"}:
                raise WorkerInputError(f"{name}.inputs entry is malformed")
            pattern, kind, minimum = item["pattern"], item["kind"], item["minimum_count"]
            if (
                not isinstance(pattern, str) or not pattern
                or Path(pattern).is_absolute() or ".." in Path(pattern).parts
                or kind not in {"file", "directory"}
                or isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1
            ):
                raise WorkerInputError(f"{name}.inputs has invalid pattern/kind/minimum_count")
            matches = []
            for path in base.glob(pattern):
                if path.is_symlink() or not path.resolve().is_relative_to(base):
                    raise WorkerInputError(f"{name} matched unsafe path: {path}")
                if (kind == "file" and path.is_file()) or (kind == "directory" and path.is_dir()):
                    matches.append(path)
            results.append(
                {"stage": name, "pattern": pattern, "kind": kind,
                 "minimum_count": minimum, "observed_count": len(matches),
                 "passed": len(matches) >= minimum}
            )
    failed = [item for item in results if not item["passed"]]
    return {"status": "PASS" if not failed else "FAIL", "checks": results, "failed": failed}
