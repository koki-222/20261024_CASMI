#!/usr/bin/env bash
#==============================================================================#
# install.sh — このリポジトリで git の pre-commit バカ除けを有効化する
#
#   .git/hooks/ は配布できない（git 管理外）ため、tracked な tools/git-hooks/ を
#   core.hooksPath に向けて使う。クローンごとに 1 回だけ実行すればよい。
#
#   使い方:  bash tools/git-hooks/install.sh
#   無効化:  git config --unset core.hooksPath
#==============================================================================#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(git -C "$HERE" rev-parse --show-toplevel)"

# 実行ビットを確実に立てる（チェックアウト環境差対策）
chmod +x "$HERE/pre-commit"

# core.hooksPath は working-tree ルートからの相対で解決される
git -C "$ROOT" config core.hooksPath "tools/git-hooks"

echo "[OK] core.hooksPath = tools/git-hooks"
echo "     → このリポジトリの git commit で pre-commit（秘密ファイル検知）が走ります。"
echo "確認:  git -C \"$ROOT\" config core.hooksPath"
