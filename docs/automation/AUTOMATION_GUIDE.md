# 競技自動化の運用契約

新規の入口は[初回導入](../workflow/BOOTSTRAP.md)と[研究PDCA](../workflow/PDCA.md)。本書は既存runtimeの詳細API参照であり、全schema/監査を毎回作るチェックリストではありません。旧repoの稼働状態は明示移行まで維持します。日次quota必須の旧profileは明示opt-inで、新規競技に枠埋め義務を適用しません。seed比較/averagingは参照元を持つcontrol/分散評価/融合として使え、新仮説件数と区別します。

このテンプレートは、モデル名やLLMの推論品質が変わっても、提出可否と証拠の強さが変わらないようにする汎用共通層です。学習コード・競技固有validatorは各プロジェクトへ適応し、layout/profileを差し替えて使います。過去4競技からの抽象化は [HISTORY_TO_GENERIC_DESIGN.md](HISTORY_TO_GENERIC_DESIGN.md) にまとめています。

1. Layout/profile: 実験根、提出根、payload、必須ファイルをJSONで差し替える。
2. 競技契約: 入出力列、評価指標、分割、リーク境界をJSONで固定する。
3. 実体証跡: データ・コード・設定・提出物をSHA-256で固定する。
4. 機械ゲート/台帳: 形式、有限値、ID順、重複、再現性、evidence kindを検査する。

## 最小コマンド

リポジトリルートで実行します。外部アップロードは行いません。

```bash
PYTHONPATH=. python3 -m tools.competition_automation contract-check automation/contracts/project.json
PYTHONPATH=. python3 -m tools.competition_automation fingerprint path/to/artifact
PYTHONPATH=. python3 -m tools.competition_automation submission-check \
  automation/contracts/project.json path/to/submission-artifact \
  --ledger automation/ledger.jsonl
PYTHONPATH=. python3 -m tools.competition_automation submission-plan \
  automation/contracts/project.json path/to/submission-artifact \
  --ledger automation/ledger.jsonl --root .
PYTHONPATH=. python3 -m tools.competition_automation audit /path/to/competition \
  --layout /path/to/competition/automation/layout.json \
  --output /path/to/competition/automation_audit.json
PYTHONPATH=. python3 -m tools.competition_automation gate \
  automation/contracts/project.json path/to/experiment_record.json --root path/to/competition
PYTHONPATH=. python3 -m tools.competition_automation research-check \
  research/manifest.json --root path/to/competition --contract automation/contracts/template.json
PYTHONPATH=. python3 -m tools.competition_automation preflight \
  automation/contracts/project.json research/manifest.json \
  --root path/to/competition
PYTHONPATH=. python3 -m tools.competition_automation controller-check \
  automation/controller_state.json --root path/to/competition
```

`--sample`はsample提出物との照合が必要な形式だけに、`--quota`と`quota-plan`は提出件数制限が適用される競技だけに追加します。`automation/contracts/template.json`のCSVは記入例であり、共通ロジックの標準提出形式ではありません。計算を使わない競技ではcompute profileも不要です。使う場合の`automation/compute_profiles.template.json`はlocal-onlyの中立例で、Kaggle Notebookは別adapterです。

workerを使う場合、`rules.final_inference`はCode Competition等の最終実行契約があるときだけ設定し、通常は`null`にできます。締切が実行可否に関わるときは`compute_request.timeline`へ公式締切snapshot、後続の評価・検証・提出時間、安全余裕を宣言します。入力が事前配置されるstageだけ`worker-input-check`でfile/directory件数を検査し、生成途中の出力は完了後のartifact checkで検証します。

`gate` の `adopted` は、契約が要求する全ハードチェックと `evidence_kind=official` がそろった場合だけです。officialには、platformのsubmission ID、取得日時、公式ページsnapshot/API responseの実体ファイル、SHA-256、`--root`による再ハッシュが必要です。`local`/`proxy`/`user_reported_official` は候補または診断に留まり、外部スコアをコードが推測することはありません。

公式値の比較には、競技の評価形式に応じた`official-feedback-check`を使用できます。固定評価では同じ`evaluation_id`を必須とし、非公開の件数や変動値を作らず、不要なgateは`null`にします。対戦型・逐次評価など母集団が変わる場合だけ、`requirements`で最小観測数、cohort分布差、直近driftの上限を有効化します。必要な証拠が欠ければ`INSUFFICIENT_EVIDENCE`で差を採用根拠にしません。比較条件は観測前に固定し、payload同一性はhash付きartifactで別に確認します。このcheckはsnapshotのhashを照合しますが、任意のHTML/APIからの値の抽出は競技固有adapterの責務です。

```bash
PYTHONPATH=. python3 -m tools.competition_automation official-feedback-check \
  automation/official_feedback_comparison.json --root .
```

`submission-check` と `submission-plan` はローカル検証専用で、ネットワーク/API/外部LB提出を実行しません。契約の `submission.upload_mode` は既定値を `manual` とし、Kaggle以外では `manual` または `disabled` だけを許可します。`manual` の検証成功は `READY_FOR_MANUAL_UPLOAD`、`artifact_status=ready`、`external_submission_status=manual_pending`として扱い、agentは外部提出エラーを発生させず、人間が提出した後にreceiptと公式スコアを記録します。人間提出待ちは実験の失敗ではありません。

