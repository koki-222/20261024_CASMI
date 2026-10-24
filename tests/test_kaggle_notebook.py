"""Tests for the Kaggle-only Biohub Notebook control layer."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tools.kaggle_notebook import KaggleNotebookError, build_metadata, load_config


class KaggleNotebookTests(unittest.TestCase):
    """Keep the Notebook private, offline, GPU-backed, and runtime-bounded."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        source = self.root / "notebooks" / "kaggle" / "biohub-pdca"
        source.mkdir(parents=True)
        (source / "biohub_pdca.ipynb").write_text("{}\n", encoding="utf-8")
        self.config = {
            "schema_version": 1,
            "competition_slug": "biohub-cell-tracking-during-development",
            "kernel": {
                "owner": "noncaffeine",
                "slug": "biohub-pdca",
                "title": "Biohub cell tracking PDCA",
                "source_dir": "notebooks/kaggle/biohub-pdca",
                "code_file": "biohub_pdca.ipynb",
                "language": "python",
                "kernel_type": "notebook",
                "is_private": True,
                "enable_gpu": True,
                "enable_internet": False,
                "dataset_sources": [],
                "kernel_sources": [],
            },
            "quota": {
                "colab_linked": True,
                "exact_weekly_gpu_hours": None,
                "remaining_gpu_hours": None,
                "reset_at": None,
                "evidence_kind": "user_reported",
                "observed_at": None,
            },
            "execution": {
                "official_max_runtime_minutes": 720,
                "planning_limit_minutes": 540,
                "checkpoint_interval_minutes": 30,
                "required_outputs": [
                    "runtime_probe.json",
                    "data_inventory.json",
                    "run_manifest.json",
                ],
            },
            "output_root": "state/kaggle/runs",
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _load(self, config: dict[str, object] | None = None) -> dict[str, object]:
        path = self.root / "config.json"
        path.write_text(json.dumps(config or self.config), encoding="utf-8")
        return load_config(path, self.root)

    def test_valid_config_builds_exact_private_offline_metadata(self) -> None:
        config = self._load()
        metadata = build_metadata(config)
        self.assertEqual(metadata["id"], "noncaffeine/biohub-pdca")
        self.assertEqual(
            metadata["competition_sources"], ["biohub-cell-tracking-during-development"]
        )
        self.assertTrue(metadata["enable_gpu"])
        self.assertFalse(metadata["enable_internet"])
        self.assertTrue(metadata["is_private"])

    def test_internet_or_public_notebook_is_rejected(self) -> None:
        for field, value in (("enable_internet", True), ("is_private", False)):
            config = deepcopy(self.config)
            config["kernel"][field] = value
            with self.subTest(field=field), self.assertRaises(KaggleNotebookError):
                self._load(config)

    def test_runtime_plan_requires_twenty_percent_margin(self) -> None:
        config = deepcopy(self.config)
        config["execution"]["planning_limit_minutes"] = 577
        with self.assertRaisesRegex(KaggleNotebookError, "20% runtime margin"):
            self._load(config)

    def test_known_quota_requires_observation_time(self) -> None:
        config = deepcopy(self.config)
        config["quota"]["remaining_gpu_hours"] = 40
        with self.assertRaisesRegex(KaggleNotebookError, "observed_at"):
            self._load(config)

    def test_repository_escape_is_rejected(self) -> None:
        config = deepcopy(self.config)
        config["output_root"] = "../outside"
        with self.assertRaisesRegex(KaggleNotebookError, "safe repository-relative"):
            self._load(config)


if __name__ == "__main__":
    unittest.main()
