#!/usr/bin/env bash
# Quadlet 定義を ~/.config/containers/systemd/ にコピーして Pod を（再）起動する（rootless）
#   使い方: podman/deploy.sh [--llm]
#     --llm … ローカル LLM（Ollama）のコンテナ diary-llm も配置し、既定のモデルを取得する。
#             一度配置すれば、以降は --llm なしでも更新する。モデルは環境変数 LLM_MODEL で変更できる（既定 gemma3:1b）
#   事前に podman/build.sh でイメージを作成しておく
#   Gemini API キー暗号化用の秘密鍵（secret）が無ければ生成する。作成済みの場合は更新しない
#   （作り直すと登録済みの API キーが復号できなくなり、各ユーザーの再登録が必要になるため）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"
IMAGE="localhost/activity-log-app:latest"
SECRET_NAME="diary-gemini-key-secret"
SERVICE="diary-pod.service"
LLM_MODEL="${LLM_MODEL:-gemma3:1b}"   # ローカル LLM で既定に使うモデル

WITH_LLM=0
for arg in "$@"; do
  case "$arg" in
    --llm) WITH_LLM=1 ;;
    *) echo "不明なオプション: $arg（使い方: podman/deploy.sh [--llm]）" >&2; exit 1 ;;
  esac
done
# 配置済みならローカル LLM も更新する
[ -f "$DEST_DIR/diary-llm.container" ] && WITH_LLM=1

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
if [ "$WITH_LLM" = 1 ]; then
  install -m 644 "$SCRIPT_DIR/diary-llm.container" "$SCRIPT_DIR/diary-llm_data.volume" "$DEST_DIR/"
  echo "ローカル LLM（diary-llm）も配置します（初回は Ollama のイメージ取得に時間がかかります）"
fi
echo "Quadlet 定義をコピーしました: $DEST_DIR"

systemctl --user daemon-reload
# 起動していなければ起動、起動中なら新しいイメージ・設定で再起動する
systemctl --user restart "$SERVICE"
echo "$SERVICE を（再）起動しました"
# 再起動で使われなくなったこのアプリの古いイメージを削除する（ラベルで絞り込み、他のアプリのイメージには触れない）
podman image prune -f --filter "label=activity-log.image=app" >/dev/null || true
systemctl --user --no-pager status "$SERVICE" | head -n 5 || true
if [ "$WITH_LLM" = 1 ]; then
  # Ollama が応答するまで待ってから既定のモデルを取得する（取得済みなら差分の確認だけで終わる）
  echo "ローカル LLM の起動を待っています"
  for _ in $(seq 1 60); do
    podman exec diary-llm ollama list >/dev/null 2>&1 && break
    sleep 2
  done
  if podman exec diary-llm ollama pull "$LLM_MODEL"; then
    echo "ローカル LLM のモデル $LLM_MODEL を取得しました"
  else
    echo "モデル $LLM_MODEL を取得できませんでした。後で手動で取得してください: podman exec diary-llm ollama pull $LLM_MODEL" >&2
  fi
fi

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "ログアウト後も動かし続けるには: loginctl enable-linger \"$USER\""
fi