`gate`は競技固有のexact replay、hidden safety、same-ID監査、外部APIへのcompile/importを暗黙に実行しません。契約の `required_checks` に列挙されたvalidatorを実行し、コマンドとログ/結果のSHA-256を `checks` に記録してください。未実行または自己申告だけのcheckは `blocked` になります。NEDOのzipは共通層で `base/agent.py` の安全なパス、Python構文、`Agent`の必須メソッドを検査しますが、依存ライブラリを含むimport smokeとPyBullet exact replayは競技validatorの証跡が必要です。

分析開始前には `research-check` を必ず通します。規約・データ定義・metric・提出形式・timeline/quota・リーク境界・過去履歴の各sectionについて、URLやLLM要約だけでなく、取得時刻付きのローカルsnapshotとSHA-256を要求します。profileで研究sectionが不足している場合は、実験開始ではなく `blocked_research_incomplete` として止めます。

日次提出枠は `automation/quota.json` のquota profileで宣言します。profile自体にも公式rules snapshotとSHA-256が必要で、値が不明なままKaggle/SIGNATEの件数を推測しません。`quota-plan` はその日（profile timezone/reset基準）の、submission-checkの実体証跡とpayload SHAが再検証できるdistinct提出候補、既提出数、残りslot、必要なdistinct候補を返します。同一SHA、control、`seed_only`/seed averaging、submission-check未実行のレコードは新しい提出候補として数えません。`ready`/`verified`などのレコード状態を正しく記録する責任はcallerにあり、plannerは外部提出や公式結果を確認しません。枠を埋めるために同じ提出物を複製したり、seedだけを変えたりすることは `BLOCKED_NO_DISTINCT_SLOTS` / `NEEDS_DISTINCT_CANDIDATES` のままにします。

LLMが「仮説が枯渇した」「reveal待ちなので終了」と判断しても、taskを成功終了にしません。終了にはobjectiveの公式/検証済み達成証拠が必要です。情報待ち・規約確認待ち・外部認証待ちは `blocked` と `resume_condition` を記録し、次回起動条件を残します。仮説枯渇を主張する場合は、探索空間、確認済みfamily、negative evidence、最終確認時刻、再開条件を機械検証可能なmanifestに保存します。

## 次の行動を止めるcontroller gate

`automation/controller_state.template.json`を競技リポジトリへコピーし、audit・実験・provider dispatch・blocked再開の直前に`controller-check`を通します。これは結果の採否を決める`gate`より前段の、実行してよいかを決める制御です。

- `state_inputs`: contract、research manifest、ledger、公式状態snapshotなどを実体SHAで固定する。同じfingerprintの再監査は`UNCHANGED_AUDIT_SKIPPED`になる。
- `providers`: provider、正規化error fingerprint、連続失敗数、`retry_after`を記録する。閾値到達中は`PROVIDER_CIRCUIT_OPEN`となり、同じ400/transport errorを各micro-taskで再試行しない。
- `families`: family予算とrobust gate連続失敗数を持つ。既定で3連続失敗、予算到達、または`closed`で停止し、新しいmechanism/input signatureと理由がない再開を拒否する。
- `experiment_history`: hypothesis signatureの再実行を拒否する。seed-only/seed averagingは参照先と理由を持つcontrolに限り、新仮説として数えない。
- `blocked_state`: `state_fingerprint`と`next_check_at`を保存する。証拠更新または予定時刻到来時だけ`RESUME_DUE`となり、それまでは`WAITING_FOR_RESUME`になる。
- `run_snapshot`: data/code/config/splitの開始時証跡を必須化する。runtimeでは各groupのJSON内容がcanonicalな`computation_payload`（code/data/splitはrole→SHA、configはdict）と一致しなければならず、実行後にrunnerやconfigを差し替えてprovenanceを作ることはできない。
- `external_input`: coverage/join/availability preflightの実体証跡がPASSになるまで学習を始めない。近接変種を逐次実行したくないfamilyは`batch_required=true`にして、共有preflight・候補signature群・停止則を先に登録する。

provider errorの同一性は、volatileなrequest IDを含めず次のコマンドで作ります。

```bash
PYTHONPATH=. python3 -m tools.competition_automation provider-error-fingerprint \
  --provider provider-name --code 400 --message 'normalized error message'
```

`controller-check`の非許可は「タスク成功」ではありません。変更待ちはblockedのまま、同じproviderだけが開回路なら別providerまたは非LLM処理へ進み、閉じたfamily以外に未評価familyがあればそちらを計画します。

## 実験のclaim、再利用、ready queue

実験を3つの識別子へ分けます。`hypothesis_id`はユーザーIssueに書かれた検証対象、`computation_id`は固定したdata/code/config/splitを実行する計算仕様、`attempt_id`はその計算の一回の実行です。Issue番号、保存先、文章、LLMの応答IDだけを変えて同じ計算を再実行してはいけません。

