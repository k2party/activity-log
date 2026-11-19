#!/usr/bin/env bash
# Quadlet 定義を ~/.config/containers/systemd/ にコピーして Pod を（再）起動する（rootless）
#   使い方: podman/deploy.sh [--llm [モデル名]]
#     --llm [モデル名] … ローカル LLM（Ollama）のコンテナ diary-llm も配置し、モデルを取得して、AI 要約に使う
#             （配置先の diary-app.container で DIARY_LLM_URL・LLM_MODEL を有効にする）。
#             モデル名を省略すると gemma3:1b（例: podman/deploy.sh --llm gemma3:4b）。前回のモデルは引き継がない
#     --llm を省略すると diary-llm を停止して定義を配置しない（AI 要約は Gemini になる）
#   配置先（~/.config/containers/systemd/）にあるこのアプリの定義ファイルは、毎回すべて削除してからコピーし直す。
#   ボリューム（DB・LLM のモデル）は削除しない（podman volume rm はしない）
#   事前に podman/build.sh でイメージを作成しておく
#   Gemini API キー暗号化用の秘密鍵（secret）が無ければ生成する。作成済みの場合は更新しない
#   （作り直すと登録済みの API キーが復号できなくなり、各ユーザーの再登録が必要になるため）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"
IMAGE="localhost/activity-log-app:latest"
SECRET_NAME="diary-gemini-key-secret"
SERVICE="diary-pod.service"
LLM_URL="http://127.0.0.1:11434"      # 同じ Pod 内の diary-llm

DEFAULT_LLM_MODEL="gemma3:1b"
USAGE="使い方: podman/deploy.sh [--llm [モデル名]]"

WITH_LLM=0
LLM_MODEL="$DEFAULT_LLM_MODEL"
while [ $# -gt 0 ]; do
  case "$1" in
    --llm)
      WITH_LLM=1
      # 続く引数がオプション（- で始まる）でなければモデル名とみなす
      if [ $# -gt 1 ] && [[ "$2" != -* ]]; then
        LLM_MODEL="$2"
        shift
      fi
      ;;
    *) echo "不明な引数: $1（$USAGE）" >&2; exit 1 ;;
  esac
  shift
done
# このアプリが配置する定義ファイル（deploy のたびに配置先から削除してコピーし直す）
APP_FILES=(diary.pod diary-db_data.volume diary-app.container)
LLM_FILES=(diary-llm.container diary-llm_data.volume)

if ! [[ "$LLM_MODEL" =~ ^[A-Za-z0-9._:/-]+$ ]]; then
  echo "モデル名が不正です: $LLM_MODEL（$USAGE）" >&2
  exit 1
fi

if ! podman image exists "$IMAGE"; then
  echo "イメージ $IMAGE がありません。先に podman/build.sh を実行してください" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
# --llm を省略したときは、動いているローカル LLM を止める（定義を消す前に止める。ボリュームは残る）
if [ "$WITH_LLM" = 0 ] && systemctl --user is-active --quiet diary-llm.service; then
  systemctl --user stop diary-llm.service
  echo "ローカル LLM（diary-llm）を停止しました（モデルのボリューム diary-llm_data は残しています）"
fi
# 配置先の定義ファイルをすべて削除する（--llm を省略した場合は diary-llm の定義も消える）
for f in "${APP_FILES[@]}" "${LLM_FILES[@]}"; do
  rm -f "$DEST_DIR/$f"
done
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

# コピー先の .container で Secret= 行を有効にする。ローカル LLM を配置するときは DIARY_LLM_URL・LLM_MODEL も有効にする
# （リポジトリのファイルは変更せず、コピー先だけを書き換える）
llm_sed=()
if [ "$WITH_LLM" = 1 ]; then
  llm_sed=(-e "s|^#Environment=DIARY_LLM_URL=.*|Environment=DIARY_LLM_URL=$LLM_URL|"
           -e "s|^#Environment=LLM_MODEL=.*|Environment=LLM_MODEL=$LLM_MODEL|")
fi
sed -e "s|^#Secret=$SECRET_NAME,|Secret=$SECRET_NAME,|" "${llm_sed[@]}" \
  "$SCRIPT_DIR/diary-app.container" > "$DEST_DIR/diary-app.container"
chmod 644 "$DEST_DIR/diary-app.container"
if [ "$WITH_LLM" = 1 ]; then
  install -m 644 "${LLM_FILES[@]/#/$SCRIPT_DIR/}" "$DEST_DIR/"
  echo "ローカル LLM（diary-llm）も配置し、AI 要約に使います（モデル $LLM_MODEL。初回は Ollama のイメージ取得に時間がかかります）"
fi
echo "Quadlet 定義をコピーしました: $DEST_DIR"

systemctl --user daemon-reload
# 起動していなければ起動、起動中なら新しいイメージ・設定で再起動する（--llm を省略した場合は diary-llm なしで起動する）
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
