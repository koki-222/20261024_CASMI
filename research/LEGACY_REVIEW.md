# 過去実験の失敗・限界と再利用条件

2026-10-24のユーザー追加依頼に基づき、旧CASMI作業の保存原本をread-onlyで調べた。今回の研究判断に使えるよう、比較条件・分母・採否・未確認範囲を整理した。新実験、予測再生成、学習、Notebook実行、Kaggle照会、旧controller/job/timerの操作はしていない。

## 確認範囲と証拠の強さ

旧ledger1,525行、旧Issue snapshot群、保存公式観測44件を索引化し、重要な比較・障害16論点から83箇所のaggregateを原文と一致確認した。固定snapshot187ファイルのSHA、既存auditと対象metricsの結び付け、分数・paired分母177項目を確認した。ledgerの1行を1実験として数えず、全過去実験を意味的に読了したとも主張しない。

数値の原本path・SHA・JSON pointerは[comparison_evidence.json](legacy/comparison_evidence.json)、保存ファイル一覧と範囲は[source_index.json](legacy/source_index.json)、提出ID・得点観測時刻は[official_observations.json](legacy/official_observations.json)。raw snapshotはGit対象外の`.local/restricted/legacy_review/`へ固定した。分子ID、正解構造、予測列、特徴量、係数、会話原文はpublic資料へ含めていない。

今回は保存されたaggregateと過去auditの原本を照合した。NN・全候補score・化学処理を独立に再計算して過去audit全体を再実証したわけではない。公式得点も保存時点の観測であり、現時点のLBを再取得していない。

## 科学的な失敗を次のIssueへ反映する

|比較・根拠ID|固定条件・評価母集団|保存結果|今の判断に反映すること|
|---|---|---|---|
|H10初期hard-negative：`H10_early_class1_damage`|canonical main1,280、NP250、Class1 sensitivity250は別母集団|未見分子側には改善があったが、Class1の同一候補比較はMRR0.850699→0.008566、rank1破壊192件|一母集団の改善を全分子への置換根拠にしない。Class1保護・source/参照可視性を分離する。全hard-negative手法を否定した結果ではない|
|candidate reachability：`zero_candidate_reachability`|固定train512/tune256の外部候補bank|正解候補はtrain2/512、tune0/256。ranker実験は未dispatch|候補に正解がないこととscoreモデルの失敗を区別する。このtune集合で別rankerを学習・比較し直しても問いに答えない|
|priorの対照不足：`H14_prior_does_not_beat_control`|同一旧候補上のtune256、legacy表現|raw FP0.021074→prior0.024853でも、凍結linear0.052956を下回る|弱い対照への改善だけで採用しない。現在の対照と候補生成・metric・処理段階を合わせる|
|固定rank fusion：`fixed_rank_fusion_losses`|train512/tune256、全手法のtune候補被覆242/256が同じ|linear0.303075に対しRRF0.211095/0.223351、FP-only0.011783等|この相対悪化を候補欠落だけで説明しない。弱いscoreとの固定融合は既試行。別の融合全般まで棄却しない。legacy metricであり現行v14評価へ直結しない|
|H10 residual：`H10_restored_residual`|full pool、source OOF315とtune256。上流全体のstrict CVではない|OOF0.357022→0.271887、tune0.404430→0.292134。tuneのTop1破壊51、救済15|この固定残差構成は棄却済み。同じ集合で係数・offsetを探索して救済しない。teacherがlegacy canonical keyで、評価targetはv14であることも記録する|
|H21 branch：`H21_fixed_branch_ablation`|同一64分子・6,000候補行・固定prior|equal mean0.579327に対しsingle0.552664、merged0.560998|固定branch除去は両方棄却。少数の勝ち数だけで判断せず、分子MRRとTop1損益を残す。全single/merged設計の否定ではない|
|H12固定0.6gate：`H12_fixed06`|同一64分子、同canonical componentを参照から除外|MRR241/416→2033/4160、差−29/320、Top1破壊11、救済0|この試験は参照不在時の誤昇格損失を測る。Class1の便益まで測定したと扱わない。同じ64で閾値探索しない|
|H12無条件昇格：`H12_unconditional512`|train-only512。独立source参照ありのarmとcomponent-absent arm|0.508612→0.530781でもTop1破壊91・救済85。component-absentは0.348992、Top1破壊144・救済0|平均改善だけで無条件昇格を採用しない。参照のない分子と空poolを分母から落とさない。正解componentの参照有無は評価maskであり推論時oracleに使わない|
|H12 utility gate：`H12_utility_oof512`・`H12_source_transfer_*`|固定ridge alpha10、component5-fold OOF、512分子。過去診断を見て設計したgateのみのOOF|independent-source0.508612→0.616233だが、component-absentは0.486906、Top1破壊18・救済0。非GNPSでfitしてGNPSへtransferすると0.566182→0.544252。5事前条件中4条件が未達で固定構成は棄却|0.616233を公式LBや採用根拠にしない。既視集合で新feature・係数・閾値を探索しない。校正gate一般の不可能性を示したわけでもない|
|H33 ICEBERG：`H33_ICE_single_cosine`|同一64分子、固定候補・single cosine、空pool込み|raw0.577988、GL0.568333、ICE0.463043。raw比Top1破壊9・救済1|この固定ICE置換は棄却済みで400分子実行へ進まなかった。既視64で融合係数を探索しない。ICE全体・別の適切な比較を一括否定しない|
|H13品質集約：`H13_selected_train_tune`・`H13_confirm256`|現行S2/M1、固定4規則から選択後の確認256。上流pretraining露出は残る|tune0.566570→0.570826、確認0.516666→0.524948。ただし確認のpure_non_gnps82分子は0.395566→0.394248|local確認の改善はその母集団の証拠として保持するが、全domain改善とは呼ばない。確認集合は既に消費済み|
|H13公式の反証|保存提出57539823、観測2026-10-24T02:20:03.956597Z|Public0.290。比較元57382993の保存観測0.293を上回らない。H11の固定cosine57502818も0.260|単一対比から一般CV–LB相関を主張しない。local確認で正でも公式で改善しない結果を隠さない。丸められたPublic値から厳密な非丸め差を推定しない|