実験入口はSQLiteなどの共有registryへ、次の順で原子的にclaimします。

1. controller actionが`experiment`であり、Issue snapshot、開始前snapshot、依存が有効であることを確認する。
2. 完了済みの同じ`computation_id`があれば元の`output_dir`・artifact・元Issueをlineageとして返し、実行中なら`JOIN_RUNNING`を返す。
3. `infra_failed`または`preempted`だけは`retry_of`を記録した新しい`attempt_id`として再実行する。仮説のnegative evidenceには数えない。
4. claim成功後にだけworker/launcherを呼び、終了状態、command、artifact、metrics、check、再開条件をappend-only eventへ記録する。

`experiment-run`のrequestは、`schema_version=1`、`computation_spec`、hash付き`controller`、hash付き`preflight`（`contract`、`research`、任意の`quota`）、hash付き`exploration`、新規の`output_dir`、相対`outputs`、正の`timeout_seconds`を必須とします。`fidelity`、`validation`、`queue`は必要な場合にhash付きsnapshotを追加し、queueを指定した場合は`queue_job_id`を選択結果へ結び付けます。実体の雛形は`automation/runtime_request.template.json`です。`computation_spec`は`task_type`、code/data/splitのrole別snapshot、`config`、`seed`、`budget`、`evaluation`、`environment`、shell expansionを含まないargv commandを持ちます。

学習taskではcommand argvに`{budget_units}`を必ず含め、adapterの累積step・tree数・episode数などtask固有の上限へ結び付けます。例えば`["python3", "{code.main}", "--steps", "{budget_units}"]`のようにします。`{budget_unit}`は任意ですが、使う場合は`budget.unit`を宣言します。runtimeは値のbindingと実行commandの記録を行うだけでadapterの上限遵守を証明しないため、実adapterが累積上限を超えないことを実測する検証とreceiptを別途保存します。新規計算では`computation_spec.resume`を省略または`null`にし、`{checkpoint_manifest}`も使用しません。

既存状態を使うrunだけ、`computation_spec.resume`へ`{"mode":"exact_resume|weights_only|feature_transfer","manifest":{"path":"...","sha256":"..."}}`を追加します。resume時のargvには`{checkpoint_manifest}`を必ず含め、fidelity planのcheckpoint manifestとpath・SHA・modeを完全一致させます。manifest本体と列挙された全componentを開始前に再hashし、resume manifest SHAをcomputation identity/signatureへ含めます。runtimeの照合は宣言・hash・lineageの一致までであり、実際のrestore semantics、optimizer/RNG等の復元、adapter上限の遵守はworker固有adapterが検証します。

commandのplaceholderは`{code.main}`、`{data.train}`、`{split.fold}`、`{config_path}`、`{output_dir}`、`{root}`、`{budget_units}`、任意の`{budget_unit}`、resume時必須の`{checkpoint_manifest}`だけを使い、実体SHAまたは宣言値へ展開します。`experiment-signature SPEC.json`はSPECだけからcomputation/hypothesis/mechanism/inputの4つのderived hashに加えて`run_snapshot_payloads`（`code`、`data`、`split`、`config`）を返し、Issue本文を読み込んだり仮説を生成したりしません。`controller-check`は旧来の読み取り専用actionを検査できますが、`experiment-run`は`action.type=experiment`と厳格なIssue-bound requestを要求します。
`data`のmanifestだけを列挙しても、workerが読むデータ本体の検証にはなりません。実際にcommandが読む全ファイル（raw/processed data、labels、feature tables、model/input assetsなど）を`computation_spec.code`/`data`/`split`のroleへ列挙し、それぞれのSHAを固定します。manifestは補助証跡として含められますが、実データ本体の代替にはなりません。
共通runtimeの既定出力は`results/{experiment_name}/attempt-001`とし、同一experimentのattemptごとに新しいdirectoryを割り当てます。`foldN`はfoldを実際に持つtaskだけが、その下位directoryとして任意に追加します。

実行中のjobを待つとき、controllerは次のready nodeを探します。依存が満たされたcandidate、差分検証、提出形式検証、調査snapshotの差分があればそちらを先にdispatchし、ready queueが空で`next_check_at`が未来の場合だけschedulerへ待機を返します。長いsleepをLLMのturnへ埋め込んだり、同じscore endpointをtight pollingしたりしません。

```bash
PYTHONPATH=. python3 -m tools.competition_automation experiment-run REQUEST.json \
  --root . --registry automation/runs.sqlite
PYTHONPATH=. python3 -m tools.competition_automation experiment-status \
  --registry automation/runs.sqlite
PYTHONPATH=. python3 -m tools.competition_automation experiment-signature SPEC.json
PYTHONPATH=. python3 -m tools.competition_automation queue-plan automation/queue.json
```

共有registryは競技repoごとに`automation/runs.sqlite`へ固定します。別registryを指定して同じ計算を再claimしてはいけません。

