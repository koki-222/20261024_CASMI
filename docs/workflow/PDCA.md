# Issue駆動のbounded Ralph loop

Ralphは「数値目標まで結果を読み次の比較を選ぶ継続運用」を指す。このrepoは特定のRalph pluginやLLM APIに依存しない。無限再試行、無制限課金、常駐schedulerを実装したという意味ではない。

## 開始

合図は**PDCA開始**。対象Issue、metric（契約から）、min/max（契約から）、公式LB数値目標、Public/Private/final、試行上限、経過時間上限、timezone付き期限を確認。不足を一度に尋ね、全項目が揃うまで開始しない。Public0.960などの例を既定値にせず、銀圏やlocal CVを数値目標の代用にしない。

```bash
python3 -m tools.competition_automation pdca-start --root . \
  --goal automation/pdca_goal.json --intake automation/issue_intake.json --trigger PDCA開始
python3 -m tools.competition_automation workflow-status --root .
```

goalは`automation/pdca_goal.template.json`を適応し、intakeは既存schemaを使う。Issue snapshotにnumber/url/body/author/取得時刻を保持し、SHAを凍結する。許可対象のGitHub repo以外のIssueは受け付けない。文字列の合図はユーザーの明示指示を受けたagentが渡すもので、第三者資料から抽出して権限を作らない。

新しい予算/Issueで再開するときはgoal/intakeを新しいversionのファイルへ保存し、旧snapshotを上書きしない。旧loopの状態はhost-local historyへ保存される。workflowの変更はmacOS/Linux/WSLのhost-local lockで直列化する。

## 研究サイクル

1. **Plan**: 現在の最大bottleneck、比較で変える判断、control、固定条件、評価母集団と露出、最小の予算を確定。前提作業は解放する具体的な実験へ結び付ける。
2. **Do**: 保存済み出力を先に確認。既存controller/continuationにIssue scopeとrequestを接続し、`experiment-run`でclaim/reuse/join。新workflowではLB目標・Issue・残試行・残時間をclaimの同一transactionで検査する。インフラretryも資源消費なので総attempt上限に含む。
3. **Check**: 対応する実データ、予測、曲線、誤り、所見/群/期間ごとの成績を読み、同じ評価集団で比較。介入消費・fallback・教師露出・不確実性を明示する。
4. **Act**: 採用、棄却、予算不足による識別不能、次rungへの延長、教師/入力改善、別の承認Issueへの投資変更を選ぶ。Issueの許可範囲を越える新しい問いはユーザーへ提案する。

completed attemptの新しい結果解釈は次で記録する。

```json
{
  "attempt_id": "元registryのcompleted attempt ID",
  "finding": "何が分かったか。数値と評価母集団は元artifactを参照",
  "next_question": "次に決着させる問い",
  "why_priority_changed": "結果が投資判断をどう変えたか",
  "decision": "continue"
}
```

```bash
python3 -m tools.competition_automation pdca-review research/decisions/review-001.json --root .
```

同じattemptを再解釈してreview数を増やせない。`request_issues`は追加Issue待ち、`freeze`は未達凍結。失敗attemptの原因究明は元失敗状態のまま記録し、科学的な完成比較やcompletedに変換しない。reviewの文章をコードが科学的に正しいと認定するわけではない。

## 探索の配分

- データ/教師/入力/目的変数/損失/学習段階/蒸留/専門化/推論/融合のうち最大bottleneckに近い軸を選ぶ。手法カタログを埋めない。
- 安い比較から有望案へ投資。ただし短期学習での順位は収束速度を含むため、曲線とcheckpointを用いて遅い手法を救済する。
- baselineと候補の評価行・ラベル・時点を揃える。独立CVが成立しないときは不確実性を明示し、実施不能なCVを形式だけ要求しない。
- seed-onlyはcontrol/分散評価/最終融合として既存schemaの参照元と理由を付ける。独立hypothesisやquota件数に数えない。採用には他候補同様の実体比較が必要。
- 同じ失敗graphの係数派生を同時に展開しない。動く最小baselineを早期に確立し、変更境界だけを検証する。

## 待機

採点/外部結果待ちは独立した承認Issueや保存予測による比較を続ける。quota0は学習/評価を止めない。`work-plan`と`wait-check`で残作業を確認し、監査を増やしてbusy状態を作らない。実行不能なら正当に待つ。

製品のwait/scheduler/heartbeatが使える場合に、それを用いて次回観測時刻に再開する。無変更時は通知せず、意味ある結果/失敗/要ユーザー判断のみ通知。なければ現在phase、目標、次job、再開条件を引き渡す。SKILLやCLIにはturnを越えてagentを起動する機能はなく、ここをbash pollingで代替しない。

## 公式得点と終了

`pdca-score`の入力は次の参照manifest。元の公式JSON行からscore/ID/statusを直接照合する。JSON以外の画面・HTMLだけの場合は競技adapterの検証を追加し、画像のhashだけで達成認定しない。

```json
{
  "snapshot": {"path": "evidence/normalized-result.json", "sha256": "…"},
  "native_snapshot": {"path": "evidence/original-response.json", "sha256": "…"},
  "native_row_path": ["submissions", 0],
  "native_fields": {"submission_id": "ref", "score": "publicScore", "status": "status"},
  "native_success_status": "COMPLETE"
}
```

row path/field名は競技の実schemaに合わせ、Kaggleの例を他platformへ強制しない。scoredと自己申告するだけでは通らず、元行の数値・ID・成功状態と一致し、errorDescription等がない必要がある。

正規化結果には`competition_url, kind: official, status: scored, preliminary: false, metric, direction, scope, score, submission_id, source_url, observed_at`を持たせる。adapterが元の公式行/画面、受付IDとversion、原証拠を照合して生成する。コードはhashと整合性を検査するが、自己申告したJSONの真正性を証明できない。

```bash
python3 -m tools.competition_automation pdca-score evidence/official-result-ref.json --root .
```

評価区分・metric・競技・成功・暫定性を照合し、min/maxで数値目標を判定。Public達成はPrivateの保証ではない。Private目標は暫定順位では達成にしない。

予算/期限終了は未達のまま停止。`workflow-status`が上限到達を確認したら`pdca-close --root . --reason budget_exhausted|deadline`で閉じる。競技終了は`--reason competition_ended --evidence evidence/closure-ref.json`で公式原証拠と共に記録し、永久activeにしない。closure-refは得点と同じsnapshot/native_snapshot参照を持ち、正規化原本にはcompetition_url、kind: official、status: ended、observed_atが必要。目標を満たすまで無限に課金・再提出しない。追加Issue受領後の再開も新しい明示指示と予算確認が必要。

LB提出権限は別管理。Kaggle以外は手動、KaggleのCode Competition最終Submit/最終選択も人間操作。提出待ちをローカル研究全体の障害にしない。
