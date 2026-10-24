"""Tests for stage-complete worker input validation."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.competition_automation.worker_inputs import WorkerInputError, check_worker_inputs


class WorkerInputTests(unittest.TestCase):
    """Ensure file and directory inputs are checked by declared type."""

    def test_all_stages_must_have_actual_inputs(self) -> None:
        """A passed stage cannot mask a missing downstream input."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            (root / "input" / "sample.dat").write_text("sample\n", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "stages": [
                    {"name": "main", "inputs": [
                        {"pattern": "input/*.dat", "kind": "file", "minimum_count": 1}
                    ]},
                    {"name": "auxiliary", "inputs": [
                        {"pattern": "input/*.bundle", "kind": "directory", "minimum_count": 1}
                    ]},
                ],
            }
            result = check_worker_inputs(manifest, root)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["failed"][0]["stage"], "auxiliary")
            (root / "input" / "sample.bundle").mkdir()
            self.assertEqual(check_worker_inputs(manifest, root)["status"], "PASS")

    def test_escape_pattern_is_rejected(self) -> None:
        """A declared input must stay inside the worker root."""
        with tempfile.TemporaryDirectory() as directory:
            manifest = {"schema_version": 1, "stages": [{"name": "train", "inputs": [
                {"pattern": "../secret", "kind": "file", "minimum_count": 1}
            ]}]}
            with self.assertRaisesRegex(WorkerInputError, "invalid pattern"):
                check_worker_inputs(manifest, Path(directory))


if __name__ == "__main__":
    unittest.main()