`experiment-run`は同一host上で宣言されたargvを一件実行するlocal runnerであり、外部実行をclaimしたり、競技全体を自動orchestrateしたり、外部LBへ提出したりしません。Kaggleを含め、upload権限と最終選択はcontractおよびユーザー指示に従い、非Kaggleは常にmanual handoffです。

同一computationの短期runから長期runへの移行は、新しいhypothesisの採用ではありません。同じmechanism/input familyのrung継続はfamily予算に新しいhypothesisとして消費せず、fidelity planの次のrungとして同じlineage・評価objective・Issue axisに対するcontinuation/promotionを判定します。短期runと長期runのmetricは混ぜずに記録します。

## 実験の忠実度、探索、多様性

開始前に`fidelity-check`でfidelity planのcomputation payload/lineage、checkpoint manifest、partial artifact、実測feasibility、promotion判定を照合します。これはargv、入力件数、出力契約を実行検査するものではなく、対象branchが実際に実行されたことや、checkpointのrestoreがworkerで成功したことも証明しません。実際のworker adapterがbranch/canary/restoreを実行し、入力件数・argv・出力契約とその証跡を別途記録します。小さいsampleの出力一致だけでfull programの同値性やhidden入力の安全性を主張しません。新しい入力経路、runner、依存、生成物形式を導入した場合だけ、事前に登録したcanaryを実行します。同じ係数やseedを変えただけで全fleetを再生しません。

`exploration-check`はIssueの許可範囲、family、mechanism/input axis、予算、停止条件、baseline、既存signatureとの距離を確認します。LLMが「仮説は尽きた」と判断してseed averagingへ移ることを許可しません。候補がなくなった状態は`WAITING_FOR_USER_ISSUE`または`blocked`であり、既存Issueの再現、失敗したattemptの回復、未実行のready nodeは継続できます。

多様性はモデル名の数ではなく、独立した機構・入力・評価 profileで数えます。表形式/木、特徴抽出＋軽量head、transfer/fine-tuning、self-supervised、構造化推論、検索/最適化、適応的計算、simulation policy、RLを候補軸にできます。RLはstate/action/reward、offline/online評価、安価な非RL baseline、計算量と安全制約を満たすときだけ候補です。RLの実装を汎用templateへ同梱せず、Issueで採用理由と実験契約を定義します。

部分学習は失敗でも採用でもありません。`budget_exhausted_inconclusive`として、到達step、wall time、学習曲線、checkpoint、未完了範囲を保存します。checkpointは実装が使用する範囲でmodel、optimizer、scheduler、AMP scaler、乱数状態、sampler位置、config、data/code snapshotを含め、使用しない状態を捏造しません。`fidelity-check`は宣言されたmanifestのcomponent、lineage、required stateを検証するだけで、実際のrestore semanticsは競技固有worker adapterが検査します。保存した特徴や凍結encoderを別headへ再利用する場合は、元checkpointと変換のSHAをrecordへ結びます。

## validation、状態圧縮、締切

候補を実行する前に`validation-plan`を作り、schema、metric、リーク境界、入力・出力、有限値、replay、stress、submissionの順序と停止条件を固定します。差分検証は依存artifactのSHAを使って再利用し、docsや日報だけの変更では科学的実験を無効化しません。新しいruntime failure class、入力経路、コード・依存変更にだけcanaryを追加します。

```bash
PYTHONPATH=. python3 -m tools.competition_automation fidelity-check automation/fidelity_plan.json --root .
PYTHONPATH=. python3 -m tools.competition_automation exploration-check automation/exploration_plan.json --root .
PYTHONPATH=. python3 -m tools.competition_automation validation-plan automation/validation_plan.json --root .
PYTHONPATH=. python3 -m tools.competition_automation validation-run \
  automation/validation_plan.json CHECK_ID --root . \
  --report validation/reports/CHECK_ID.json \
  --evidence validation/process/CHECK_ID.json
PYTHONPATH=. python3 -m tools.competition_automation validation-plan automation/validation_plan.json --root .
PYTHONPATH=. python3 -m tools.competition_automation state-compact \
  automation/work_queue.json --archive automation/archive/queue-001.json \
  --output automation/work_queue.compact.json
PYTHONPATH=. python3 -m tools.competition_automation queue-plan \
  automation/work_queue.json --quota automation/quota.json \
  --ledger automation/ledger.jsonl --root .
```

`validation-run`はplanで`RUN_REQUIRED`になった一つのcheckだけをbounded executionし、新規report/process evidenceをreceiptへ追加します。receiptを`automation/validation_plan.json`の`receipts`へ保存してhashを更新した後、`validation-plan`を再実行してください。`CACHED_PASS`は再実行せず、`PENDING_CANARY`や依存不整合は別checkを先に進めます。

`state-compact`は汎用event履歴を再構築する機能ではありません。queue JSONのterminal jobだけを新規archiveへ移し、active jobが参照するdependency tombstoneをcompact outputへ残します。source queueは変更せず、archive/outputを上書きせず、新しいpathとarchive/outputのSHAを返します。締切計画には学習・評価・形式検証・人間提出待ちを含め、quotaだけで完走不能な計算を開始しません。`queue-plan PLAN --quota PROFILE --ledger LEDGER --root ROOT`は検証済みquotaのtarget/reset/countをqueueへ結び付けますが、外部提出は行いません。日次quotaはdistinctで形式検証済みの候補だけを数え、同じpayload、再包装、seed-onlyで埋めません。

