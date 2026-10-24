#!/usr/bin/env bash
# Register the versioned repo skills without copying or replacing user skills.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$DEST" "$ROOT/.claude/skills"
for name in competition-bootstrap competition-pdca; do
    source="$ROOT/.agents/skills/$name"
    target="$DEST/$name"
    if [[ -e "$target" || -L "$target" ]]; then
        if [[ ! -L "$target" || "$(readlink "$target")" != "$source" ]]; then
            echo "Refusing to replace existing skill: $target" >&2
            exit 1
        fi
    else
        ln -s "$source" "$target"
    fi
    alias="$ROOT/.claude/skills/$name"
    if [[ ! -e "$alias" && ! -L "$alias" ]]; then
        ln -s "../../.agents/skills/$name" "$alias"
    fi
    echo "Registered $name -> $source"
done
