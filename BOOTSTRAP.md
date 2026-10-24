# CASMI26 bootstrap

既存READMEとGit履歴を保った新しい作業先。競技URLはREADME、入口はAGENTS.md。

1. [公式仕様・例外・未確認事項](research/OFFICIAL_SUMMARY.md)
2. [公開手法の系統と限界](research/SOURCE_MAP.md)
3. [評価設計](automation/evaluation_design.json)
4. [移植・依存・検証](research/ADAPTATION.md)
5. [固定package](research/bootstrap_package.json)
6. [収集範囲](research/COVERAGE.md)
7. [過去実験の失敗・限界と再利用条件](research/LEGACY_REVIEW.md) — Issue整理前に読む。

read-onlyな形式確認は次のコマンドで行う。sampleを予測精度の証拠にしない。

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements-bootstrap.lock
.venv/bin/python -m tools.casmi_contract --test datasets/raw/casmi26/test.parquet --submission datasets/raw/casmi26/sample_submission.csv
.venv/bin/python -m unittest discover -s tests -p test_casmi_contract.py
.venv/bin/python -m unittest discover -s tests -p test_workflow.py
```

公式配布ファイルはルールへ同意済みの参加者が正規のKaggle機能で取得する。Gitには含めない。trainの旧archive参照はhost-localな`.local/data_reuse.json`に記録し、新作業先へ3GBを複製していない。
