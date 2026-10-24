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
