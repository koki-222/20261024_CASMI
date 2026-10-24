# CASMI26への適応報告

## 出発点・移植範囲

ユーザー指定先は`/Users/kokiimai/Documents/2026_workspace/2_Dev/20261024_CASMI`。READMEと`.git`のみの既存repoへ追加することをユーザーが明示承認した。既存READMEの2行と初期commit`76d17fa458c6d6db0433cfd4c25ed9d44bda52d9`を保持した。branchはmain、remoteは`git@github.com:koki-222/20261024_CASMI.git`、visibilityはpublicを確認。新repo作成・visibility変更は行っていない。

固定の移植元は`/Users/kokiimai/Documents/2026_workspace/2_Dev/202608_comp_template`、source HEADは`84f83415a17f83025d15c60c58482f227c0c2e37`。移植元mainにはbootstrap SKILL、agent metadata、AGENTS、README、BOOTSTRAPの5ファイルの未commit更新があった。必要な実ファイルを採用し、元HEADだけのarchiveで更新を落とさなかった。`TEMPLATE_PROVENANCE.json`にsource status、dirty diffのSHA、採用した各ファイルのSHAと宛先を記録する。

共通automation code、workflow/automation docs、SKILL、管理schema、回帰test、必要なlocale/setup補助を移植した。`CLAUDE.md`はAGENTSへの相対symlink。`reference_generic/`は既存汎用回帰用の例で、CASMIのモデルとして採択していない。

医学画像reference、シミュレーションreference、旧競技runtime、データ、weights、credential、`.git`、旧controller/SQLite/job/timer、旧提出CSVと採択Issueを丸ごとコピーしなかった。他競技contractは有効設定にせず、generic regressionのためだけに`tests/fixtures/reference_contracts/`へ隔離した。対応testのCONTRACTS参照先だけを修正した。

## CASMI専用の変更

- `automation/project.json`を指定URLでbootstrap-initし、有効contractは`automation/contracts/current.json`だけにした。CSVのstring prediction、分子単位MRR@25、最大25候補、offline Notebook制約、hidden test安全性を定義。bootstrap時点ではupload_modeをdisabledにした。
- `tools/casmi_contract.py`はread-onlyなParquet/CSV validationとdocumented metric semanticsの小さなadapter。構造canonicalizationはRDKit2026.03.3を要求し、stereo除去をtautomer化より前に行う。既存の医学モデルや重みを既定にしていない。
- 出力validatorは公式metricより厳しく、全runtime ID対応・空要素なし・有効SMILESを要求する。公式metricは無効SMILESを不一致とし、空semicolonを削除して順位を付ける。これは品質guardの差であり、無効提出まで完全に同じ挙動だと主張しない。
- 新しいAGENTSとKAGGLE_DIRECTIONにbootstrap境界、旧作業の非継承、実scorer deployment pin未確認、weightの元training code要件を明記した。旧PublicLB目標・CPU限定方針・実行権限は継承しなかった。

## データ・依存

Python3.12.11の独立`.venv`を作成した。共有したのは既存uv管理のinterpreterだけで、旧venvのsite-packagesを変更していない。最小依存はpyarrow21.0.0、rdkit2026.3.3。解決されたnumpy2.5.4、pillow13.0.0も`requirements-bootstrap.lock`で固定。polarsは利用していない。DL framework・GPU依存・model weightは未追加。

既存の正規配布archiveからsampleとtestだけを新作業先のGit対象外へコピーした。trainは外部SSD上の実体をread-onlyでSHAとParquet footer確認し、新作業先へ3GBを複製・symlink化していない。host-local pathは`.local/data_reuse.json`、公開する非レコードmetadataは`DATA_CONTRACT_SMOKE.json`。将来trainを使う際は保存manifestのhashを照合し、必要な配置を明示する。

## 検証

実施結果は`VALIDATION.json`。