`tools/kaggle_elapsed_time.py`は認証・importを遅延させた一回限りのstatus collectorで、pending中に無限pollingしません。返された`next_check_at`をscheduler/heartbeatが使います。期限前は独立作業を進め、期限が来た確認は一回のbounded observationとして処理してからlocal作業を継続します。

## 採点待ち中のIssue継続と待機ゲート

`queue-plan`だけでは、queueへ登録されなかった未検証Issueを検出できません。新しい競技では`automation/issue_intake.template.json`と`automation/continuation.template.json`を適応し、次の正本を作ります。

- `automation/issue_intake.json`: ユーザーが承認した全Issueのsnapshotと、未検証cohort・機構・評価軸に分解した`scope_ids`。一つの診断の完了をIssue全体の完了にしない粒度で固定する。
- `automation/work_queue.json`: 各jobの依存・資源・時間と`kind`。`hypothesis`、`prerequisite`、`repair`、`submission`、`monitor`、`maintenance`を区別する。
- `automation/continuation.json`: intake/queueのpath・SHA、全scopeの`job_ids`と`resolution`、各jobに対応する`work_items`。この正本が存在するとruntimeで厳格なqueue接続を強制する。

初期化順は、Issue snapshot → intake → queue → controllerとruntime request → continuationです。queueにrequest自身のhashを入れず、requestがqueueのhashを、continuationがrequestのhashを持つため循環参照しません。queue更新後は影響するrequestとcontinuationを再固定します。稼働中の元request/receiptは書き換えず、registryの実予約を引き継いだ新しい計画へ進みます。

```bash
PYTHONPATH=. python3 -m tools.competition_automation work-plan \
  automation/continuation.json --root .
# 選択された科学jobは既存experiment-run、maintenanceは既存validation-runへ渡す。
# 採点確認や外部提出は既存の競技固有adapter・承認ゲートを使う。
PYTHONPATH=. python3 -m tools.competition_automation wait-check \
  automation/continuation.json --root .
```

`work-plan`は読み取り専用で、polling、sleep、実験起動、外部提出、Issue取得、automation予約を行いません。`wait-check`は同じ判定を行い、待機可能ならexit 0、未着手作業・解釈・再確認が必要ならexit 1、不正な入力ならexit 2です。controllerでも`action.type=wait`を使えば正本の待機判定を通せます。これらの結果を受けて実行基盤が次の一手を実行し、計算終了・受付・成果物取得後に再計画します。無限の自前loopは追加しません。

### work itemと検証の完了

科学jobと前提作業・必須修復には`scope_id`と`request: {path, sha256}`を持たせます。requestの`controller.action.source_issue`はintakeの元Issueと一致させ、`queue`と`queue_job_id`で同じ共有queueへ接続します。requestがない未検証scopeは`NEEDS_EXECUTABLE_PLAN`であり、検証枯渇とは扱いません。同一computationを別jobへ重複登録せず、一つのjobを依存先として共有します。

`work-plan`は実registryを読み取り、完了済みcomputationの成果物・receiptを再ハッシュして`REUSE_COMPLETED`、実行中は`JOIN_RUNNING`にします。最初の実行から資源予約を持たせることで、GPU待ち中もCPU検証を進められます。running jobをqueueから落としたり、資源予約を小さくして並行実行を通したりしてはいけません。running jobには実行基盤が利用する`next_check_at`を置き、無期限待機にしません。旧standalone runは勝手に予約付きへ改称せず、停止・完了後に移行します。

完走は仮説の採否ではありません。結果の解釈がなければ`INTERPRET_RESULTS`です。scopeの`resolution`には次のJSONのpath/SHAを指定します。

```json
{
  "scope_id": "issue-1-mechanism",
  "source_issue": {"number": 1, "snapshot": {"path": "research/issues/1.json", "sha256": "..."}},
  "outcome": "inconclusive",
  "reason": "固定した診断は終了。独立CVやLB改善は未証明。",
  "artifacts": [{"path": "results/experiment/attempt-001/metrics.json", "sha256": "..."}]
}
```

`outcome`は`supported`、`refuted`、`inconclusive`だけです。該当scopeの`hypothesis` jobが実際に生成した成果物と照合し、log・maintenance receipt・別scopeの結果で完了させません。部分予算の結果を`inconclusive`として整理しても、そのscope以外の未検証範囲やfamily全体を否定したことにはなりません。`goal_achieved`は常にfalseで、採用・公式score・目標達成の既存ゲートは別途必要です。

maintenance itemは`scope_id: null`と`validation: {plan: {path, sha256}, check_id: "..."}`を使います。不変の`CACHED_PASS`は完了として再利用し、依存未成立のcheckを勝手に実行しません。`progress.maintenance_completed`と`progress.resolved_scientific_scopes`は別集計です。通常は仮説・前提作業をmaintenanceより優先し、必須repair、日次候補準備、実deadlineの制約は保持します。

