#!/usr/bin/env bash
# 行動記録メモのコンテナイメージをビルドする（Docker Compose 用）
#   使い方: docker/build.sh
#   docker/.env が無ければ docker/.env.sample から作成する（秘密鍵は生成した値。既にあれば変更しない）
#   起動・更新（ビルド後）: docker compose -f docker/compose.yaml up -d
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$SCRIPT_DIR/compose.yaml")
SAMPLE_SECRET="ACTIVITY_LOG_TOOL-GEMINI_API_KEY"   # docker/.env.sample の仮のキー

echo "イメージをビルドします（docker/compose.yaml）"
"${COMPOSE[@]}" build

# このアプリの古いイメージ（ビルドで名前が外れたもの）だけを削除する。
# ラベル activity-log.image=app（docker/Dockerfile）で絞り込み、同じホストの他のアプリのイメージには触れない。
# コンテナが使用中のイメージは削除されない（次回の docker/build.sh で削除される）
docker image prune -f --filter "label=activity-log.image=app" >/dev/null || true

echo "ビルド完了"

# docker/.env が無ければ docker/.env.sample から作成する（既にあれば何もしない）。
# 暗号化用の秘密鍵 GEMINI_KEY_SECRET は、サンプルの仮のキーではなく生成した値にする（値は表示しない）
ENV_FILE="$SCRIPT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  secret="$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")"
  ( umask 077; sed "s|^GEMINI_KEY_SECRET=.*|GEMINI_KEY_SECRET=$secret|" "$SCRIPT_DIR/.env.sample" > "$ENV_FILE" )
  echo "docker/.env を docker/.env.sample から作成しました（GEMINI_KEY_SECRET は生成した値）"
  echo "  この秘密鍵は変更しないでください（変更すると登録済みの Gemini API キーは全員再登録が必要になります）"
elif grep -q "^GEMINI_KEY_SECRET=$SAMPLE_SECRET\$" "$ENV_FILE"; then
  echo "注意: docker/.env の GEMINI_KEY_SECRET がサンプルの仮のキーのままです。運用を始める前に生成した値に置き換えてください" >&2
  echo "  生成: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"" >&2
fi
echo "起動・反映するには: docker compose -f $SCRIPT_DIR/compose.yaml up -d"
