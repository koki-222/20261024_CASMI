#!/usr/bin/env python3
"""fold 割り当て生成スクリプト（全実験で共有する folds.csv を作る）.

KFold / StratifiedKFold / GroupKFold / StratifiedGroupKFold を引数で選び、
`workspace/fold/{version}/folds.csv` に出力する。
設計意図・バージョニング規約は同階層の README.md と CLAUDE.md「Fold設計（最重要）」を参照。

使い方:
    # ランダム KFold（最後の手段。まずデータの性質を確認すること）
    python generate_folds.py --input ../../datasets/train.csv \\
        --version v1_random_kfold5 --strategy kfold --id-col image_id

    # クラス不均衡 → StratifiedKFold
    python generate_folds.py --input ../../datasets/train.csv \\
        --version v1_stratified_label --strategy stratified \\
        --id-col image_id --target-col label

    # グループ構造あり → GroupKFold
    python generate_folds.py --input ../../datasets/train.csv \\
        --version v1_group_patient --strategy group \\
        --id-col image_id --group-col patient_id

    # グループ + 不均衡 → StratifiedGroupKFold
    python generate_folds.py --input ../../datasets/train.csv \\
        --version v1_sgkf_patient_label --strategy stratified_group \\
        --id-col image_id --target-col label --group-col patient_id
"""

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd
from sklearn.model_selection import (
    GroupKFold,
    KFold,
    StratifiedGroupKFold,
    StratifiedKFold,
)

log = logging.getLogger(__name__)

# strategy 名 → (splitter クラス, target 必須か, group 必須か)
# ``time_block`` は通常の sklearn splitter ではなく、過去のみを学習に
# 使うカスタム割り当てを ``iter_custom_splits`` で生成する。
STRATEGIES = {
    "kfold": (KFold, False, False),
    "stratified": (StratifiedKFold, True, False),
    "group": (GroupKFold, False, True),
    "stratified_group": (StratifiedGroupKFold, True, True),
    "time_block": (None, False, False),
}

# 出力先はスクリプト基準で組む（cwd 依存禁止。CLAUDE.md「学習コードの鉄則」と同じ流儀）
FOLD_ROOT = Path(__file__).resolve().parent


