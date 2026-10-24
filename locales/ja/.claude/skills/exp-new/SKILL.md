---
name: exp-new
description: 競技契約とユーザーIssueに結び付いた実験フォルダを、選択したreferenceから作成する。SESSION_NOTES.mdとrun.shを生成する。
argument-hint: "<実験名> [--human] [--reference reference_generic|reference|reference_sim]"
---

# 新規実験フォルダ作成Skill

## 手順

1. `$ARGUMENTS`から実験名、`--human`、任意の`--reference`を取得する。指定がない場合は依存のない`reference_generic/`を使う。DL、simulation、RL用のreferenceは、Issueと競技契約が明示的に適合するときだけ選択する。referenceの選択は仮説の代わりにならない。
2. 同一リポジトリの`research/manifest.json`、contract、controller action、対象Issue snapshotを確認する。`preflight`または`controller-check`がPASSでなく、Issueに検証可能な予測・対照・停止条件がない場合は作成を中止する。

```bash
PYTHONPATH=. python3 -m tools.competition_automation research-check \
  research/manifest.json --root . --contract automation/contracts/template.json
PYTHONPATH=. python3 -m tools.competition_automation controller-check \
  automation/controller_state.json --root .
```

3. `workspace/`を確認し、agent用は`expA00_<name>`、人間用は`exp200_<name>`から未使用の番号を選ぶ。同名フォルダや同一computation signatureがある場合は新規作成しない。
   初回実行前に`automation/issue_intake.json`と`automation/continuation.json`を初期化し、対象scope・`kind: hypothesis`の共有queue job・凍結requestを対応付ける。`work-plan`で選択を確認し、実行後や待機前は`wait-check`へ進む。queueが空なだけで検証枯渇と判断しない。
4. 選択したreferenceをコピーする。既定の`reference_generic/`はrequest-firstで、モデル・データ・metricを選択しない。付属の線形回帰demoは統合テスト専用の明示opt-inであり、GPU/AMPやDLが不要なタスクへ`reference/`を自動コピーしない。

```bash
cp -r reference_generic/ workspace/<新フォルダ名>/
```

5. `SESSION_NOTES.md`を作成する。Issue番号・snapshot SHA、hypothesis/computation/attemptの識別子、変更するmechanism/input axis、baseline、selection/stress評価、予算、停止条件、再開方法を記録する。

```markdown
# SESSION_NOTES — <実験フォルダ名>

## セッション情報
- 日付: YYYY-MM-DD
- 作業フォルダ: workspace/<実験フォルダ名>
- source Issue: #<number> / <snapshot SHA>
- hypothesis_id / computation_id / attempt_id: ...
- 目標: ...

## 計画
- mechanism/input axis: ...
- baseline: ...
- selection/stress profile: ...
- budget / stopping condition: ...
- resume/reuse plan: ...

## 結果
<!-- 数値、評価母集団、uncertainty、失敗check、artifact SHAを追記 -->

## 次のステップ
- [ ] ...
```

6. `run.sh`は実験ディレクトリへ移動し、直接training scriptを呼ばず、Issue-bound requestを`experiment-run`へ渡す。requestが未生成なら停止する。launcherを呼び出す前に、実験入口の原子的claimと`fidelity-check`を通す。長いscore待ちをrun scriptへ入れない。

```bash
#!/usr/bin/env bash
set -euo pipefail
EXP_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(git -C "$EXP_DIR" rev-parse --show-toplevel)"
EXP_NAME="$(basename "$EXP_DIR")"
REQUEST="${REQUEST_PATH:-automation/requests/${EXP_NAME}.json}"
test -f "$ROOT/$REQUEST"
PYTHONPATH="$ROOT" python3 -m tools.competition_automation experiment-run \
  "$ROOT/$REQUEST" --root "$ROOT" --registry "$ROOT/automation/runs.sqlite"
```

7. 学習を行う場合、checkpointはmodelだけでなくoptimizer、scheduler、AMP scaler、乱数状態、sampler位置、config、data/code snapshotを、実装が使用する範囲で保存する。CPU/表形式/シミュレーションでは該当しない状態を明記する。wall timeで止まった場合は`budget_exhausted_inconclusive`とし、途中結果だけで手法を採否しない。
8. 完了後は`experiment-run`が記録した状態、metrics、checks、artifact SHAを確認し、必要なら`validation-plan`、`exploration-check`、`state-compact`を実行する。提出候補は`submission-check`と人間handoffを経て初めて`ready`になる。

## 注意

- 新しいseed、閾値、再包装、同じcheckpointの再実行は新しい仮説ではない。元computationを参照するcontrol/attemptとして記録する。
- 完了済みcomputationは再利用し、実行中はjoinする。`infra_failed`/`preempted`以外の再実行には新しいIssueとmechanism/input signatureが必要。
- 仮説が足りない場合にagentがIssueやRL案を自動生成して埋めない。`WAITING_FOR_USER_ISSUE`または`blocked`として返す。
- 実験フォルダへdatasets、model、credential、巨大artifactをコピーしない。生成物はcontractと`.gitignore`に従う。
