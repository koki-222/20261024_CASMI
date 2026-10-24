---
name: wiki
description: コンペ知識を knowledge/ ストック層（原子ページ + INDEX）に効率よく蓄積・検索する。daily_report のフローから再利用可能な知見を蒸留して昇格させ、重複を排除しながら最新化する。知見が出たとき・週次整理・過去知見を引きたいときに使う。
argument-hint: "[add <トピック> | find <クエリ> | promote | consolidate / 空欄でINDEX確認]"
---

# Knowledge Wiki Skill

`knowledge/` は **リポジトリ内の明示的な .md ファイル**で構成する見える知識資産（Claude Code の `memory/` とは別物。memory はセッション横断の個人メモ、こちらは commit して共有するコンペ知識）。

設計思想・ページ作法は `knowledge/README.md` を参照。本スキルはその運用を機械化する。

## サブコマンド（`$ARGUMENTS` の先頭語で分岐）

### 空欄: 現状確認
1. `knowledge/INDEX.md` を読む（全ページは読まない）
2. カテゴリ別のページ数と status 分布を要約
3. **知識の穴を提案**: 直近 daily_report に出ているのに未昇格の知見、`idea`/`testing` のまま放置されているページ

### `add <トピック or 知見>`: 蒸留して昇格
1. 対象の知見を特定（`$ARGUMENTS` の内容、または直近 daily_report / SESSION_NOTES から）
2. **既存ページとの重複チェック**: INDEX を見て、同じトピックの既存ページがあれば**新規作成せず更新**する
3. カテゴリを判定（technique / data / error / decision / 必要なら新設）
4. `_template.md` の frontmatter に従ってページを作成 or 更新:
   - `id` はファイル名と一致（kebab-case）
   - `source` に出典（daily_report 日付 / exp フォルダ）を必ず残す
   - `status` を適切に（効果確認済みなら `validated`、棄却なら `rejected`）
   - `impact` に数値（CV/LB差分）があれば記入
   - 関連ページを `links` と本文の `[[id]]` で繋ぐ
5. **本文は蒸留**: 要点→根拠→使い方→注意。daily_report の生ログをコピペしない
6. `INDEX.md` に1行追記（該当カテゴリ節へ、フック付き）

### `find <クエリ>`: 検索してロード
1. `INDEX.md` を読み、クエリに関連するページをフックから2-3個選ぶ
2. 選んだページ**だけ**ロードして要約
3. 関連 `[[links]]` があれば辿るか提案

### `promote`: daily_report から拾う
1. 最新の `daily_reports/*.md` を読む
2. 「再利用可能な結論」になっている知見を抽出（単発の作業ログは除く）
3. 既存ページと突き合わせ、未昇格のものを**ページ案として列挙**
4. ユーザー確認の上で `add` 相当を実行

### `consolidate`: 整理（週次目安）
1. 全ページの frontmatter を一括ロード（本文は必要分だけ）
2. 検出して提案:
   - **重複**: 同一トピックの複数ページ → 統合
   - **矛盾**: 相反する結論 → どちらが新しい/正しいか確認
   - **陳腐化**: 前提が変わったページ → `stale` 化
   - **孤立**: どこからもリンクされていないページ → 関連付け
3. `INDEX.md` を**再生成**（ページ数・最終整理日を更新）

## 原則

- **まず daily_report、次に knowledge**。フローを飛ばして直接 knowledge に書かない（出典が消えるため）
- **原子的に**。1ページ1トピック。大きくなったら分割
- **棄却知見も残す**（`rejected`）。「効かなかった」も価値ある知識
- **INDEX を常に最新に**。ページを足したら必ず INDEX に1行
- 中身を引くときは **INDEX → 該当ページのみ** の順。全ページ走査しない
