# 実行履歴から汎用starterへ落とす設計

この文書は、SAD、GeoAI、NEDO、ROGIIの実行履歴を、競技名やモデル名に依存しない制御へ抽象化したものです。個別競技の契約JSONは参照プロファイルであり、共通コードは4競技の知識を直接持ちません。

## 履歴で繰り返した失敗

| 観測された失敗 | 共通化する制御 | LLMの扱い |
|---|---|---|
| local CV・proxy・公式LBを同じスコアとして扱う | metric、direction、evidence kind、official provenanceをrecordに固定 | 数値の意味の説明だけ。採用判断はしない |
| 同じCSV/zipを日付やディレクトリだけ変えて再提出する | payload SHA、control/reference、重複台帳 | 類似候補の説明だけ。重複解除はしない |
| random splitや通常の`fold != val_fold`で未来・同一groupを学習する | split profile、cutoff/manifest、feature boundary、ID disjointness | ユーザーIssueの分割案を検査し、リークを説明する |
| hidden入力・Test label・same-ID lookup・外部データを混入する | train-only policy、provenance、hidden-safety validatorの証跡 | 規約の読み取り補助。PASSを自己申告しない |
| 長時間実行・provider error・未完了ログを成功と扱う | lifecycle（planned/running/succeeded/failed/blocked）、command、timeout、evidence artifact | 実行不能の説明と再開案だけ |
| 「仮説が枯渇した」「reveal待ちだから終了」とLLMが判断する | completion stateを`completed`にせず`blocked`に固定し、探索空間・確認済みfamily・negative evidence・resume conditionを保存 | 枯渇判定と終了はしない。残る証拠と追加Issueの判断材料をユーザーへ返す |
| 近いfamilyの実験を閾値だけ変えて繰り返す | parent/family/hypothesis hash、negative evidence、stop reason | 既存Issue内の重複を説明する。新規Issueを無断作成しない |
| 仮説枯渇を理由にseed averagingへ逃避する | seedless semantic signature、`seed_only`/`seed_averaging`の明示control、通常candidate/quotaから除外 | seed変更を新規仮説として昇格しない |
| 提出形式の検証前にready/提出候補として扱う | `ready`にsubmission PASS・実体SHAを必須化し、quota plannerでも再ハッシュ | エラーの説明文だけ。PASSを自己申告しない |
| 非KaggleのLB/API提出をagentが疎通確認・自動retryし、外部エラーでタスクをblockedにする | `submission.upload_mode=manual`を既定化し、非Kaggleの`agent`を契約で拒否。`submission-plan`はネットワークなしで`READY_FOR_MANUAL_UPLOAD`/`manual_pending`を返す | 外部提出は人間へ引き渡し、remote score未取得をローカル検証Failにしない |
| platformごとのCSV/zip/notebook/Docker差異を手作業で確認する | submission contract adapterと共通CLI | エラーの要約だけ |

## 2026-08-20のSAD・GeoAI最新ログから追加した制御

対象はSAD task `019fe4d7-d13d-78a1-8286-5cac48e55aa6`とGeoAI task `019f7a81-2ec8-7cd3-825f-d5ffe46293aa`の最新会話・実行ログである。結論文だけでなく、実験番号、metric、提出物、provider error、blocked条件を照合した。