## 実装障害・管理上の誤りを科学的失敗と混同しない

- **確認集合の枯渇という誤診断**：旧H12準備処理は、実際の評価queryに加えreference除外集合全体まで評価済みと数えていた。旧失敗版・修正版・回帰testを保存原本で確認した。修正後に未評価256componentの固定manifestが作成されているため、「fresh集合がない」という以前の要約は現状の結論に使わない。ただし、その後の固定utility gateは別の理由で棄却されている。この256を無条件に評価する許可にはならない。
- **H13の途中失敗**：`H13_execution_failure`はneutral mass欠損で174query保存後に停止し、精度結果は未生成。修復v003は174件を再利用して残り594件を計算し、欠損は4arm共通のempty poolとして全768を分母に含めた。中断時の部分結果を完成精度や悪化と扱わない。
- **数値精度と候補軸**：`feature_precision_repair`ではfloat32 metadataの修復でnegative候補の順序13件・集合2件が変わった。candidate array自体が不変でも、学習に使うnegative構成が不変とは限らない。dtypeや特徴量段階を対応付ける。
- **wrapper失敗と有効計算の分離**：H33では比較計算後の補助assertが旧GLのunsupported zero-vector表現を誤判定した。保存rankからの回復auditがあり、NN・rankの再計算なしで棄却判断を保持している。wrapperが失敗したことをモデル精度の失敗と呼ばない。
- **要約・台帳の遅延**：読み取った旧current_stateは2026-10-22更新で、H13を採点未確認、H12校正を準備段階としていた。2026-10-24のraw score/metricsが後続証拠である。過去ledgerのrunning件数を現時点の稼働数とは扱わない。
- **得点revision**：保存された44観測には時期・scorer・再採点条件の異なる履歴がある。初期の0.337等と後期0.293を並べて単一の最新best推移や原因にしない。今回は再採点・live LB照会を行っていない。

## 既に行った対策と次の引き渡し

1. [reuse_policy.json](legacy/reuse_policy.json)で棄却済みの固定構成・反例・再開に必要な新証拠を整理した。機構全体の禁止リストにはしていない。
2. [exposure.json](legacy/exposure.json)で旧train/tune/confirmの役割と後続消費を記録した。新repoを作ったことによって集合や試行回数が未使用に戻ることはない。
3. evaluation_designとAGENTSへ、候補被覆、参照条件、paired損益、上流露出、既試行との対応をIssue起案前に確認する要件を反映した。
4. Pro向けプロンプトへ、各Issueに既試行との違い・残る証拠不足・再利用可能な成果物を必須で書くよう追加した。今回は新仮説やIssueの採択・登録をしていない。

旧weight・実装・承認・提出権限は移植していない。過去結果を再計算する前に保存物と閉じたreceiptを照合し、新しい問いを解く場合だけ変更点と評価露出を明示してユーザーのIssue判断へ渡す。
