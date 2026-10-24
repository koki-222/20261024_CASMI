"""Validate saved evidence hashes, aggregate projections and arithmetic without reruns."""

from __future__ import annotations

import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any


def check_arithmetic(value: Any) -> int:
    """Check available exact-rank fractions and paired-count denominators recursively."""
    checked = 0
    if isinstance(value, dict):
        for fraction_key, number_key in [("mrr_fraction", "mrr"), ("delta_mrr_fraction", "delta_mrr"), ("fraction", "mrr")]:
            if fraction_key in value and number_key in value:
                if not math.isclose(float(Fraction(value[fraction_key])), value[number_key], abs_tol=1e-12, rel_tol=0):
                    raise ValueError(f"Fraction mismatch: {fraction_key}")
                checked += 1
        for key in ("wins_losses_ties", "paired_wlt"):
            if key in value and "queries" in value:
                if sum(value[key]) != value["queries"]:
                    raise ValueError("Paired denominator mismatch")
                checked += 1
        for child in value.values():
            checked += check_arithmetic(child)
    elif isinstance(value, list):
        checked += sum(check_arithmetic(child) for child in value)
    return checked


def validate(root: Path) -> dict[str, Any]:
    """Verify frozen originals and the exact aggregate values published from them."""
    index = json.loads((root / "research/legacy/source_index.json").read_text())
    sources = {}
    for item in index["sources"]:
        path = root / item["snapshot_path"]
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            raise ValueError("Legacy snapshot escapes root or is a symlink")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError(f"Legacy source changed: {item['path']}")
        sources[item["path"]] = item
    evidence = json.loads((root / "research/legacy/comparison_evidence.json").read_text())
    projections = 0
    for case in evidence["cases"]:
        item = sources[case["source"]]
        if case["source_sha256"] != item["sha256"]:
            raise ValueError("Case source digest differs from index")
        data = json.loads((root / item["snapshot_path"]).read_text())
        for projection in case["values"]:
            value = data
            for key in projection["pointer"]:
                value = value[key]
            if value != projection["value"]:
                raise ValueError(f"Projection mismatch: {case['id']}")
            projections += 1
    arithmetic = check_arithmetic(evidence)
    observations = json.loads((root / "research/legacy/official_observations.json").read_text())
    for item in observations["items"]:
        if item["source_sha256"] != sources[item["source"]]["sha256"]:
            raise ValueError("Official observation source mismatch")
        if item["submission_id"] == "unknown":
            raise ValueError("Official observation missing submission ID")
        if any(not ref["sha256_matches"] for ref in item["links_verified"]):
            raise ValueError("Official observation binding mismatch")
    return {"status":"PASS_SAVED_SOURCES_AND_AGGREGATES_ONLY", "sources":len(sources),
            "cases":len(evidence["cases"]), "projections":projections,
            "arithmetic_checks":arithmetic, "official_observations":len(observations["items"]),
            "new_inference_or_training":False, "live_official_refresh":False}


if __name__ == "__main__":
    print(json.dumps(validate(Path(__file__).resolve().parents[1]), ensure_ascii=False, indent=2))
