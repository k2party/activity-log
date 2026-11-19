#!/usr/bin/env bash
# 行動記録メモのコンテナイメージをビルドする（Podman Quadlet 用）
#   使い方: podman/build.sh
set -euo pipefail

IMAGE="localhost/activity-log-app:latest"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "イメージをビルドします: $IMAGE"
# Dockerfile は docker/ に置いている（ビルドコンテキストはリポジトリ直下）
podman build -f "$ROOT_DIR/docker/Dockerfile" --ignorefile "$ROOT_DIR/docker/Dockerfile.dockerignore" \
  -t "$IMAGE" "$ROOT_DIR"

# このアプリの古いイメージ（ビルドで名前が外れたもの）だけを削除する。
# ラベル activity-log.image=app（docker/Dockerfile）で絞り込み、同じホストの他のアプリのイメージには触れない。
# コンテナが使用中のイメージは削除されない（次回の podman/build.sh か podman/deploy.sh で削除される）
podman image prune -f --filter "label=activity-log.image=app" >/dev/null || true

echo "ビルド完了: $IMAGE"
echo "起動中のアプリに反映するには podman/deploy.sh を実行してください"