def setup_logging(out_dir: Path) -> None:
    """コンソール（INFO）とファイル（DEBUG）の両方に出力する。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir / f"generate_folds_{datetime.now():%Y%m%d_%H%M%S}.log"

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(fh)
    root_logger.addHandler(ch)

    log.info(f"Log file: {log_file}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fold 割り当てを生成して workspace/fold/{version}/folds.csv に保存する")
    parser.add_argument("--input", required=True, help="入力 CSV のパス（train メタデータ）")
    parser.add_argument("--version", required=True, help="バージョン名（例: v1_group_patient）。出力先ディレクトリ名になる")
    parser.add_argument("--strategy", required=True, choices=sorted(STRATEGIES), help="fold の切り方")
    parser.add_argument("--id-col", required=True, help="サンプルを一意に識別するキー列名（例: image_id）")
    parser.add_argument("--target-col", default=None, help="stratify に使う列名（stratified / stratified_group で必須）")
    parser.add_argument("--group-col", default=None, help="グループ列名（group / stratified_group で必須）")
    parser.add_argument("--n-splits", type=int, default=5, help="fold 数（デフォルト: 5）")
    parser.add_argument("--seed", type=int, default=42, help="乱数シード（デフォルト: 42）")
    parser.add_argument(
        "--time-col",
        default=None,
        help="time_block で時系列順を決める列名。日時として解釈できる必要がある",
    )
    parser.add_argument(
        "--min-train-times",
        type=int,
        default=1,
        help="time_block で各 validation block の前に必要な最小学習時点数",
    )
    return parser.parse_args()


def make_splitter(strategy: str, n_splits: int, seed: int):
    cls, _, _ = STRATEGIES[strategy]
    if strategy == "group":
        # GroupKFold は shuffle / random_state 非対応（決定的な切り方になる）
        log.info("GroupKFold は shuffle 非対応のため seed は使われない")
        return cls(n_splits=n_splits)
    return cls(n_splits=n_splits, shuffle=True, random_state=seed)


def validate_args(args: argparse.Namespace, df: pd.DataFrame) -> None:
    """列の存在と strategy の必須引数をチェックする。"""
    _, need_target, need_group = STRATEGIES[args.strategy]
    if args.n_splits <= 0:
        raise SystemExit("--n-splits は正の整数で指定すること")
    if args.strategy == "time_block":
        if not args.time_col:
            raise SystemExit("--strategy time_block には --time-col が必須")
        if args.time_col not in df.columns:
            raise SystemExit(f"列 '{args.time_col}' が入力 CSV に存在しない")
        if args.min_train_times <= 0:
            raise SystemExit("--min-train-times は正の整数で指定すること")
    if need_target and args.target_col is None:
        raise SystemExit(f"--strategy {args.strategy} には --target-col が必須")
    if need_group and args.group_col is None:
        raise SystemExit(f"--strategy {args.strategy} には --group-col が必須")

    for col in filter(None, [args.id_col, args.target_col, args.group_col]):
        if col not in df.columns:
            raise SystemExit(f"列 '{col}' が入力 CSV に存在しない。列一覧: {list(df.columns)}")

    if df[args.id_col].duplicated().any():
        raise SystemExit(f"キー列 '{args.id_col}' に重複がある。一意な列を指定すること")


def iter_custom_splits(df: pd.DataFrame, args: argparse.Namespace) -> Iterator[tuple[int, pd.Index]]:
    """Yield validation blocks for strategies that are not sklearn splitters.

    ``time_block`` keeps the earliest observations as train-only rows (fold
    ``-1``) and partitions only future validation periods.  This makes the
    expanding-window semantics explicit instead of silently using future rows
    in a random fold.
    """
    if args.strategy != "time_block":
        return
    times = pd.to_datetime(df[args.time_col], errors="raise")
    unique_times = pd.Index(sorted(times.unique()))
    required = args.min_train_times + args.n_splits
    if len(unique_times) < required:
        raise SystemExit(
            f"time_block に必要な時点数が不足: {len(unique_times)} < {required}"
        )
    validation_times = unique_times[args.min_train_times :]
    blocks = [block for block in np.array_split(validation_times, args.n_splits) if len(block)]
    if len(blocks) != args.n_splits:
        raise SystemExit("time_block の validation block 数を作れない")
    for fold, block in enumerate(blocks):
        val_mask = times.isin(block)
        val_index = df.index[val_mask]
        train_times = unique_times[unique_times < block[0]]
        if len(train_times) < args.min_train_times:
            raise SystemExit(f"fold {fold}: 学習時点数が不足")
        yield fold, val_index


def select_time_block_split(
    folds: pd.DataFrame,
    manifest: dict[str, object],
    val_fold: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return a leak-safe expanding-window train/validation pair.

    Consumers must use this helper (or reproduce its cutoff predicate) for a
    ``time_block`` manifest.  ``fold != val_fold`` is intentionally not a
    valid substitute because it would include rows from later validation
    periods in the training set.
    """
    if manifest.get("strategy") != "time_block":
        raise ValueError("manifest is not a time_block split")
    time_column = manifest.get("time_column")
    if not isinstance(time_column, str) or time_column not in folds.columns:
        raise ValueError("time_block manifest time_column is missing from folds.csv")
    fold_specs = manifest.get("folds")
    if not isinstance(fold_specs, list):
        raise ValueError("time_block manifest does not contain fold cutoffs")
    spec = next(
        (item for item in fold_specs if isinstance(item, dict) and item.get("fold") == val_fold),
        None,
    )
    if not isinstance(spec, dict):
        raise ValueError(f"unknown validation fold: {val_fold}")
    cutoff = spec.get("val_time_min")
    if not isinstance(cutoff, str) or not cutoff:
        raise ValueError("time_block fold is missing val_time_min")
    times = pd.to_datetime(folds[time_column], errors="raise")
    validation = folds.loc[folds["fold"] == val_fold].copy()
    training = folds.loc[times < pd.Timestamp(cutoff)].copy()
    if training.empty or validation.empty:
        raise ValueError(f"fold {val_fold} has an empty train or validation partition")
    if bool((pd.to_datetime(training[time_column], errors="raise") >= pd.Timestamp(cutoff)).any()):
        raise ValueError(f"fold {val_fold} training rows violate the time cutoff")
    return training, validation


