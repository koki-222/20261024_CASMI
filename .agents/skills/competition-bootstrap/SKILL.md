---
name: competition-bootstrap
description: コンペURLとユーザー指定の移植先パスを受け、固定の汎用repoから移植・公開情報収集・push・ChatGPT Pro向けIssue整理プロンプト提供まで進める。移植先未指定なら質問する。学習・LB提出・仮説起案は行わない。
---

# コンペの初回導入

## 入力と固定の移植元

汎用repoの移植元は次の絶対パスに固定する。cwd、競技repo内のコピー、このSKILLの配置場所から別の移植元を推測しない。固定先が存在しない/読めない場合は停止し、別repoへ自動fallbackしない。参照元として読み、移植先として変更しない。

```text
/Users/kokiimai/Documents/2026_workspace/2_Dev/202608_comp_template
```

入力はコンペURLと**ユーザーが明示した移植先ディレクトリの絶対パス**。不足項目をまとめて質問し、回答前にディレクトリ作成・コピー・初期化・情報収集を開始しない。移植先未指定時の質問は「移植先ディレクトリの絶対パスを指定してください。汎用repoは上記の固定パスを使用します。」。cwdや競技slug、過去の別競技の設定で補わない。同じ依頼ですでに移植先を明示済みなら再質問しない。相対パスなら絶対パスを求める。Git remoteは移植先の設定を確認し、不明ならpush前に尋ねる。モデルや数値目標はこの段階で選ばない。

1. 固定の移植元にある`AGENTS.md`と`docs/workflow/BOOTSTRAP.md`を読む。移植元と移植先のcwd、branch、差分を区別して確認する。移植先パスはsymlinkを解決し、移植元と同一・移植元配下・移植元を含む親なら拒否する。既存の非空ディレクトリは新規移植せず、内容と差分を提示して明示的な移行指示を待つ。稼働中の競技を再初期化しない。
2. 固定の移植元から、確認済みの移植先へ必要な基盤を適応する。以後の競技用コマンドの作業ディレクトリと`--root`はこの移植先に固定する。既存データ/提出/実験を上書きしない。templateのoriginへ競技資料をpushしない。
3. 移植先で`bootstrap-init URL --root .`を実行。契約、依存、data/metric/提出adapter、規約と評価設計を実競技へ適応する。古い競技contractやreferenceのモデルを既定にしない。
4. ここで停止せず、公式情報、Code/Notebook、Discussion全ページ・返信・添付、model/教師/data、関連論文、過去上位解法を収集する。原文・Version・時刻・出典系統・SHAを保存。収集不能/再配布不可は明示し、網羅と断言しない。詳細schemaは手順書に従う。
5. 適法にpushできる原本と索引、適応報告、評価設計、coverage、主要系統の比較をpackageへ固定する。科学的な仮説起案/学習/公開Notebook実行/LB提出はしない。read-onlyなデータ契約・形式smokeは可。
6. `bootstrap-ready research/bootstrap_package.json --root .`。変更をレビューし、明示したパスだけstage、secret検査とテストを通してcommit。push先/branchを確認し、`bootstrap-publish --root . --remote origin --branch BRANCH --authorize-push`を実行。失敗/受付不明なら成功扱いせず、force/retry連投しない。
7. remote commit一致後にだけ`bootstrap-handoff --root .`。返ったプロンプト全文をチャットに提供し、repo/commit/資料リンク、収集の限界、ChatGPTでユーザーがProを選ぶこと、Issue整理後の合図「PDCA開始」を伝える。リンクだけでなく貼り付け可能な全文を渡す。

外部書き込み・規約同意・認証・公開先・有料資源は、各ツールの権限とユーザーの依頼を越えない。GitHub repoがない/公開可否が不明なら、その不足だけを報告して止める。仮説生成やPDCAを先走らない。

CLIは移植先で`python3 -m tools.competition_automation COMMAND ...`として使う。収集そのものは利用可能な正規コネクタ/CLI/browserをagentが操作する。管理CLIが全サイトを自動取得できるとは扱わない。
