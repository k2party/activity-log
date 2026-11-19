#!/usr/bin/env bash
# 行動記録メモのコンテナイメージをビルドする（Podman Quadlet 用）
#   使い方: podman/build.sh
set -euo pipefail

IMAGE="localhost/activity-log-app:latest"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "イメージをビルドします: $IMAGE"
podman build -t "$IMAGE" "$ROOT_DIR"

# 置き換えられて名前の無くなった古いイメージを掃除する
podman image prune -f >/dev/null || true

echo "ビルド完了: $IMAGE"
echo "起動中のアプリに反映するには podman/deploy.sh を実行してください"
