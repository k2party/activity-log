# activity-log（行動記録メモ）

睡眠・食事・服薬・外出などの日々の行動を記録し、週間帳票（PDF）として出力できる Web アプリです。
サーバーは **Python 標準ライブラリのみ**で動作し、データは **SQLite** に保存します（外部パッケージ不要）。

## 主な機能

- **行動記録** — 種類ごとに開始・終了日時とメモ（20文字まで）を記録
  | 種類 | 内容 |
  | --- | --- |
  | 睡眠 (`sleep`) | 就寝〜起床。起床は睡眠の終了時刻として後から入力。18:00 以降の就寝は翌日の記録として表示（起床が 24:00 より前なら当日） |
  | 食事 (`meal`) | 開始〜終了・メモ |
  | 服薬 (`medicine`) | 服薬時刻のみ（終了時刻なし）・メモ |
  | 外出 (`out`) | 開始〜終了・行先 |
  | その他 (`other`) | 開始〜終了・内容 |
- **1日の感想** — 日付ごとに1件（1000文字まで）
- **入力候補** — 最近使ったメモ・行先を候補として表示
- **週間帳票 PDF** — 指定期間を含む月〜日を1ページ（A4横）として出力
- **ログイン / ユーザー管理** — 各ユーザーは自分の記録のみ閲覧・編集可能。管理者（`admin`）はユーザーの追加・変更・削除が可能

## 必要環境

- Python 3（Docker イメージは `python:3.14-slim` を使用）
- または Docker / Docker Compose

## 起動方法

### ローカルで直接起動

```bash
python3 app/server.py
# → http://127.0.0.1:8000/
```

DB は既定で `data/activity_log.db` に作成されます（`data/` の中身は git 管理外）。

オプション（環境変数でも指定可能。コマンドライン引数が優先）:

| 引数 | 環境変数 | 既定値 | 説明 |
| --- | --- | --- | --- |
| `--host` | `HOST` | `127.0.0.1` | 待ち受けアドレス |
| `--port` | `PORT` | `8000` | 待ち受けポート |
| `--db` | `DB_PATH` | `data/activity_log.db` | SQLite DB ファイル（無ければ作成） |
| — | `TRUST_PROXY=1` | 無効 | リバースプロキシ配下で `X-Forwarded-For` / `X-Forwarded-Proto` を信頼する |
| — | `COOKIE_SECURE=1` | 無効 | ログイン Cookie に常に `Secure` 属性を付ける（HTTPS 運用時） |

### Docker Compose で起動

```bash
docker compose up -d --build
```

- `127.0.0.1:8000` のみに公開されます（ホスト側の nginx 等のリバースプロキシから接続する想定）。ポートは `APP_PORT` で変更できます。
- DB は名前付きボリューム `db-data`（コンテナ内 `/data/activity_log.db`）に永続化されます。
- コンテナは非 root ユーザー（UID 10001）で動作し、`/healthz` でヘルスチェックを行います。

### Podman（Quadlet）で起動

`podman/` に systemd 用の Quadlet 定義があります。

| ファイル | 内容 |
| --- | --- |
| `podman/diary.pod` | Pod `diary`。`127.0.0.1:8000` のみに公開 |
| `podman/diary-app.container` | アプリ本体（イメージ `localhost/activity-log-app:latest`、非 root・ヘルスチェック・自動再起動） |
| `podman/diary-db_data.volume` | DB 永続化用ボリューム `diary-db_data`（コンテナ内 `/data`） |

rootless の場合:

```bash
podman build -t localhost/activity-log-app:latest .
mkdir -p ~/.config/containers/systemd
cp podman/diary.pod podman/diary-app.container podman/diary-db_data.volume ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start diary-pod.service
loginctl enable-linger "$USER"   # ログアウト後も動かし続ける場合
```

rootful の場合は `/etc/containers/systemd/` に配置し、`systemctl` を `--user` なしで実行します。
イメージを更新したときは再ビルド後に `systemctl --user restart diary-pod.service` を実行してください。

## 初回ログイン

DB 作成時に管理者ユーザーが自動で作成されます。

| ユーザID | 初期パスワード |
| --- | --- |
| `admin` | `actAdmin` |

> ⚠️ 初回ログイン後、必ずパスワードを変更してください。

一般ユーザーは管理者が「ユーザ管理」画面（`/users`）から追加します。

## 画面

| パス | 内容 |
| --- | --- |
| `/` | 行動記録（要ログイン） |
| `/login` | ログイン |
| `/users` | ユーザ管理（管理者のみ） |

## API

更新系リクエスト（POST / PUT / DELETE）には CSRF 対策として `X-Requested-With: fetch` ヘッダーが必要です。
期間指定は `?from=YYYY-MM-DD&to=YYYY-MM-DD`（両端含む・最大400日）。

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET | `/healthz` | ヘルスチェック |
| POST | `/api/login` / `/api/logout` | ログイン / ログアウト |
| GET | `/api/me` | ログイン中のユーザー情報 |
| PUT | `/api/me/password` | 自分のパスワード変更 |
| GET | `/api/activities?from=&to=` | 期間内の行動記録 |
| POST | `/api/activities` | 行動記録の追加 |
| PUT / DELETE | `/api/activities/{id}` | 行動記録の更新 / 削除 |
| GET | `/api/activities/open-sleep` | 起床未入力の最新の睡眠 |
| GET | `/api/history?category=meal\|out\|other` | 入力候補（最近のメモ） |
| GET | `/api/daily-notes?from=&to=` | 期間内の感想 |
| PUT | `/api/daily-notes/{YYYY-MM-DD}` | 感想の保存（空なら削除） |
| GET | `/api/report.pdf?from=&to=` | 週間帳票 PDF |
| GET / POST | `/api/users` | ユーザー一覧 / 追加（管理者） |
| PUT / DELETE | `/api/users/{user_id}` | ユーザー更新 / 削除（管理者。`admin` は削除不可） |

## セキュリティ

- パスワードは PBKDF2-HMAC-SHA256（ソルト付き・600,000回）でハッシュ化して保存
- セッショントークンは `HttpOnly` / `SameSite=Lax` Cookie で管理し、DB には SHA-256 ハッシュのみ保存（最終アクセスから14日で失効）
- ログイン失敗が続いた場合は一時的にログインを拒否（15分間に5回）

## PDF 帳票について

各ページのヘッダは1行で、左に「行動記録（開始日～終了日）」（そのページの月曜〜日曜）、中央に「氏名：ユーザーの氏名」、
右に「出力日時：yyyy/mm/dd HH:mm:ss」（日本時間）を表示します。

日本語は PDF 標準の日本語フォント（平成角ゴシック W5）を参照しており、フォントは埋め込んでいません。
Acrobat / Chrome / Edge / macOS プレビューなど、閲覧側のフォントで表示されます。

## ファイル構成

```
app/                      アプリ本体
  server.py               HTTP サーバー・API・DB スキーマ／マイグレーション
  auth.py                 パスワードハッシュ・セッション・ログイン試行制限
  pdf_report.py           週間帳票 PDF の生成
  index.html              行動記録画面
  login.html              ログイン画面
  users.html              ユーザ管理画面
data/                     ローカル実行用の DB 置き場（中身は git 管理外）
podman/                   Podman Quadlet 定義（.pod / .container / .volume）
Dockerfile                コンテナイメージ（app/ の中身を /app にコピー）
compose.yaml              Docker Compose 定義
```

DB スキーマは `PRAGMA user_version` で管理され、起動時に自動でマイグレーションされます。