| 最新ログの観測 | 問題 | 汎用化した制御 |
|---|---|---|
| SAD A162〜A167でC3Sの`nobs/sensor/freqbandID`、RZSM、PASSIVE、ACTIVEを小分けに逐次実行。RZSMはpooled約`-0.0108`でもp90/lateを通らず、ACTIVEはcoverageが極小 | 共有coverage/join検査より先に近接変種を1本ずつ回し、実験回数とagent turnを浪費 | 外部入力のcoverage preflightを必須化し、近接変種familyは`batch_required`、共有preflight、候補signature群、事前停止則を要求 |
| SADのspatial最大劣化はA160 `+0.2015366`、A161でも`+0.0430192`。pooled改善だけでは採用不能 | 単一平均metricによる誤採用と、同一familyの閾値微調整反復 | robust gate失敗をfamily単位で数え、既定3連続で閉鎖。再開には新しいmechanism/input signatureを要求 |
| GeoAI B461、B462、B465、B468は状態監査を繰り返したが、新しい合法・非重複candidate、artifact、提出、commitはいずれも0 | 状態不変でも別番号の「残余仮説監査」を生成し続ける | contract/research/ledger/official-stateの合成fingerprintが同じ監査を`UNCHANGED_AUDIT_SKIPPED`にする |
| 両taskで各micro-taskごとに同じ`reasoning_content`系400/transport errorをFlash/Proで再発させ、その後fallback | provider障害を仮説探索と混同し、既知の失敗経路へ毎回再dispatch | provider error fingerprint、連続失敗閾値、`retry_after`によるcircuit breaker。provider障害はnegative experiment evidenceに数えない |
| GeoAIはreveal予定日をresume条件にblockedとなったが、予定日経過後も自動的な再評価が実行されなかった | 自然言語のresume conditionだけではtaskが再開されない | blocked時のstate fingerprintと`next_check_at`を機械判定し、証拠変更または時刻到来で`RESUME_DUE`を返す |
| GeoAIの事後監査でv010の重複、v334/v435のbyte-identical、B450の実行後runner変更によるprovenance混在が確認された | 名前や報告文では重複・実行時コードを保証できない | 既存payload SHA/semantic signature拒否に加え、全実験でrun-start data/code/config/split snapshotを必須化 |

精度面では、SADの最良既知Public LB `0.721694866`と目標`0.3`、GeoAIの最良既知LB `0.846351592`と目標`0.965`を混同しない。今回のcontrollerは精度を保証せず、無意味な再実行を減らして、残った計算・提出枠を新しい情報を持つ仮説へ割り当てる。

### 数値で確認できた根拠

- SADは外部RMSEが`0.727739708`から`0.721694866`へ改善した後、CatBoost routerが`0.748123263`へ悪化し、local strict gateと外部結果が反転した。
- GeoAIは最高LB`0.846351592`に対し目標`0.965`未達で、CV-LB順位Spearman`-0.900`を記録した。CVだけで候補を採用できない。
- NEDOはr59のLB`13.14269630916221`と目標`68.69398`の差が残り、同一zip SHA`d40ecda...`の再包装が複数候補として現れた。
- ROGIIはcached public`4.902`がfresh hidden`7.113`へ転移せず、visible完走とhidden安全性を分離する必要があった。

提出・実験数、重複、HEADのsnapshotは`audits/20260815.md`と`../../automation/audit_snapshots/20260815_summary.json`を根拠とする。これらは精度向上の証明ではなく、比較・採否を壊した履歴上の失敗を再発させないための入力である。

## 汎用層の責務

1. **Layout profile** (`automation/layout.json`): 実験根、提出根、必須ファイル、payload拡張子、除外ディレクトリを設定する。共通監査コードに`workspace`や`submit`を固定しない。
2. **Competition profile** (`automation/contracts/*.json`): metric、出力schema、split policy、required checksだけを競技ごとに宣言する。SAD/GeoAI/NEDO/ROGIIはこの参照プロファイルの例。
3. **Experiment record**: 実験ID、parent/family、provenance、seed、command、状態、metrics、checks、artifactsを同一recordへ集約する。文章だけの実験完了報告は証跡にならない。
4. **Deterministic validators**: CSV/zip/notebook等の形式、finite/range/ID、path/symlink、SHA、重複、証跡実体を検査する。競技固有のreplayやhidden監査はvalidatorの出力をhash付きcheckとして受け取る。
5. **Evidence gate**: `candidate`、`adopted`、`rejected`、`blocked`を分離する。`adopted`は公式証跡と全required checksが揃った場合だけ許可する。
6. **History audit**: 実験数、payload/support、重複、未完了/blocked、Git snapshotを再計算可能なJSONで保存する。

`experiment_signature`はvolatileなscore・ログ・提出時刻を除いたsemantic payloadから計算し、別artifactでも同じ実験をledgerで拒否します。`change_scope=seed_only`または`seed_averaging=true`は、`base_signature`を持つ再現/controlとしてのみ保存でき、採用・提出・日次quotaのdistinct候補にはなりません。

## LLMに依存しない境界

契約の解釈後に機械化すべきなのは、入力・出力schema、split、リーク境界、metricの値域、seed/command、artifact hash、提出検証、公式証跡、重複、採否状態です。モデル変更・停止・provider 400/429が起きても、これらの結果は変わってはいけません。

