#!/usr/bin/env bash
set -euo pipefail

# The CLI writes the same stdout/stderr to a persistent file in the bind-mounted local/.
shopt -s nullglob
logs=(local/logs/*.log)
if (("${#logs[@]}" == 0)); then
  echo "保存された実行ログがありません（local/logs/*.log）。" >&2
  exit 1
fi
latest=$(ls -t -- "${logs[@]}" | head -n 1)
echo "実行ログ: $latest" >&2
tail -n +1 -f -- "$latest"
