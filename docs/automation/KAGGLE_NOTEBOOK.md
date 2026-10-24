# Kaggle Notebookを使う分析PDCA

## 責務

local controllerはresearch、fold、hypothesis、data/code/config/split hash、台帳、採否を保持し、private Kaggle Notebookはcompetition dataを直接読むEDA、前処理、学習、評価、最終推論を担当します。

Colab連携済みという申告、Kaggle quota画面の残時間、Notebook runtimeで実測したGPU/RAM/diskは別証拠です。前者だけから後二者を推測しません。

## 初期化

```bash
cp automation/kaggle_notebook.template.json automation/kaggle_notebook.json
mkdir -p notebooks/kaggle/analysis-pdca state/kaggle/runs
```

competition、owner、slug、Notebook pathを埋め、task固有のruntime/data probe Notebookを作成します。credentialはrepoへ保存せず、OAuthまたはKaggle CLIの標準credential storeを使います。

```bash
kaggle auth login
PYTHONPATH=. python3 tools/kaggle_notebook.py --root . auth-check
PYTHONPATH=. python3 tools/kaggle_notebook.py --root . render-metadata
```

## 実行契約

- Notebookの公開範囲、accelerator、Internet設定は公式規約と実験roleに合わせる。GPUやofflineを全タスクの既定値にしない。
- 競技の公式runtime上限から、学習後の検証を含めて余裕を残す。
- checkpoint間隔はcompute profileと実測runtimeに従う。
- exact quotaとreset時刻を取得時刻付きで記録する。
- runtime/data probeを回収してからcompute backendを`available=true`にする。
- 同一仮説、seedだけの変更、既知のcode failureを再実行しない。
- outputはrun IDごとの新規directoryへ回収し、SHA-256 manifestを生成する。
- 高コストのjob投入前に、利用可能なら安価なprobeで実workerの入力rosterを取得する。task固有のfile/directory形式を宣言し、事前配置が必要な全stageを`worker-input-check`で検証する。規約・deadline・所要時間に照らし、全stageを完了できない計画は投入しない。

```bash
PYTHONPATH=. python3 -m tools.competition_automation worker-input-check \
  automation/worker_inputs.json --root /path/to/materialized/input
```

このcheckは実際に配置されたworker filesystemで実行する。ローカルの推定rosterをPASSさせても遠隔環境の証拠にはならない。`minimum_count`は観測済みrosterに合わせて設定し、stageの入力契約変更時はprobeを更新する。`compute_request.timeline`には公式締切snapshotと学習後の採点・検証・提出に要する時間を固定し、全critical pathが締切に収まらなければ`compute-plan`を停止する。部分checkpointは`validated`に昇格せず、全required outputs・training/split manifest・runtime integrityを照合する。

remote実行はbundleを確認してから明示します。

```bash
PYTHONPATH=. python3 tools/kaggle_notebook.py --root . push --confirm-push
PYTHONPATH=. python3 tools/kaggle_notebook.py --root . status
PYTHONPATH=. python3 tools/kaggle_notebook.py --root . logs
PYTHONPATH=. python3 tools/kaggle_notebook.py --root . collect-output --run-id expA00-v1
```

`collect-output`は設定の`required_outputs`を検査し、不足時はFAILにします。実行完了だけを精度改善や提出候補の証拠として扱いません。