LLMに残すのは、Issue実行時に必要な資料の意味理解、実装補助、失敗原因の候補、レポート草稿です。仮説の起案はユーザーがGitHub Issueで行い、agent/LLMは明示的な例外指示なしに公開資料全体を使って新規仮説を生成しません。Issue本文も`checks`や`requested_decision`へ直接昇格させません。

## 新しい競技を追加する手順

1. `automation/layout.json`を競技の実験/提出構成へコピーして変更する。
2. `automation/contracts/template.json`を新しいslugでコピーし、metric・submission・split・policyを埋める。
3. `survey/competition/overview.md`へ公式metric、データ時点、外部データ規約、hidden条件を記録する。
4. baselineを1本だけ実行し、recordとprovenanceを保存する。
5. deterministic submission-check、leakage validator、replay/hidden validatorを実行し、ログをSHA付きでrecordへ登録する。
6. `audit --layout automation/layout.json`で履歴・重複・supportファイルを確認してから、候補をgateへ渡す。

この流れでは、競技固有の精度改善コードは各プロジェクトに残り、starterは再現性・比較可能性・安全な採否を共通化します。共通層だけで外部LBを予測したり、精度向上を保証したりはしません。

## 2026-10-15 CASMI・RSNAの実行ログから追加した設計

CASMIとRSNAのJSONL実行履歴を、会話だけでなくcommand、終了状態、sleep、保存されたrecord、提出artifactまで照合した。両競技で、科学的な仮説の不足よりも、実行の識別、待機中の作業、計算予算、候補coverageの管理がボトルネックになっていた。

| ログの観測 | 失敗の原因 | 汎用化した制御 |
|---|---|---|
| Issue番号やresearch manifest pathだけを変えた近接実験が別signatureになった | 文書・運用metadataと科学的計算identityが混ざっていた | hypothesis/computation/attemptを分離し、source Issue、path、時刻をsemantic signatureから除外する。seed変更は明示controlへ固定する |
| controllerの実行履歴に`resume_check`やstate監査が多く、実際のexperiment actionへの結び付きが弱かった | controllerが実験launcherの単一入口でなく、callerの履歴に依存していた | `experiment-run`でaction、Issue、snapshot、依存を検査してから原子的claimを作る。完了済みは再利用し、runningはjoinする |
| CASMIで55分、続いて約75分のsleepを計画し、RSNAでも約8時間のtimer待機中に保存済みpredictionsの高速診断が残っていた | scoring待ちをLLM turnの終了と同一視し、独立作業のready queueを再評価しなかった | DAGのready node、依存、資源、deadlineを機械計画し、ready queueが空の場合だけschedulerへ待機を返す |
| RSNAの約1300通りの全branch再実行後、共通graph batchの5提出が同じ失敗経路になった | 小変更ごとのfull replayと、未知runtime failureへのcanary不足 | 依存SHAによるdelta validation、変更classごとのcanary、shared failure fingerprintを使う。全fleet再実行を既定にしない |
| CASMIのraw metadata attentionは目標20,000stepに対し12,805stepで止まり、model stateだけを保存した | optimizer/RNG/samplerがなく、継続も公平な比較もできなかった | full-state checkpoint、multifidelity rung、曲線・budget・再開結果のrecordを必須にする。部分学習は`budget_exhausted_inconclusive`とする |
| CASMIではrankerより前のcandidate coverageが低く、RSNAでは近い入力・decoder・TTA系が多かった | rankerの改善を急ぎ、入力候補とmechanismの探索空間を狭くした | coverage、conditional rank quality、total metricを分け、Issueの許可範囲内で機構・入力・評価軸をportfolio化する |
| `fidelity`を少数sampleの一致だけで判断し、新しいbranchがhidden相当経路を通った証拠が不足した | sample output identityとprogram equivalenceを混同した | fidelity planのlineage/manifestを検証し、branch target・restore・new runtime/input classの実行は競技固有adapterが担当する。必要なcanaryと生成物・code/data SHAを別証跡として記録する |

### 多様性の扱い

