---
name: submit-check
description: 提出物の事前検証を実行する。Kaggle CSV / Kaggle Code Competition / 予測ファイル zip / Docker コンテナ / Simulation エージェントの5形式に対応。submission-validator agent を呼び出して機械的にチェックする。提出直前に必ず使う。
argument-hint: "<submit/v00X_<元exp>_xxx のパス> [--platform kaggle|gc|codabench]"
---

# 提出物 事前検証 Skill

## 手順

1. **引数解析**:
   - `$ARGUMENTS` から提出フォルダパスを取得（必須、例: `submit/v001_baseline`）
   - `--platform` フラグがあればそれを使う、なければ `KAGGLE_DIRECTION.md` から推定

2. **形式判定**:
   - フォルダ内のファイル構成から判定:
     - `notebook.py` あり → (A) Kaggle CSV型
     - `kernel-metadata.json` + `dataset-metadata.json` あり → (A') Kaggle Code Competition 型
     - `predict.py` + `run.sh` あり → (B) 予測ファイル zip 型
     - `Dockerfile` + `process.py` あり → (C) Docker コンテナ型
     - `agent.py` / `main.py` / `submit_agent.py` 等のエージェントエントリがあり、上記いずれにも当てはまらない → (D) Simulation エージェント型

3. **決定的な共通ゲートを先に実行**:
   - `automation/contracts/` に対象契約がある場合は、LLM agentより先に
     `PYTHONPATH=. python3 -m tools.competition_automation submission-check <contract> <artifact> [--sample <sample>] [--ledger automation/ledger.jsonl]`
     を実行する
   - 契約が見つからない場合は「契約未登録」として Warning/Block にし、形式を推測してPASSにしない
   - `fingerprint` と台帳の重複チェックを行い、同一SHAの再包装を新候補として扱わない。`--ledger` 指定時に既存SHAならFailにする
   - このゲートのFailは、LLM agentの文章や「完走した」という報告で上書きしない
   - `submission-plan`も実行し、`upload_mode=manual`なら`READY_FOR_MANUAL_UPLOAD`として人間へ引き渡す。これはローカルPASSであり、外部LB提出を試みてはいけない

4. **submission-validator agent を呼び出し**:
   - 上で判定した形式と提出フォルダパスを渡して実行依頼
   - agent は自動で形式に応じた検証を行う
   - (A') の場合は submission-validator の「1'. Kaggle Code Competition 型のチェック」（kernel-metadata / dataset-metadata / self-contained notebook の検証）を実行させる
   - (D) の場合は submission-validator の「(D) Simulation エージェント型のチェック」（self-contained / 1エピソード完走 / タイムアウト・不正手・print 混入の検証）を実行させる

5. **検証結果を集約して報告**:
   - Pass / Fail / Warning を上から順に表示
   - Fail があれば**提出してはいけない**旨を強調
   - 修正案を最後に提示
   - `manual`契約で外部LBが未取得でもFailにしない。提出物は`ready`、外部状態は`manual_pending`として記録する

6. **submit/SUBMISSIONS.md への記録準備**:
   - メタ情報（実験フォルダ・モデル元パス・fold 定義・学習データ・主要パラメータ・前処理・CV）を agent の出力から抽出
   - SUBMISSIONS.md にまだ記載がなければ、追記用の行を提案する（**自動コミットはしない**）

## 注意

- **絶対に外部にアップロードしない**。実アップロードはユーザーが手動で行う
- **Kaggle以外ではagentがLB/API提出を絶対に試みない**。Zindi、SIGNATE、grand-challenge、CodaBench、独自サイトは人間提出が既定であり、提出エラーを自動リトライしない
- 検証中にモデルファイル / submission.csv / 予測 zip を `.gitignore` から外していないか確認
- Docker 検証では `bash test.sh` を実行する。長時間かかる場合は run_in_background で回す