### scoped blockerと監視

`work_items[].blockers`には次の観測JSONのpath/SHAを指定します。理由や時刻を文章だけの全体blockedにしません。

```json
{
  "kind": "score",
  "state": "waiting",
  "affected_job_ids": ["official-contrast"],
  "reason": "この比較だけが受付済み候補の公式結果を必要とする。",
  "resume_condition": "対応するsubmission IDの公式結果を取得する。",
  "next_check_at": "2026-10-16T06:15:00Z"
}
```

kindは`score`、`quota`、`approval`、`input`、`family`、stateは`waiting`または`cleared`です。quota blockerは`submission`以外へ使えません。scoreへの本当の依存、family停止・再開理由の科学的妥当性はユーザー承認と競技adapterで確認し、LLMの「待つべき」という一言を証拠にしません。controllerの旧blocked stateにも`affected_job_ids`を指定でき、`controller-check --job-id JOB`で対象外の独立jobを止めずに済みます。未指定の旧全体blockerは従来どおり維持し、意味を推測して解除しません。

監視は`kind: monitor`、`status: waiting`、timezone付き`next_check_at`とscoped blockerを持つ独立jobにします。最も古い期限到来job一件を`monitor_jobs`へ返すため、local jobがあっても無期限延期されません。呼出元は返された`monitor_timeout_seconds: 45`を守る既存adapterで一取得し、原観測保存→次回時刻/状態更新→再計画へ進みます。future pollを早めず、PENDING・通信障害を科学的失敗へ変換しません。terminalの監視は原履歴を保持してqueueから退役させます。提出jobはこの汎用plannerから自動実行せず、既存submission-planと競技固有手順でhandoffします。人間待ちは該当提出jobのapproval blockerにし、local研究を止めません。

`WAIT_ALLOWED`は全scopeの対応と証拠を照合した結果であり、「未来のあらゆる仮説が存在しない」ことの証明ではありません。intake自体のGitHub網羅性・承認・scope分解の妥当性は人間が確認します。Issue変更・ユーザーの追加承認時はintakeを更新し、scopeを無断削除して待機を通さないでください。`state-compact`を使う場合も、継続planが参照するscope/job/証拠を失わないよう新しい計画へ明示移行します。

AGENTS.mdには恒久ルールだけを残し、現行状態は上記JSONへ、時刻付き過去判断は日報・追記型decision履歴へ置きます。採点待ちの「次は新しい公式feedback後」などの記述を全Issueへの永久停止指示にしてはいけません。

## 評価分布と公式feedbackの固定

`automation/evaluation_profiles.template.json`をコピーし、評価を次の役割へ分けます。

- `diagnostic`: 安価なiteration用。最終candidate選定には使わない。
- `selection`: hidden testのgroup/time/spatial/entity shiftへ最も近い主選定profile。
- `stress`: worst fold、tail、late periodなど失敗側を測るprofile。
- `official_feedback`: Public/LB。ローカルselection metricと混ぜない。

`automation/evaluation_profile_manifest.template.json`からprofileごとのsplit・data・推論recipeを実体化してhash固定し、`controller_state.json`の`evaluation_policy`へselection profile、required stress、familyごとの公式feedback round上限を登録します。各experimentの`evaluation_plan`が一致しなければ、controllerは`SELECTION_DISTRIBUTION_MISMATCH`、`STRESS_PROFILE_MISSING`、`OFFICIAL_FEEDBACK_BUDGET_EXCEEDED`で停止します。

## championと最終portfolio

`automation/selection_plan.template.json`を`automation/selection_plan.json`へコピーし、候補を一括登録します。

```bash
PYTHONPATH=. python3 -m tools.competition_automation selection-check \
  automation/selection_plan.json --root .
```

schema v2のselection planでは、`automation/candidate_result_evidence.template.json`と`automation/eligibility_evidence.template.json`から候補ごとの証拠を作り、hashで固定します。宣言したselection/stress score、観測数、distribution、group key、intervalの比較champion、prediction相関の比較champion、artifact/family/lineage/complexityが証拠JSONと一致しなければFAILです。`n_observations=0`のprofileは選抜根拠になりません。

現行templateの`require_matched_scored_cohort=true`では、selection/stress profileごとに、順序付き採点行と正解（または競技で定義した評価状態）を固定した`.jsonl`ファイルを作り、plan内のpath/SHAと候補result evidenceの`scored_cohort_sha256`を一致させます。1行を`{"row_key":"一意な評価行ID","reference":正解または評価状態}`とし、重複ID・欠損reference・空ファイルを拒否します。候補の`n_observations`は実ファイルの行数と一致しなければFAILです。旧championに同じcohortでの評価がない場合、代替profileの数値を埋めて選抜を通さず、比較不能として再評価または保留します。これは共通母集団の同一性を検査するもので、score計算自体やsplitのリーク安全性は競技固有validatorで別に確認してください。既存schema v2のplanはflagを省略すると従来挙動のままです。

