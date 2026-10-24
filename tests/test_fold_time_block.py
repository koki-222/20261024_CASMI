"""Regression tests for expanding-window fold leakage boundaries."""

from __future__ import annotations

import importlib.util
import unittest
from argparse import Namespace
from pathlib import Path


try:
    import pandas as pd
except ModuleNotFoundError:  # pragma: no cover - exercised in the competition venv
    pd = None


ROOT = Path(__file__).resolve().parents[1]
FOLD_SCRIPT = ROOT / "workspace" / "fold" / "generate_folds.py"


def _load_fold_module():
    if pd is None:
        return None
    spec = importlib.util.spec_from_file_location("competition_fold_generator", FOLD_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load fold generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(pd is not None, "pandas is available in the competition environment only")
class TimeBlockTests(unittest.TestCase):
    """Ensure a later validation block never enters an earlier train set."""

    def test_select_time_block_split_excludes_future_rows(self) -> None:
        module = _load_fold_module()
        assert module is not None
        frame = pd.DataFrame(
            {
                "ID": [f"id-{index}" for index in range(12)],
                "time": pd.date_range("2020-01-01", periods=12, freq="D"),
            }
        )
        args = Namespace(
            strategy="time_block",
            time_col="time",
            min_train_times=3,
            n_splits=3,
        )
        frame["fold"] = -1
        for fold, validation_index in module.iter_custom_splits(frame, args):
            frame.loc[validation_index, "fold"] = fold
        parsed = pd.to_datetime(frame["time"])
        fold_specs = []
        for fold in range(3):
            val_times = parsed.loc[frame["fold"] == fold]
            cutoff = val_times.min()
            fold_specs.append(
                {
                    "fold": fold,
                    "train_max_time": parsed.loc[parsed < cutoff].max().isoformat(),
                    "val_time_min": cutoff.isoformat(),
                    "val_time_max": val_times.max().isoformat(),
                }
            )
        manifest = {"strategy": "time_block", "time_column": "time", "folds": fold_specs}

        train, validation = module.select_time_block_split(frame, manifest, 0)
        self.assertEqual(set(train["fold"]), {-1})
        self.assertEqual(set(validation["fold"]), {0})
        self.assertTrue((pd.to_datetime(train["time"]) < pd.Timestamp(fold_specs[0]["val_time_min"])).all())
        self.assertFalse(set(train["ID"]).intersection(set(frame.loc[frame["fold"] > 0, "ID"])))


if __name__ == "__main__":
    unittest.main()