「ML/DL/RLを全部試す」を新しい固定ルールにはしない。Issueは、検証したい失敗機構に応じて、表形式/木、特徴抽出＋軽量head、transfer、self-supervised、構造化推論、検索・最適化、適応的計算、simulation policy、RLなどの軸を選び、安価なbaselineと停止条件を持つ。RLはstate/action/rewardがデータと評価に対応し、offline/online評価と計算制約を説明できる場合に限る。固定予測問題へ形式だけRLを付けることは多様性として数えない。

全学習を前提にせず、短いpilot、少量データ、frozen encoder、early checkpoint、partial feature、段階的fine-tuningを予算候補として扱う。ただし、途中結果は手法全体のnegative evidenceではない。checkpointのresume fidelityと未完了範囲を保存し、比較可能なselection/stress評価に届かない結果は採用根拠にしない。

### 実装時の受け入れ条件

- 同一computationのIssue/path/出力先変更が再実行を発生させない。
- 完了済み成果物へ再利用、実行中へjoin、infra failureだけretryが返る。
- score polling中でも、依存のないready nodeがdispatch可能である。
- 部分checkpointからfull-stateを復元でき、復元不能なら明示的にinconclusiveになる。
- 同じpayload、再包装、seed-onlyが候補枠・日次quotaを消費しない。
- branchのfidelity、schema、submission、stressが実体artifactとSHAで検証される。
- state compact後もarchiveから現在stateを再構築でき、元のappend-only履歴を削除しない。

## 2026-09-01 Trace the Ace最終結果から追加した制御

Trace the Aceではsession-group OOF`0.535659`に対し、objective-cold proxy`0.594835`、最終Private最良`0.5995`だった。fast CVをselectionへ使わず、hidden分布へ近いprofileを別に固定する必要がある。Public最良v004とPrivate最良v008は反転したがPrivate差は`0.0003`で、95%区間`[-0.00110, 0.00184]`は0を含んだ。微小なLeaderboard差はchampion置換ではなく、条件付きhedgeとして扱う。

この結果から次を追加した。

- controllerの`evaluation_policy`でselection/stress profileと公式feedback round上限を固定する。
- `selection-check`でeffect-size、group-aware interval、complexity、hedge regret、prediction correlation、distinct familyを検証する。
- completionの`ended`で、目標未達の競技終了をfake successや永久blockedと分離する。
- `retrospective-check`でfinal Public/Privateとfrozen selection planを照合する。

詳細数値と判断境界は`docs/automation/audits/20260901_TTA_FINAL.md`を参照する。

## 2026-09-14 SAD最終結果から追加した制御

ユーザー報告の最終28件では、Public最良とPrivate最良はいずれも`wWWXzjhe`（`0.721694866` / `0.764368572`）で、Public/Private順位のSpearman相関は`0.92009`、Private−Public差の平均は`+0.0418098`だった。したがって、失敗を大規模な順位shake-upや一定offsetだけで説明しない。最終5候補のPrivate最良は`ajXYJdYg`の`0.777640909`で、全履歴上のPrivate最良との差は`+0.013272337`だった。

一方、履歴上のスコア最良候補には外部データのavailable-time契約が未確定なものがあった。スコアだけの`historical_best`を自動的にchampionへ昇格させず、規約とデータ時点をhash付き証拠で確認した`verified eligible`候補と分離する。

この結果からschema v2へ次を追加した。

- `gate`のadoptionをfrozen selection planへ接続し、record ID・family・提出artifact SHAの一致を必須化できる。
- selection score、stress score、観測数、distribution/group、interval/correlationの比較championを候補result evidenceへhash固定する。
- family名とは別にlineageを固定し、portfolio多様性と公式feedback budgetをlineage単位でも検証する。
- final snapshotの正規化値をraw platform artifactまたは`user_reported_official`へ結び付け、manifest値を再照合する。
- retrospectiveで全提出最良、選抜最良、verified eligible最良とregretを分け、CV/Public/Privateの順位相関とpairwise concordanceを出力する。

これらはSAD固有のGRU、blend、外部データを禁止する規則ではない。評価profileの選抜力と証拠接続が壊れたことを検知し、同系統候補の量産や後付け選抜planを防ぐ汎用制御である。

