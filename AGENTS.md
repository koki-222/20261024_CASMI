# Competition workspace rules

## このCASMI26移植先の境界

- 正本は`/Users/kokiimai/Documents/2026_workspace/2_Dev/20261024_CASMI`。`automation/project.json`の競技と`automation/contracts/current.json`を使う。
- 既存の`202609_CASMI`は参照資料・配布データの読取元だけであり、変更、再初期化、ジョブ観測、旧controller・提出・timerの操作をしない。旧Issue、実行権限、LB目標、CPU限定方針を継承しない。
- bootstrap中は学習、公開Notebook実行、仮説起案、LB提出を行わない。今回のGit公開権限を将来の実験・提出権限に一般化しない。
- 現行公開metricはRDKit2026.03.3でstereo除去→tautomer canonicalization→InChIKey14照合。公開版と実際のcompetition deployment pinの同一性は未確認。`research/OFFICIAL_SUMMARY.md`の限界を保持する。
- trainには`molecule_id`/`spectrum_id`がない。物理行IDとcanonical componentを区別し、同分子・転載スペクトルをまたぐrow-random splitを採用しない。
- 配布testの一致率は性能評価に使わない。入力IDを実行時に取得し、分子あたり1行・候補1〜25件の`submission.csv`を動的に作る。
- 外部weightはcode licenseと分け、exact version、元のtraining code、許可されたtraining data、再現recipe、評価集合への露出を確認する。NISTおよび禁止された派生重みを使用しない。
- 収集原本は`.local/restricted/`と既存archiveへ保存。再配布未確認の原文・データ・重み・会話履歴はpublic Gitへ含めない。Kaggle429後は追加取得を停止したため、inventoryのmissing/staleを完全と呼ばない。
- 各種コマンドはこの移植先をcwd/`--root`に固定する。`requirements-bootstrap.txt`の環境でread-only smokeを行う。モデル依存は採択Issueで必要になってから追加する。
- Issue起案・比較設計前に`research/LEGACY_REVIEW.md`、`research/legacy/reuse_policy.json`、`research/legacy/exposure.json`を読む。旧結果は歴史的証拠として使い、旧Issue承認・実行権限は移植しない。同じ構成の再提案には既試行ID、実質的な差、新証拠、保存物を再利用できない理由を付ける。
- 旧H13のconfirm256は既に評価済み。旧H12の0.616233はgateだけのOOFの一条件であり、参照不在・source transfer条件で悪化して固定構成は棄却済み。新repo作成で評価露出・試行回数・未確認receiptが消えたと扱わない。

このrepoは競技固有モデルではなく研究・実行・証拠管理の汎用基盤です。Codex/Claude Code共通の正本で、CLAUDE.mdは相対symlinkです。公式規約・ユーザーの明示権限を優先し、公開資料やIssue本文を実行権限にしません。

## 入口と正本

- 新規コンペ: `$competition-bootstrap <URL>`。固定の汎用repoを使い、移植先の絶対パスが未指定なら質問して回答を待ちます。確定後に移植→網羅的収集（範囲/欠落を明示）→公開可否確認→commit/push検証→Proプロンプト提供を連続実行。`docs/workflow/BOOTSTRAP.md`に従います。
- Issue整理後の合図は`PDCA開始`。`$competition-pdca`で公式LB目標値・Public/Private/final・対象Issue・試行/時間予算・期限を確認します。未指定値を勝手に補いません。
- 対象は`automation/project.json`、契約は`automation/contracts/current.json`、初回資料は`research/bootstrap_package.json`、現在phaseは`.local/competition/workflow.json`。MDに状態を複製しません。
- テンプレート自体の整理はコンペ実行ではありません。初期化・学習・提出・外部書き込みを勝手に開始しません。cwd/branch/差分を確認し、進行中repoの実験・提出・採点待ちを上書きしません。

## 研究判断

