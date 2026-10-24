"""Exercise rank, normalization and runtime ID boundaries with synthetic inputs."""

import tempfile
import unittest
from pathlib import Path

from tools.casmi_contract import canonical_key, reciprocal_rank, validate_rows


class CasmiContractTests(unittest.TestCase):
    """Detect metric drift and invalid output before any external submission."""

    def test_first_hit_and_cutoff(self) -> None:
        """Count only the first correct guess and exclude rank 26."""
        self.assertEqual(reciprocal_rank(["CCO"], ["CC", "CCO", "CCO"]), 0.5)
        self.assertEqual(reciprocal_rank(["CCO"], ["CC"] * 25 + ["CCO"]), 0)

    def test_stereo_tautomer_regression(self) -> None:
        """Make stereochemical constraints irrelevant before tautomer enumeration."""
        self.assertEqual(canonical_key("C=C(C)C1CCC2CCC(=O)C=C2C1"),
                         canonical_key("C=C(C)[C@@H]1CCC2CCC(=O)C=C2C1"))

    def test_schema_and_runtime_id_coverage(self) -> None:
        """Reject duplicates, unknown IDs and too many guesses."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission.csv"
            path.write_text("molecule_id,smiles\nm_a,CCO\n")
            self.assertEqual(list(validate_rows(path, {"m_a"})), ["m_a"])
            with self.assertRaisesRegex(ValueError, "IDs differ"):
                validate_rows(path, {"m_b"})
            path.write_text("molecule_id,smiles\nm_a,CCO\nm_a,CCO\n")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                validate_rows(path)
            path.write_text("molecule_id,smiles\nm_a," + ";".join(["CCO"] * 26) + "\n")
            with self.assertRaisesRegex(ValueError, "1..25"):
                validate_rows(path)
