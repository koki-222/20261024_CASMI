# Knowledge Index

> 検索用の目次。**中身を読む前にまずここを見る** → フックで関連ページを選んで、そのページだけロードする。
> 知見は daily_report に書いてから蒸留して該当ページへ昇格する（フロー→ストック）。蓄積・整理は `/wiki`。
> 各行: `- [title](path) — フック（status / impact）`

ページ数: 0 ／ 最終整理: -

## technique（手法）
<!-- 効いた/効かなかった手法。例: - [Dihedral TTA 8-way](technique/tta-dihedral.md) — seg推論で+0.4% Dice、低コスト（validated / CV +0.004） -->

## data（データの性質・fold・リーク）
<!-- 例: - [患者単位リーク](data/fold-leak-patient.md) — 同一患者がtrain/valに跨るとCV楽観。GroupKFoldで回避（validated） -->

## error（エラーパターンと対処）
<!-- 例: - [マスク2pxずれ](error/mask-shift-2px.md) — resize補間でラベルが2pxずれる。NEAREST固定で解消（validated） -->

## decision（方針判断と理由）
<!-- 例: - [aux loss を外す](decision/drop-aux-loss.md) — 収束は速いがCV悪化のため不採用（rejected） -->
