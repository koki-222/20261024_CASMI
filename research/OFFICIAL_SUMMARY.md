# 公式仕様の独自要約

2026-10-24に公式Kaggle SDKで11ページを取得した。原文は再配布せずローカル保存し、`inventory/official.json`にSHA・取得時刻と原典URLを記録する。この文書は原文の転載ではない。

## タスク・データ

[公式Data](https://www.kaggle.com/competitions/enveda-CASMI26-molecule-id-mass-spectra/data)はMS/MSから分子の2D構造を同定する課題である。複数スペクトルを分子ごとに集約し、順位付きSMILESを最大25件出力する。public libraryにある分子、構造DBにのみある分子、構造DBにない新規分子の3クラスがあり、その割合と所属は非公開。

公式file listingは`train.parquet`3,033,286,496bytes、`test.parquet`4,848,729bytes、`sample_submission.csv`43,619bytes。旧archiveの同サイズ配布物を再利用し、実バイトSHAとschemaを再確認した。公式APIはremoteの内容SHAを返さないため、サイズ一致だけで最新内容が同一だとは証明できない。

train実体は2,539,608行・18列で、`normalized_smiles`が教師。`molecule_id`/`spectrum_id`はtrainに存在しない。test実体は1,213行・400分子・12列。ページの約1,500スペクトルという概数より実体を優先する。ページにはadductを10種列挙しつつ表中にsevenとある記述不整合もあるため、許可schemaと実入力から扱いを決める。

配布testはtrain由来の動作確認用であり、提出時にhidden testへ置換される。IDから教師を引く、配布例の答えや予測CSVを持ち込む、test membershipで精度を測る、といった運用は採用しない。`ms2_mzs`と`ms2_normalized_intensities`は整列した配列。energyは`collision_energy_ev`、trainのsource・instrument・adduct記録には異質性がある。

## Metric・提出

[公式Evaluation](https://www.kaggle.com/competitions/enveda-CASMI26-molecule-id-mass-spectra/overview/evaluation)のMRR@25を最大化する。分子ごとの最初の正解順位の逆数を平均し、正解なしは0。rank1=1、rank2=0.5、rank25=0.04。stereoを除去してtautomer canonicalizationし、InChIKey14を照合する。RDKitは2026.03.3固定。

[公開metric](https://www.kaggle.com/code/metric/casmi-mean-reciprocal-rank/14)の取得metadataはversion14、lastRunTime=2026-10-21T02:31:14.183Z。sourceにはheavy-atom compositionによる安全な事前gateと、無効SMILESを不一致にする処理がある。空semicolon要素を削除してから順位を付ける。新adapterは出力品質のため空要素・無効SMILES自体を拒否するが、順位採点は同じfirst-hit semanticsである。

**公開metricの最新版と、competitionが実際にdeployしている版が同じかは未確認。** [質問751474](https://www.kaggle.com/competitions/enveda-CASMI26-molecule-id-mass-spectra/discussion/751474)も回答なし。形式smokeとsynthetic regressionは実施したが、公開Notebookの実行やofficial deploymentとの完全同値検証は行っていない。

最終CSVは`molecule_id,smiles`、実入力の全分子を各1回、semicolonで候補順位を表す。Notebook提出、Internet無効、CPU/GPUとも9時間以内、出力名`submission.csv`。[Code Requirements](https://www.kaggle.com/competitions/enveda-CASMI26-molecule-id-mass-spectra/overview/code-requirements)を参照。

## 規約・外部資産

[公式Rules](https://www.kaggle.com/competitions/enveda-CASMI26-molecule-id-mass-spectra/rules)には、競技データの非参加者への再配布禁止、winner codeのMIT、外部資産の利用可能性・再現性の条件がある。競技データや原文をこのpublic repoへ載せない。

[主催者の2026-10-17告知750330](https://www.kaggle.com/competitions/enveda-CASMI26-molecule-id-mass-spectra/discussion/750330)はCC-BY-NCなど非商用条項付きのopen external dataを許可し、NISTとその派生重みを禁止している。[Welcome](https://www.kaggle.com/competitions/enveda-CASMI26-molecule-id-mass-spectra/discussion/741359)も同日編集を明記している。

[回答750612/3541157](https://www.kaggle.com/competitions/enveda-CASMI26-molecule-id-mass-spectra/discussion/750612#3541157)はtrain.parquetだけで学習した他者weightにも元training codeへのaccessが必要と説明する。コードのMITだけでweight・教師の適格性を判断しない。exact weight、元training data/code、recipe、再配布条件、評価露出を別々に確認する。GitHubでcodeを公開したことはモデル採択の許可ではない。

## 日程・quota

開始2026-09-14、entry/team mergerは2026-12-07、最終提出2026-12-14、いずれの締切も23:59UTC。最終提出は日本時間2026-12-15 08:59。[Timeline](https://www.kaggle.com/competitions/enveda-CASMI26-molecule-id-mass-spectra/overview/timeline)を参照。

規約上は日次5提出、最終選択2件、team最大5名。今回のAPI観測では`numAllowedNow=5`だったが、これは取得時点の値であり、今後の予約や提出許可ではない。reset時刻は応答になく未確認。提出直前にfresh stateを確認する。今回は提出を行っていない。
