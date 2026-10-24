"""Validate molecule-level CASMI files and mirror the documented metric semantics."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

EXPECTED_RDKIT_VERSION = "2026.03.3"
TEST_COLUMNS = {
    "molecule_id", "spectrum_id", "ms2_mzs", "ms2_normalized_intensities",
    "base_peak_intensity", "adduct", "ionization_mode", "instrument_type",
    "precursor_mz", "collision_energy_ev", "collision_energy_orig",
    "collision_energy_orig_units",
}


def canonical_key(smiles: str) -> str | None:
    """Remove stereochemistry before tautomer normalization with the official pin."""
    import rdkit
    from rdkit import Chem
    from rdkit.Chem.MolStandardize import rdMolStandardize

    if rdkit.__version__ != EXPECTED_RDKIT_VERSION:
        raise RuntimeError(f"RDKit version mismatch: {rdkit.__version__}")
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        return None
    try:
        Chem.RemoveStereochemistry(molecule)
        normalized = rdMolStandardize.TautomerEnumerator().Canonicalize(molecule)
        return Chem.MolToInchiKey(normalized)[:14] or None
    except (ValueError, RuntimeError, TypeError):
        return None


def validate_rows(path: Path, expected_ids: set[str] | None = None) -> dict[str, list[str]]:
    """Require one ranked list per expected molecule and valid nonempty SMILES."""
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["molecule_id", "smiles"]:
            raise ValueError("CSV columns must be molecule_id,smiles in that order")
        rows = list(reader)
    if not rows:
        raise ValueError("Submission is empty")
    predictions = {}
    for row in rows:
        key, value = row["molecule_id"], row["smiles"]
        if not key or not value or key in predictions or None in row:
            raise ValueError("Empty fields, duplicate molecule_id, or malformed CSV")
        candidates = [part.strip() for part in value.split(";")]
        if not 1 <= len(candidates) <= 25 or not all(candidates):
            raise ValueError("Require 1..25 nonempty semicolon-separated candidates")
        if any(canonical_key(candidate) is None for candidate in candidates):
            raise ValueError("Invalid SMILES")
        predictions[key] = candidates
    if expected_ids is not None and set(predictions) != expected_ids:
        raise ValueError("Molecule IDs differ from runtime input")
    return predictions


def reciprocal_rank(answers: list[str], candidates: list[str]) -> float:
    """Return the first matching rank; absent or invalid guesses earn zero."""
    accepted = {key for answer in answers if (key := canonical_key(answer))}
    if not accepted:
        raise ValueError("No valid answer structure")
    for rank, candidate in enumerate(candidates[:25], 1):
        if canonical_key(candidate) in accepted:
            return 1.0 / rank
    return 0.0


def inspect_test(path: Path) -> dict[str, Any]:
    """Check actual test arrays and molecule axes without using example labels."""
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    if set(table.column_names) != TEST_COLUMNS:
        raise ValueError(f"Unexpected test schema: {table.column_names}")
    rows = table.to_pylist()
    spectrum_ids = set()
    for row in rows:
        if not row["molecule_id"] or not row["spectrum_id"] or row["spectrum_id"] in spectrum_ids:
            raise ValueError("Missing IDs or duplicate spectrum_id")
        spectrum_ids.add(row["spectrum_id"])
        mzs, intensities = row["ms2_mzs"], row["ms2_normalized_intensities"]
        if mzs is None or intensities is None or len(mzs) != len(intensities) or not mzs:
            raise ValueError("Unaligned or empty peak arrays")
        if not all(math.isfinite(x) and x > 0 for x in mzs):
            raise ValueError("Nonfinite or nonpositive m/z")
        if not all(math.isfinite(x) and 0 <= x <= 1.000001 for x in intensities):
            raise ValueError("Intensity outside [0,1]")
        if not math.isfinite(row["precursor_mz"]) or row["precursor_mz"] <= 0:
            raise ValueError("Invalid precursor m/z")
    return {
        "rows": len(rows), "molecules": len({row["molecule_id"] for row in rows}),
        "schema": str(table.schema),
        "adduct_counts": dict(Counter(row["adduct"] for row in rows)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "performance_evidence": False,
    }


def main() -> None:
    """Expose read-only file inspection, without training or Kaggle writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", type=Path)
    parser.add_argument("--submission", type=Path)
    args = parser.parse_args()
    report: dict[str, Any] = {}
    if args.test:
        report["test"] = inspect_test(args.test)
    if args.submission:
        expected = None
        if args.test:
            import pyarrow.parquet as pq

            expected = set(pq.read_table(args.test, columns=["molecule_id"])["molecule_id"].to_pylist())
        report["submission_rows"] = len(validate_rows(args.submission, expected))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