- CASMI adapterのsynthetic regression3件、workflow24件、generic contract/gate等41件、計68件PASS。
- 分子400件・スペクトル1,213行のtest schema、正の有限m/z、強度範囲、配列整列、spectrum ID一意性、sample CSVの全runtime molecule ID対応をPASS。
- 追加したCASMI Python2ファイルのruff check、tools/tests compileallをPASS。
- 最初のgeneric testは省略したsetup/localeと旧contract fixturesの欠落で失敗した。必要な支持ファイルを追加し、他競技contractをtestsへ隔離して当該testを再実行しPASS。失敗を無視したりtestを削除したりしていない。

全モデル推論、公開Notebook実行、学習、scientific comparison、LB提出、official deploymentとのmetric完全parity確認は未実施。形式smokeとsynthetic testは精度改善の証拠ではない。

## 収集・公開の境界

公式必須仕様と主要host回答を保存した。catalogは783Notebook・120Discussion、保存原本716Notebook（有効本文715件・空container1件）・120Discussion（うち38件のみ今回fresh）。GetTopicの429でKaggleアクセスを終了し、retryや別APIへの迂回をしていない。その後は既存原本のoffline照合だけを行った。全fork・全最新本文の意味を読了したとは主張しない。67Notebook本文、11旧版、82旧Discussion snapshot、25画像/添付参照、winner participant資産等のgapをinventoryへ残す。

今回の収集手順scriptは`work/`のscratchに置き、packageには観測scopeと原文SHAを記録する。再利用した旧sourceにはexact scriptVersionId/versionとdigestを保持し、catalog timestamp一致と最新版確認を区別する。

競技データ・未知licenseのNotebook/Discussion原文・紙面全文・会話履歴・個人credentialはGit対象外。publicに載せるのは独自要約、version/hash索引、aggregateな形式検査報告、MITで再配布可能な3upstream READMEと対応LICENSE。READMEはGit blobを上流のexact commitと照合した。コードのMITをweight licenseやwinner適格性へ一般化していない。

再配布したupstream Markdownには元から末尾空白がある。原本SHAを維持するため整形せず、`.gitattributes`で`research/public/**`だけを空白検査対象外にした。新規code・要約・設定の空白検査と全stageのsecret検査は維持する。

公開前にpathを明示してstageし、secret hookとdiff検査を行う。`bootstrap-ready`後に通常pushし、remote commit一致を確認してからのみhandoffする。結果はhost-local workflow stateが正本であり、この文書の予定をpublish成功の証拠にしない。

## ユーザー追加依頼による過去実験レビュー

初回引き渡し後、ユーザーが過去の失敗点を考慮した対応とプロンプト再提供を明示依頼した。旧作業をread-onlyで読み、187ファイルをhost-localへ固定し、ledger1,525行、旧Issue snapshot群、保存公式観測44件、重要比較・障害16論点を整理した。83aggregate投影と177件の分数・paired分母を`tools/validate_legacy_review.py`で原本照合した。結果は`LEGACY_REVIEW.md`、`legacy/comparison_evidence.json`、`legacy/reuse_policy.json`、`legacy/exposure.json`等へ反映した。raw原本、分子ID、正解、予測、学習済み係数はGitへ公開していない。

旧current_stateの更新時刻より新しいH12utilityとH13score原本を優先し、「fresh確認集合不足」の古い阻害状態と、その後に固定gateが科学的条件で棄却された事実を分離した。H13のconfirm256消費済み、参照不在・source別の悪化、候補欠落、実装障害を評価設計とIssue起案要件へ反映した。新仮説・Issueの採択、学習、推論、公開Notebook実行、提出・新しい公式照会はしていない。

既に公開済みのpackageを更新するため、移植先の`bootstrap-ready --refresh`だけを追加した。`waiting_for_issues`で明示flagを指定した場合のみ旧publication/package/handoff receiptを履歴へ保存し、古いhandoffを無効化して再公開待ちへ戻す。稼働PDCAのresetは拒否する。移植元templateは変更していない。この境界を2回帰testで確認し、workflowは計26件PASS。初回68件のうち変更対象workflow24件を26件として再検証したもので、科学実験の件数ではない。