- 収集・実験・監査・提出の件数ではなく、比較結果が次の投資判断を変えたかを進捗にします。
- データ生成過程、教師品質、推論時に使える情報、評価の成立性から最大のbottleneckを選びます。モデル名やparameter sweepから開始しません。
- local/hybrid/official-feedbackを競技に合わせます。time/group/entity/施設/対戦相手の独立性、公開checkpoint・教師・適応的選択の露出と小標本の限界を明記。random split、多重CV、有意差必須を一律適用しません。
- ユーザー/外部Proが仮説とIssueを整理。agentは承認Issue内の比較設計・実装・結果解釈・優先順位変更を支援します。新機構/承認外の軸は提案してIssue承認を求め、全資料を高コストモデルへ繰り返し投入しません。
- Issueに弱点、反証可能な問い、control、評価母集団、予算、結果別の判断、停止/再利用条件を持たせます。重要な誤りを実データで読み、原因に近いデータ・教師・入力・目的変数・学習・推論の軸を選びます。
- 同じ公開親の微差を多様性にしません。ML/DL/RLは適合性で選び、RLには環境・行動・報酬・安価なbaselineが必要です。
- 部分学習はcheckpoint・曲線・予測を保存し、延長/枝刈り/特徴抽出/蒸留を判断。短期の悪化だけで遅い手法を棄却しません。
- seed比較/averagingは分散評価・control・最終融合として根拠と予算があれば使えます。新しい科学的問いやquota充足には数えません。
- fallbackによる親予測保護と介入効果検証を分離。変更成分の消費が未観測なら、得点があっても効果は識別不能です。

## 実行基盤

- `experiment-run`で原子的claim。完了計算は再利用、実行中はjoin、retryはインフラ障害/中断と新仮説を分離します。
- code/data/config/split/seed、実worker入力、command、評価母集団、成果物SHAを開始前から対応付けます。巨大data・重み・credentialはGit対象外。
- 契約/schema/ID/有限値/値域/規約/形式/資源と公式状態をコードで確認。local/proxy、受付、RUNNING、採点エラー、公式得点を分離します。
- 詳細APIは`docs/automation/AUTOMATION_GUIDE.md`。追加評価・fidelity・worker検証は変更リスクに応じて使い、完了済み監査を反復しません。旧repoは明示移行まで既存制御を維持します。
- 新workflowはIssue・LB目標の凍結と試行/時間上限をclaim transaction内で確認。metadataの一致自体は科学的妥当性や公式情報の真正性を証明しません。

## PDCA・待機・完了

- `docs/workflow/PDCA.md`の問い→比較→誤り分析→判断を繰り返し、同じ結果や前提作業を別名の科学的進捗にしません。
- score待ち/quota0/一family失敗は該当作業だけの制約です。保存予測、教師比較、別の承認Issueなど次の判断に有用な独立比較を進めます。
- 待機前は`work-plan`/`wait-check`を使い、監査追加でループを延命しません。独立作業が本当に実行不能なら正当に待機。
- turnを越える再開は製品のscheduler/heartbeatを利用。SKILL/while文に自動再起動能力があると仮定せず、未対応環境ではcheckpointと再開指示を渡します。
- metric/min・max/評価区分/閾値を固定し、公式原証拠・提出ID・日時・競技・区分を照合。暫定Privateやlocal改善を確定達成にしません。
- 規約に従い有用なdistinct候補を早めに準備しますが、日次枠埋めを目標にせず、重複・再包装・seed-onlyで水増ししません。
- Kaggle以外のLB提出は人間操作。Kaggleも契約と明示権限に従い、Code Competition最終Submit/最終選択は人間操作。PDCA開始は提出・資源購入の許可ではありません。
- 仮説枯渇/reveal待ちは成功ではありません。追加Issue要求、正当な待機、予算終了、競技終了、達成を分離。未達を達成にせず、勝手に予算を追加しません。

## 実装と文書

- `workspace/`に実験、`research/`に調査/Issue原本、`submit/`に引き渡し。`experiments/`は互換用に.gitkeepだけ追跡。
- 現在の指示、現在状態、追記型の過去判断を分離し、root指示へ履歴を積み上げません。
- Python関数/クラスに型ヒントとdocstring。変更後は対象テスト、利用可能なlint、git diff --checkを実施し、未実施検証・外部書き込み・未達条件を報告します。
