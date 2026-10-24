# 主要系統と証拠の読み方

モデル・仮説の採択は行っていない。以下は公開資料の系統整理であり、比較実験の提案や性能保証ではない。Notebookは`notebook_index.json`のversion・SHAで区別し、同一親のforkを独立な機構の証拠に数えない。作者のタイトル・記述中のLB値を独立確認した公式得点と扱わない。

|系統|入力・出力と根拠|判断上の限界|
|---|---|---|
|参照library検索|観測MS/MSをtrain/公開libraryへ照合して候補を返す。公式Class1の説明と公開cosine/analog系Notebook|転載スペクトル、同分子、同sourceの混入で過大評価しやすい。参照bank内外と教師の独立性を分ける|
|指紋予測＋構造DB検索|spectrumからmolecular fingerprintを予測してPubChem/COCONUT等の構造候補を順位付け。MassSpecGymのDeepSets retrieval例、公開FPNet系|候補生成の正解包含率が上限になる。元training codeがない公開weightはwinner適格性未解決|
|構造→MS/MS simulation|GLACIER/ICEBERG等で候補の予測spectrumと観測spectrumを比較。MITの一次READMEとGLACIER論文|候補数・energy・adduct・GPU/CPU時間に依存。MassSpecGym学習版と禁止NIST学習版を混同しない|
|SSL表現|DreaMSは未注釈MS/MSの自己教師あり学習によるspectrum representation。公式GitHubとZenodo|backboneとheadで学習露出・licenseが異なる。Zenodoのembedding/SSL weightsは各1GB超で未download・未採択|
|prior・ranker・融合|PubChem popularity、異なるengineの順位融合、再順位付け。公開prior/dual-gate/rank2系列|公開集合の人気分布と未知分子への一般化を分ける。fallbackが元予測を保護していても介入の効果が観測されたとは限らない|
|de novo生成|構造DB外を含むSMILES生成。公式Class3とMassSpecGym generation/MSNovelist|retrievalのみでは到達しない母集団がある。化学的妥当性とMRR、生成候補被覆、実行時間の証拠を分ける|
|前処理・教師品質|ピーク整列、precursor/adduct、energy、instrument/source差。公式Dataの説明|固定フィルタ閾値を既定の正解にしない。train品質列を推論時の入力と混同しない|

## 一次資料

- [MassSpecGym](https://arxiv.org/abs/2410.23326v3)はretrieval/generation/simulationを別タスクとして提供する。構造分割とbenchmarkの候補集合は本競技のhidden母集団と同一ではない。
- [GLACIER](https://arxiv.org/abs/2606.29161v2)はsingle-stageなfragment-based simulationを扱う。論文中のMassSpecGym/NIST結果はCASMI26のMRR@25や本作業の実測ではない。NIST結果は背景説明に限り、対応weightは使用しない。
- [DreaMS](https://doi.org/10.1038/s41587-025-02663-3)の一次実装READMEとMIT licenseを`public/pluskal-lab_DreaMS/`に収録。Zenodo10997887のmetadataはCC-BY-4.0とするが、各headの教師・元training recipeの適格性を一括承認していない。
- [MSNovelist](https://pmc.ncbi.nlm.nih.gov/articles/PMC9262714/)は指紋・式からのde novo生成を説明する。SIRIUSのservice依存・offline実行・モデルtraining dataは別に確認が必要。
- [CASMI2022公式結果](https://www.fiehnlab.ucdavis.edu/casmi/casmi-2022-results)と著者の手法文献を参照した。2022のformula/structure/adduct別評価を2026のMRRに換算しない。結果画像と参加者Drive資料の全本文は未取得で、上位解法全体の収集完了とはしていない。

## 旧作業の扱い

ユーザー指定の参照チャットを`read_thread`で確認し、旧CASMI checkoutとSSD archiveを読み取った。初回bootstrapでは過去実験の体系的な照合をしていなかったが、2026-10-24のユーザー追加依頼で[LEGACY_REVIEW.md](LEGACY_REVIEW.md)と`legacy/`の証拠索引を追補した。旧repoの稼働状態、採択仮説、goal、権限、timer、モデル・予測CSVは移植していない。最新catalogにある783Notebookのうち保存原本を716件照合できた（有効本文715件、空container1件）。705件はcatalogのlastRunTimeと一致、11件は旧版。これは本文全体の意味を新たにレビューした件数ではなく、最新版や公式deployの保証でもない。

既知の未解決点は、trainの元acquisitionまでのcrosswalk、source名をまたぐ転載、公開weightの元training recipe、構造DB外母集団の代表性、public反復選択の露出、実際のscorer deployment pinである。これらと公開手法の比較からIssueを整理する判断はPro・ユーザーへ引き渡す。
