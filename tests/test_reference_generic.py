"""Tests for the opt-in dependency-free reference demo."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reference_generic.run import _resume_contract, load_rows, train


class ReferenceGenericTests(unittest.TestCase):
    """Verify strict resume semantics and neutral scaffold behavior."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.data = self.root / "train.csv"
        self.data.write_text("x,y,target\n1,2,5\n2,1,6\n3,4,11\n", encoding="utf-8")
        self.matrix, self.labels, data_sha = load_rows(self.data, "target", ["x", "y"])
        self.contract = _resume_contract(data_sha, "target", ["x", "y"], "sgd", 0.01)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_partial_resume_matches_uninterrupted_run(self) -> None:
        """A checkpoint resumed to N steps equals one uninterrupted N-step run."""
        full_checkpoint = self.root / "full.json"
        partial_checkpoint = self.root / "partial.json"
        resumed_checkpoint = self.root / "resumed.json"
        full = train(
            self.matrix, self.labels, 12, 0.01, 3, full_checkpoint, None, self.contract
        )
        train(
            self.matrix,
            self.labels,
            5,
            0.01,
            3,
            partial_checkpoint,
            None,
            self.contract,
        )
        resumed = train(
            self.matrix,
            self.labels,
            12,
            0.01,
            3,
            resumed_checkpoint,
            partial_checkpoint,
            self.contract,
        )
        self.assertEqual(
            json.loads(full_checkpoint.read_text()),
            json.loads(resumed_checkpoint.read_text()),
        )
        self.assertEqual(full["train_mse"], resumed["train_mse"])

    def test_resume_rejects_data_or_learning_rate_change(self) -> None:
        """Resume must reject changes that invalidate computation identity."""
        checkpoint = self.root / "checkpoint.json"
        train(self.matrix, self.labels, 4, 0.01, 2, checkpoint, None, self.contract)
        changed_data = dict(self.contract, data_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "resume data/features"):
            train(
                self.matrix,
                self.labels,
                8,
                0.01,
                2,
                checkpoint,
                checkpoint,
                changed_data,
            )
        changed_rate = dict(self.contract, learning_rate=0.02)
        with self.assertRaisesRegex(ValueError, "resume data/features"):
            train(
                self.matrix,
                self.labels,
                8,
                0.02,
                2,
                checkpoint,
                checkpoint,
                changed_rate,
            )

    def test_invalid_budget_is_rejected(self) -> None:
        """Non-positive or non-finite budgets cannot start the demo."""
        with self.assertRaises(ValueError):
            train(
                self.matrix,
                self.labels,
                0,
                0.01,
                2,
                self.root / "bad.json",
                None,
                self.contract,
            )
        with self.assertRaises(ValueError):
            train(
                self.matrix,
                self.labels,
                3,
                float("nan"),
                2,
                self.root / "bad.json",
                None,
                self.contract,
            )

    def test_corrupt_checkpoint_parameters_are_rejected(self) -> None:
        """Resume must reject non-finite model parameters instead of coercing them."""
        checkpoint = self.root / "checkpoint.json"
        train(self.matrix, self.labels, 4, 0.01, 2, checkpoint, None, self.contract)
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        state["weights"][0] = "not-a-number"
        checkpoint.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "weights must be finite"):
            train(
                self.matrix,
                self.labels,
                8,
                0.01,
                2,
                checkpoint,
                checkpoint,
                self.contract,
            )


if __name__ == "__main__":
    unittest.main()
