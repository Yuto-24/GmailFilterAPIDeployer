#!/usr/bin/env bash
set -euo pipefail

# docker compose run creates a one-off container, not a normal compose service instance.
# Include stopped containers for invocations made without --rm.
mapfile -t containers < <(docker ps -a --filter "label=com.docker.compose.project=$(docker compose config --format json | sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)" --filter "label=com.docker.compose.service=deployer" --format '{{.ID}}')
if (("${#containers[@]}" == 0)); then
  echo "deployerコンテナが見つかりません。--rm で終了済みの場合、Dockerログは残りません。" >&2
  exit 1
fi
if (("${#containers[@]}" > 1)); then
  echo "複数のdeployerコンテナが見つかりました。最新のコンテナを表示します。" >&2
fi
docker logs --tail 100 -f "${containers[0]}" 2>&1 | awk '
  /\[処理(開始|完了)\] Gmail messages.get batch/ {next}
  {print; fflush()}
'
