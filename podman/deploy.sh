#!/usr/bin/env bash
# Quadlet 定義を ~/.config/containers/systemd/ にコピーして Pod を（再）起動する（rootless）
#   使い方: podman/deploy.sh
#   事前に podman/build.sh でイメージを作成しておく
#   Gemini API キー暗号化用の秘密鍵（secret）が無ければ生成する。作成済みの場合は更新しない
#   （作り直すと登録済みの API キーが復号できなくなり、各ユーザーの再登録が必要になるため）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"
IMAGE="localhost/activity-log-app:latest"
SECRET_NAME="diary-gemini-key-secret"
SERVICE="diary-pod.service"

if ! podman image exists "$IMAGE"; then
  echo "イメージ $IMAGE がありません。先に podman/build.sh を実行してください" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
install -m 644 "$SCRIPT_DIR/diary.pod" "$SCRIPT_DIR/diary-db_data.volume" "$DEST_DIR/"

# 暗号キーの secret が無ければ生成する（作成済みなら値は変えない）
if podman secret exists "$SECRET_NAME"; then
  echo "暗号キー（secret: $SECRET_NAME）は作成済みのため、そのまま使います"
else
  # 32バイトの乱数（token_urlsafe で 43 文字）。値は画面に出さず、そのまま secret に渡す
  python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" \
    | podman secret create "$SECRET_NAME" - >/dev/null
  echo "暗号キー（secret: $SECRET_NAME）を生成しました"
fi

# コピー先の .container で Secret= 行を有効にする
sed "s|^#Secret=$SECRET_NAME,|Secret=$SECRET_NAME,|" \
  "$SCRIPT_DIR/diary-app.container" > "$DEST_DIR/diary-app.container"
chmod 644 "$DEST_DIR/diary-app.container"
echo "Quadlet 定義をコピーしました: $DEST_DIR"

systemctl --user daemon-reload
# 起動していなければ起動、起動中なら新しいイメージ・設定で再起動する
systemctl --user restart "$SERVICE"
echo "$SERVICE を（再）起動しました"
systemctl --user --no-pager status "$SERVICE" | head -n 5 || true

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "ログアウト後も動かし続けるには: loginctl enable-linger \"$USER\""
fi