スコア上の`historical_best`と、規約・available-time・外部データ利用条件を確認した`verified`候補を分離します。`champion_before`、`champion_after`、`selected`はすべてeligibility evidenceが`verified`でなければなりません。family名ではなく`lineage_signature`でも多様性と公式feedback回数を制限するため、名前変更だけでbudgetを回避できません。

- complexなchallengerは、marginを超える改善に加えて区間全体が有利な場合だけchampionを置換できる。
- より単純な候補はmaterial regressionを除外できる同等候補なら置換できる。
- championとhedgeのどちらも、required stress profileのregret上限を超えてはならない。
- hedgeは別family、最大regret以内、最大prediction correlation以下でなければならない。
- hedgeの相関は`champion_after`を比較対象にし、champion変更後の古い相関値を流用しない。
- 最終提出が人間操作でも、提出前のplan hashと変更理由を残す。

契約の`policy.selection_proof_required_for_adoption=true`の場合、通常の`gate`による`adopted`判定にも、record ID・family・提出artifact SHAが一致するhash固定済みselection planが必要です。公式スコアが付いただけでは自動採用されません。

## 競技終了とretrospective

目標未達のまま競技が終了した場合、completion stateは`ended`とし、`ended_at`、reason、公式終了/final snapshotの`terminal_evidence`を記録します。`ended`は成功ではなく、再開条件のない外部終了です。

Private reveal後は`automation/retrospective.template.json`を実体化します。

```bash
PYTHONPATH=. python3 -m tools.competition_automation retrospective-check \
  automation/retrospective.json --root .
```

schema v2では`automation/final_leaderboard_snapshot.template.json`の正規化行を使います。snapshotのsubmission IDとPublic/Private値をmanifestへ照合し、artifact/family/complexityもfrozen planへ照合します。`evidence_kind=official`には別のraw platform artifactのpathとSHAが必須です。ユーザー報告だけなら`user_reported_official`とし、officialへ昇格させません。相対時刻しか分からない場合は`submitted_at: null`としてfreeze timingを未検証のまま明示し、正確な時刻を推測してはいけません。

checkは全提出最良、選抜portfolio最良、verified eligible最良を分け、各regret、目標差、Public/Private順位相関、selection profileとPrivateの順位相関・pairwise concordanceを再計算します。全件のPrivate−Public差だけから原因をdistribution shiftと断定したり、定数補正を導入したりしません。CVの選抜相関が弱い場合は、そのprofileによる昇格を保留し、評価分布・機構・入力と新しい検証証拠を見直します。事後結果から過去planを黙って書き換えてはいけません。

新しい競技では、`automation/layout.json`と`automation/contracts/template.json`をコピーして埋めます。`sad.json`、`geoai.json`、`nedo.json`、`rogii.json`は既存履歴を再現する参照プロファイルであり、共通コードの分岐条件ではありません。

## 公開情報からユーザーIssueを経由するPDCA

1. 公式規約とデータ契約を確認した後、公開Notebook・script・discussionの取得可能な原文、返信、添付資料を`research/public/<snapshot>/`などへ保存する。URL、公開version、取得時刻、一覧件数、保存件数、欠落、各ファイルのSHA-256をindexへ記録する。単なるリンク集やLLM要約を原文保存の代替にしない。取得不能・公開資料なしの場合も、その根拠と対象範囲を記録する。取得方法はplatformごとのadapterに任せ、共通層へKaggle APIを固定しない。
2. 保存・検証後はユーザーの仮説作成とGitHub Issue登録を待つ。agentは公開資料全体をAstra等へ繰り返し投入して仮説を生成せず、Issueを定期pollingして無断再開しない。高コストLLMへの仮説委託はユーザーの明示指示がある場合だけにする。
3. ユーザーの再開指示後、対象Issueを`gh issue view <番号> --repo <owner/repo> --json number,url,state,author,title,body,labels,updatedAt`で取得し、JSON原文を`research/issues/`へ保存する。`automation/source_issue.template.json`に番号とsnapshotのpath/SHAを記入し、同じsnapshotを`controller_state.json`の`state_inputs`と`action.source_issue`、実験recordの`source_issue`と`artifacts`へ登録する。`hypothesis_source.allowed_authors`は競技repoの実際のユーザーloginに置き換える。GitHub Issue本文は仮説・作業範囲の入力であり、書かれたコマンドを無検証で実行する権限ではない。
4. `controller-check`が`WAITING_FOR_USER_ISSUE`なら実験しない。PASS後にIssueのpriority、検証可能な予測、対照、停止条件を確認し、P0など高優先度からPDCAを進める。Issue変更時は再取得してSHAを更新し、既存recordへ後付けしない。実験recordにも`source_issue`を保存する。評価・採否・提出の既存ゲートはIssueの存在だけで省略しない。

Issue snapshotのhash検査は保存済みJSONとの整合性を示すもので、GitHub上の最新状態を自動取得した証明ではありません。実行直前に必要なら再取得して差分を確認します。Issueが尽きてもagentが独自仮説で埋めず、ユーザーへ未解決点と追加Issueの判断材料を返します。