def log_fold_distribution(df: pd.DataFrame, args: argparse.Namespace) -> None:
    """各 fold の件数・ターゲット分布・グループ非重複を記録する（SESSION_NOTES 転記用）。"""
    assigned = df[df["fold"] >= 0]
    train_only = int((df["fold"] < 0).sum())
    log.info(f"fold ごとの件数:\n{assigned['fold'].value_counts().sort_index().to_string()}")
    if train_only:
        log.info("train-only rows (fold=-1): %d", train_only)
    if args.target_col:
        dist = assigned.groupby("fold")[args.target_col].value_counts(normalize=True).unstack(fill_value=0)
        log.info(f"fold ごとの '{args.target_col}' 分布:\n{dist.round(4).to_string()}")
    if args.group_col:
        # 同一グループが複数 fold に跨っていないか（リーク検査）
        n_leaky = (df.groupby(args.group_col)["fold"].nunique() > 1).sum()
        if n_leaky > 0:
            raise SystemExit(f"グループ '{args.group_col}' が複数 fold に跨っている: {n_leaky} 件（リーク）")
        log.info(f"グループ '{args.group_col}' の fold 間リークなし（{df[args.group_col].nunique()} グループ）")


def main() -> None:
    args = parse_args()

    out_dir = FOLD_ROOT / args.version
    out_csv = out_dir / "folds.csv"
    # 既存バージョンは上書き禁止（過去実験の再現性のため。切り直すなら新バージョンを作る）
    if out_csv.exists():
        raise SystemExit(f"{out_csv} は既に存在する。上書きせず新しい --version を指定すること")

    setup_logging(out_dir)
    log.info(f"Args: {vars(args)}")

    df = pd.read_csv(args.input)
    log.info(f"入力: {args.input} ({len(df)} 行)")
    validate_args(args, df)

    y = df[args.target_col] if args.target_col else None
    groups = df[args.group_col] if args.group_col else None

    df["fold"] = -1
    if args.strategy == "time_block":
        split_records = iter_custom_splits(df, args)
        for fold, val_index in split_records:
            df.loc[val_index, "fold"] = fold
    else:
        splitter = make_splitter(args.strategy, args.n_splits, args.seed)
        for fold, (_, val_idx) in enumerate(splitter.split(df, y, groups)):
            df.loc[df.index[val_idx], "fold"] = fold
    if args.strategy != "time_block" and (df["fold"] < 0).any():
        raise SystemExit("fold 未割り当ての行がある")
    if (df["fold"] >= 0).sum() == 0:
        raise SystemExit("validation fold に割り当てられた行がない")

    log_fold_distribution(df, args)

    # 学習側は id で join する。time_block では時刻も保存し、普通の
    # ``fold != val_fold`` が未来行を混ぜることを明示的に防ぐ。
    keep_cols = [args.id_col] + [c for c in [args.target_col, args.group_col] if c]
    if args.strategy == "time_block":
        keep_cols.append(args.time_col)
    keep_cols.append("fold")
    df[keep_cols].to_csv(out_csv, index=False)
    fold_manifest: list[dict[str, object]] = []
    if args.strategy == "time_block":
        parsed_times = pd.to_datetime(df[args.time_col], errors="raise")
        for fold in range(args.n_splits):
            val_times = parsed_times.loc[df["fold"] == fold]
            if val_times.empty:
                raise SystemExit(f"fold {fold}: validation rows are empty")
            val_min = val_times.min()
            val_max = val_times.max()
            train_mask = parsed_times < val_min
            train_times = parsed_times.loc[train_mask]
            if train_times.empty:
                raise SystemExit(f"fold {fold}: training rows are empty")
            fold_manifest.append(
                {
                    "fold": fold,
                    "train_max_time": train_times.max().isoformat(),
                    "val_time_min": val_min.isoformat(),
                    "val_time_max": val_max.isoformat(),
                    "train_rows": int(train_mask.sum()),
                    "validation_rows": int((df["fold"] == fold).sum()),
                }
            )
    else:
        fold_manifest = [
            {
                "fold": fold,
                "train_rows": int((df["fold"] != fold).sum()),
                "validation_rows": int((df["fold"] == fold).sum()),
            }
            for fold in range(args.n_splits)
        ]
    metadata = {
        "schema_version": 1,
        "strategy": args.strategy,
        "time_column": args.time_col if args.strategy == "time_block" else None,
        "args": vars(args),
        "input": str(Path(args.input).resolve()),
        "input_sha256": hashlib.sha256(Path(args.input).read_bytes()).hexdigest(),
        "row_count": len(df),
        "fold_counts": {
            str(int(key)): int(value)
            for key, value in df["fold"].value_counts().sort_index().items()
        },
        "train_only_rows": int((df["fold"] < 0).sum()),
        "folds": fold_manifest,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log.info(f"保存: {out_csv}")
    log.info(f"保存: {out_dir / 'manifest.json'}")
    log.info(f"config.yaml には cv.folds_csv: workspace/fold/{args.version}/folds.csv を指定する")


if __name__ == "__main__":
    main()