追加監査では、最終5候補を同一2期CVで比較できても、履歴上の最良LB候補に対応する同一strict-CVのOOFは確認できなかった。単にprofile名・分布signatureと正の観測数が揃うだけでは、旧championと新候補の採点行・正解が同じとは保証できない。この残差リスクに対し、現行selection plan templateでは`require_matched_scored_cohort=true`とし、selection/stressの採点母集団snapshot SHAを候補結果へ結び、候補間の観測数も照合する。CV選抜力そのものやLB改善は保証せず、比較不能ならchampion置換を保留する。

## 2026-09-30進行中のKaggriculture・Biohubからの更新

この時点で両競技は未終了であり、以下はPrivate結果の事後分析ではない。
表中の対戦相手帯、rating、GEFF、GPU時間は原因分析の事例であり、汎用contractの必須fieldや既定閾値ではない。共通層は評価ID、選択式の比較gate、worker入力契約、成果物・締切検証だけを扱う。

| 実行証拠 | 誤判断の危険 | 今回の共通化 |
|---|---|---|
| Kaggricultureの元v026は88局中64局が2500–2999帯でrating2867.8、同一archive名の再提出は20局すべて2000未満帯で1798.0。両方の直近rating窓は上昇中で、再提出sourceのSHA receiptは未発見 | 約1070点差を同一方策の性能崩壊と断定する、または旧scoreから再提出の回復を保証する | `official-feedback-check`で観測数・cohort構成・driftを事前閾値と照合。source同一性はartifact SHAを別に提示し、比較不能ならscore差を採用根拠にしない |
| 同競技のG926は作物切替72回でも独立したopponent×seed単位が3件、売上まで追跡できたcycleが0件。G927は両agentが16勝0敗で識別不能。最新v190/v191も501/454局で1664.3/1654.5へ下落中 | 発火回数や易しいlocal panelの全勝を機構改善と誤認し、未成熟のactive枠を情報量のない候補で置換する | 既存selection/stress gateを維持し、発火・独立単位・outcome識別・active portfolioの置換損失を事前登録する。公式ratingは成熟まで保留 |
| Biohubの公式Public最高は0.953、別系統0.947とともに最終2枠へ選択済み。しかしstrict胚分離CV–LB pairは0件。K133は時系列2モデル後に補助DeepCenterのGEFF入力欠落で停止し、約6.56 GPU時間を消費 | Public値だけでモデル改善と主張する、部分checkpointを昇格する、入力形式を推測してGPUを再投入する | `worker-input-check`で全stageのfile/directory rosterを実worker上で確認し、`validated` jobには宣言済み`expected_output`を要求。既存selection gateを飛ばさない |
| BiohubではGPU残量45時間でも競技締切まで約2時間で、K133両foldの学習・採点は不可能だった | quotaだけを見て未完了が確実な計算を始め、検証不能な新提出を作る | 実行計画に締切までのtraining・validation・submission全経路を含める。不足なら監査だけに留める |

証拠は隣接する`202609_kaggriculture/daily_reports/20260930.md`、同repoの`evidence/v026_rating_cohort_audit_20260930T0710JST.json`、`202609_Biohub/research/reports/20260930_deadline_pdca_audit.md`を参照する。これらは時点snapshotであり、進行中のtaskが後に更新した公式値を自動反映しない。

## 2026-10-01 RSNAの運用変更から採用した入口

RSNAでは公開Notebook/scriptとdiscussionの原文をローカルへ保存した後、ユーザーが仮説をGitHub Issuesへ整理し、P0からPDCAを再開した。以前の「公開資料をAstraへまとめて渡す／都度Astraを呼ぶ」方式はtoken費用が大きく、ユーザーが仮説を起案する運用へ変更された。この判断はモデル性能の評価ではなく、仮説起案の責任とtoken使用境界の変更である。

汎用テンプレートでは、公開資料の原文・version・返信・添付・取得範囲を保存した後にユーザーIssueを待つ。`controller-check`は設定されたGitHub repository、許可された作成者、Issue番号・URL・状態・本文、ローカルsnapshot SHAを照合し、Issue未登録なら`WAITING_FOR_USER_ISSUE`を返す。Issueは実験の出典であり、採否や提出成功の証拠ではない。Issue作成だけを無断再開の許可とみなさず、ユーザーの再開指示後に実行する。Kaggle固有APIやRSNA固有のP0件数は共通コードへ固定しない。