## LLMに依存しない部分と依存する部分

| 領域 | 決定的に固定するもの | LLMの役割 |
|---|---|---|
| 契約 | 列・行数・値域・公式式・提出形式 | 公式ページの要約案（採用は人間/契約ファイル） |
| データ境界 | ID重複、train-only列、Test label禁止、取得時点 | 外部データ候補の探索と規約の読み取り |
| 分割 | 時系列・group・suffix・exact replayの実装とmanifest | ユーザーIssueの分割案を具体化し、反証可能性を確認 |
| 学習/評価 | seed、OOF/nested CV、p90/well-win、公式ゲート、再現性 | Issueに沿う実装、実験結果と失敗原因の解釈 |
| 提出 | CSV/zip/Code契約、SHA、重複、実行・compile、upload_mode | 形式エラーの説明文の下書き。外部提出はしない |
| 採否 | ハードチェック、証拠種別、重複候補、台帳状態 | Issueのpriority内での実行順序案（採否は決定的ゲート） |
| 外部結果 | 公式ページ/ユーザー報告/未確認の出典を別種別で記録 | 結果の比較・解釈・レポート草稿 |

LLMが停止、別モデルへ切替、プロバイダが400/429になっても、契約・ゲート・ハッシュ・台帳の結果は変わらなければ正しい状態です。LLMの文章だけを根拠に、スコア、リーク安全性、提出成功、目標達成を記録してはいけません。

## 記録レコードの最小形

`automation/experiment_record.template.json`をコピーして、`ledger-append` に渡すJSONを作ります。少なくとも次を含めます。

```json
{
  "record_id": "expA00",
  "family": "baseline",
  "parent_record_id": null,
  "proposal": "ユーザー作成Issueの検証可能な仮説要約（採否の証拠ではない）",
  "source_issue": {"number": 1, "snapshot": {"path": "research/issues/1.json", "sha256": "..."}},
  "status": "succeeded",
  "requested_decision": "candidate",
  "external_submission_status": "not_requested",
  "research_manifest_path": "research/manifest.json",
  "research_manifest_sha256": "...",
  "completion_state_path": "state/completion.json",
  "completion_state_sha256": "...",
  "provenance": {
    "data": {"sha256": "..."},
    "code": {"sha256": "..."},
    "config": {"sha256": "..."},
    "split": {"sha256": "..."}
  },
  "checks": {
    "contract": {"status": "PASS", "command": "contract-check ...", "evidence_path": "evidence/contract.json", "evidence_sha256": "..."},
    "leakage": {"status": "PASS", "command": "project-validator ...", "evidence_path": "evidence/leakage.json", "evidence_sha256": "..."},
    "reproducibility": {"status": "PASS", "command": "replay ...", "evidence_path": "evidence/replay.json", "evidence_sha256": "..."},
    "submission": {"status": "PASS", "command": "submission-check ...", "evidence_path": "evidence/submission.json", "evidence_sha256": "..."}
  },
  "metrics": [
    {"name": "rmse", "value": 0.8, "evidence_kind": "proxy"}
  ],
  "artifacts": [
    {"path": "results/metrics.json", "sha256": "..."},
    {"path": "evidence/contract.json", "sha256": "..."},
    {"path": "evidence/leakage.json", "sha256": "..."},
    {"path": "evidence/replay.json", "sha256": "..."},
    {"path": "evidence/submission.json", "sha256": "..."},
    {"path": "research/manifest.json", "sha256": "..."},
    {"path": "state/completion.json", "sha256": "..."}
  ]
}
```

同じ `record_id` または同じartifact hashは、`control=true`、`control_of`（または `reference_record_id`）、理由を明示しない限り追加できません。これにより、別日付に同じzipを再パッケージして「新候補」と報告する事故を止めます。

seedだけが異なるsemantic signatureも別仮説とはみなしません。seed averagingは`base_signature`を持つ明示的controlとしてのみ保存でき、通常のcandidate/adoptedや日次quotaを通過しません。

実体まで再ハッシュする場合は、台帳追加時に `--root` を付けます。

```bash
PYTHONPATH=. python3 -m tools.competition_automation ledger-append \
  automation/ledger.jsonl experiment_record.json --root /path/to/competition
```

## 競技別の適用

- SAD: `sad.json`。時間ブロック・Test-like mask・observed/masked分解を主評価にし、外部GDO等は可用時点とchecksumを記録する。
- GeoAI: `geoai.json`。Train-only nested/domain CV、`TargetF1`と`TargetRAUC`の契約、`0.6*F1+0.4*AUC`の外部結果を別管理する。
- NEDO: `nedo.json`。exact replay、fresh seed、compile/import、公式ゲートを満たさないproxy候補を提出候補にしない。
- ROGII: `rogii.json`。well-grouped suffix holdout、hidden-test-safeな動的Code Competition、`TVT`/same-ID lookup禁止を固定する。

この層は精度を自動的に上げるものではありません。精度改善の前提である「何を比較しているか」「何が公式結果か」「同じ候補を再提出していないか」を壊さないための基盤です。
