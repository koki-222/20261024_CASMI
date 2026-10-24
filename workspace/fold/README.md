# workspace/fold/ — fold 割り当ての永続化

## 設計意図

- **fold 割り当ては 1 箇所で生成・固定し、全実験で同じ `folds.csv` を共有する**
  - 実験ごとに fold を切り直すと CV スコアが比較不能になり、リークの温床にもなる
  - 学習コードは `folds.csv` をキー列で join して train/val を分けるだけにする
- 切り方の選定基準は `CLAUDE.md` の「Fold設計（最重要）」を参照（安易にランダム KFold にしない）
  - 時系列 → `time_block`（expanding-window）。複雑なpurge/embargoは競技固有スクリプトで実装する
  - グループ構造あり → GroupKFold
  - クラス不均衡 → StratifiedKFold
  - グループ + 不均衡 → StratifiedGroupKFold
- fold 設計の理由と各 fold の分布は実験の `SESSION_NOTES.md` に記録する（`generate_folds.py` のログをそのまま転記してよい）

## 使い方

```bash
cd workspace/fold

# 例: グループ + 不均衡 → StratifiedGroupKFold
python generate_folds.py --input ../../datasets/train.csv \
    --version v1_sgkf_patient_label --strategy stratified_group \
    --id-col image_id --target-col label --group-col patient_id
```

### 時系列の expanding-window 割り当て

行のランダム分割が不適切なタスクでは、`time_block` を使う。最初の
`--min-train-times` 時点は train-only（`fold=-1`）として残し、以降を未来順の
validation blockへ分ける。生成先には `folds.csv` と `manifest.json`（引数、入力SHA-256、
fold件数、train-only件数）を保存する。

```bash
python generate_folds.py --input ../../datasets/train.csv \
    --version v1_time_block --strategy time_block --id-col ID \
    --time-col time --n-splits 4 --min-train-times 12
```

`fold=-1` は「どのvalidationにも入れない過去学習行」を表す。`time_block` の
`folds.csv`には時刻列が保存され、`manifest.json`の各foldに
`train_max_time`/`val_time_min`/`val_time_max`が記録される。学習コードは
`select_time_block_split`（または同じ `time < val_time_min` 条件）を使うこと。
`fold != val_fold`だけで学習行を作ると未来foldが混入するため禁止する。ROGIIの
well suffix、NEDOのexact replay、GeoAIのnested/domain分割は、競技固有の評価器で
実装し、このCSVをランダムKFoldの代用にしない。

- 引数の詳細は `python generate_folds.py --help` / `generate_folds.py` 冒頭の docstring を参照
- シードはデフォルト 42 で固定（`--seed` で変更可。変えたら別バージョンにする）
- 学習側は config.yaml の `data.folds_csv` で使用バージョンを指定する:

```yaml
data:
  folds_csv: workspace/fold/v1_sgkf_patient_label/folds.csv
```

## バージョニング規約

- バージョン名は `v1_xxx` 形式（連番 + 切り方が分かる短い識別子）
  - 例: `v1_random_kfold5` / `v2_group_patient` / `v3_sgkf_patient_label`
- **前処理やデータを変更したら新バージョンを作る。古いバージョンは削除しない**（過去実験の再現性のため）
  - `generate_folds.py` も既存バージョンへの上書きを拒否する
- ディレクトリ構成:

```
workspace/fold/
├── README.md
├── generate_folds.py
├── v1_xxx/
│   ├── folds.csv                          # キー列 + (target/group 列) + [time] + fold
│   └── generate_folds_YYYYMMDD_HHMMSS.log # 生成時の引数・分布ログ
└── v2_yyy/
    └── ...
```

## 検証

- 生成ログで各 fold の件数・ターゲット分布・グループ非重複（リーク検査）を確認する
- CV/LB の相関を確認し、相関が弱ければ fold 設計を見直して**新バージョン**を作る
