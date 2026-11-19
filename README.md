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
- **感想の AI 要約** — 入力中の感想（50文字以上）を Google Gemini で要約して入力欄に反映（保存前に確認・「元に戻す」可能）
  - API キーを設定したユーザーのみ使える。「AI要約」でサーバーがそのユーザーのキーで Gemini API を呼び出し、要約を直接反映する（API キー未設定のユーザーには「AI要約」ボタンを表示しない）
  - API キーは各ユーザーが画面上部の「Gemini設定」で登録・削除する（[Google AI Studio](https://aistudio.google.com/apikey) で取得）。キーは本人の要約にのみ使われ、画面・API には末尾4文字以外返さない
- **入力候補** — 最近使ったメモ・行先を候補として表示
- **週間帳票 PDF** — 指定期間を含む月〜日を1ページ（A4横）として出力
- **ログイン / ユーザー管理** — 各ユーザーは自分の記録のみ閲覧・編集可能。管理者（`admin`）はユーザーの追加・変更・削除が可能（ユーザ一覧では各ユーザーの Gemini API キーの設定有無を確認でき、設定ありの場合は `...` ＋末尾4文字を表示する。キーの登録・変更は本人のみ）
- **利用マニュアル** — 一般ユーザー向けの利用マニュアル（`app/MANUAL.html`）を、ログイン画面と行動記録画面の「マニュアル」からダウンロードできる（ログイン不要）。管理者向けマニュアル（`app/MANUAL_ADMIN.html`）は管理者のみ、ユーザ管理画面と行動記録画面の「管理者マニュアル」からダウンロードできる
- **管理者の帳票出力** — 管理者でログインすると行動記録の入力欄・一覧は表示せず、任意のユーザーを選んで指定期間の PDF 帳票・CSV を出力できる

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
| — | `GEMINI_KEY_SECRET` | 未設定 | 各ユーザーの Gemini API キーを DB に暗号化して保存するための秘密鍵（32文字以上）。未設定のときは API キーを登録できず、AI 要約は使えない（他の機能は使える） |
| — | `GEMINI_MODEL` | `gemini-flash-latest` | 要約に使う Gemini のモデル（混み合って 503 等やタイムアウトになったときは `gemini-flash-lite-latest` で1回だけ再試行する） |

> AI 要約を使うと、感想の本文が Google（Gemini）に送信されます。要約は1ユーザーあたり10分間に20回まで。
>
> 各ユーザーの Gemini API キーは、`GEMINI_KEY_SECRET` から導出した鍵で**暗号化して** DB（`users.gemini_api_key`）に保存します（HMAC-SHA256 による CTR 方式＋改ざん検知。Python 標準ライブラリのみで実装。`app/secretbox.py`）。DB ファイルやそのバックアップだけが漏れてもキーは読めません。
> - 秘密鍵は DB とは別に管理してください（DB と同じボリュームやバックアップに入れない）。生成例: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
> - 秘密鍵を変更・紛失すると、登録済みのキーは復号できなくなり「未設定」扱いになります（各ユーザーが再登録すれば使えます）。
> - 旧バージョン（平文で保存していた DB バージョン6）から更新するときは、平文のキーが残っていれば起動時に暗号化して平文の列を削除します。このとき `GEMINI_KEY_SECRET` が未設定だと起動を中止します。

### Docker Compose で起動

```bash
docker/build.sh                            # イメージをビルド（初回は docker/.env も自動作成）
docker compose -f docker/compose.yaml up -d   # 起動（アプリ更新時も build.sh の後にこれで反映）
```

- Docker で使うファイル（`Dockerfile`・`compose.yaml`・除外設定・環境変数のサンプル）は `docker/` にまとめています。ビルドはリポジトリ直下をコンテキストにして行います。
- 環境変数は `docker/.env` に書きます（git 管理外）。`docker/.env` が無ければ `docker/build.sh` が `docker/.env.sample` から作成し、`GEMINI_KEY_SECRET` には仮のキーではなく生成した値を入れます。既に `docker/.env` があれば変更しません（仮のキーのままなら警告します）。

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
| `podman/build.sh` | イメージ `localhost/activity-log-app:latest` をビルドする |
| `podman/deploy.sh` | 暗号キー `GEMINI_KEY_SECRET` を Podman secret `diary-gemini-key-secret` として生成し（作成済みなら更新しない）、Quadlet 定義を `~/.config/containers/systemd/` にコピーして `diary-pod.service` を（再）起動する |

rootless の場合:

```bash
podman/build.sh                  # イメージをビルド
podman/deploy.sh                 # 暗号キー生成（初回のみ）・配置・起動
loginctl enable-linger "$USER"   # ログアウト後も動かし続ける場合
```

アプリを更新したときは `podman/build.sh && podman/deploy.sh` を実行してください。
暗号キーを作り直す（`podman secret rm diary-gemini-key-secret` してから deploy）と、登録済みの Gemini API キーは復号できなくなり各ユーザーの再登録が必要になります。
rootful の場合はスクリプトを使わず、Quadlet 定義を `/etc/containers/systemd/` に配置して `systemctl` を `--user` なしで実行します。

## 運用（サーバー管理者向け）

### バックアップ

- データはすべて SQLite の1ファイル（コンテナ内 `/data/activity_log.db`）にあります。Podman はボリューム `diary-db_data`、Docker Compose はボリューム `db-data` です。
- 定期的にボリュームごとバックアップしてください（例：`podman volume export diary-db_data -o diary-db_data.tar`）。復元は `podman volume import diary-db_data diary-db_data.tar`（Pod を停止してから）。
- 暗号化用の秘密鍵（`GEMINI_KEY_SECRET` / secret `diary-gemini-key-secret`）は DB と別に保管し、DB と同じバックアップに入れないでください。

### 動作確認・ログ

- `/healthz` が `{"status": "ok"}` を返せば正常です（コンテナは30秒ごとに確認し、異常時は再起動します）。
- ログ：Podman は `journalctl --user -u diary-app.service`、Docker Compose は `docker compose -f docker/compose.yaml logs app`。AI 要約の失敗の詳細（Gemini API のエラー）や DB の移行結果はログに出ます。
- ログイン試行制限・要約回数制限はメモリ上にあり、再起動でリセットされます。

### 管理者のパスワードを忘れた場合

画面からは再設定できません。サーバー上で次を実行して、`admin` のパスワードを初期値（`actAdmin`）に戻してから、ログインして変更してください（コンテナの場合は Pod を停止し、ボリューム内の DB に対して実行します）。

```bash
python3 -c "import sys; sys.path.insert(0, 'app'); import auth, sqlite3; c = sqlite3.connect(sys.argv[1]); c.execute('UPDATE users SET password_hash = ? WHERE user_id = ?', (auth.hash_password('actAdmin'), 'admin')); c.commit()" data/activity_log.db
```

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
| `/manual.html` | 利用マニュアル（HTML）のダウンロード（ログイン不要） |
| `/manual_admin.html` | 管理者マニュアル（HTML）のダウンロード（管理者のみ） |

## API

更新系リクエスト（POST / PUT / DELETE）には CSRF 対策として `X-Requested-With: fetch` ヘッダーが必要です。
期間指定は `?from=YYYY-MM-DD&to=YYYY-MM-DD`（両端含む・最大400日）。

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET | `/healthz` | ヘルスチェック |
| POST | `/api/login` / `/api/logout` | ログイン / ログアウト |
| GET | `/api/me` | ログイン中のユーザー情報 |
| PUT | `/api/me/password` | 自分のパスワード変更 |
| PUT | `/api/me/gemini-key` | 自分の Gemini API キーの登録 `{"api_key": ...}`（空なら削除） |
| GET | `/api/activities?from=&to=` | 期間内の行動記録（管理者は `&user_id=` で他のユーザーを指定可） |
| POST | `/api/activities` | 行動記録の追加 |
| PUT / DELETE | `/api/activities/{id}` | 行動記録の更新 / 削除 |
| GET | `/api/activities/open-sleep` | 起床未入力の最新の睡眠 |
| GET | `/api/history?category=meal\|medicine\|out\|other` | 入力候補（最近のメモ。重複なし・新しい順・最大50件） |
| GET | `/api/daily-notes?from=&to=` | 期間内の感想（管理者は `&user_id=` で他のユーザーを指定可） |
| PUT | `/api/daily-notes/{YYYY-MM-DD}` | 感想の保存（空なら削除） |
| POST | `/api/summarize` | 文章 `{"text": ...}` を本人の API キーで Gemini に要約させ `{"summary": ...}` を返す（保存はしない。キー未登録時は 400） |
| GET | `/api/report.pdf?from=&to=` | 週間帳票 PDF（管理者は `&user_id=` で他のユーザーを指定可） |
| GET / POST | `/api/users` | ユーザー一覧 / 追加（管理者）。一覧には Gemini API キーの設定状態 `gemini_key`（`set` 設定済み / `unset` 未設定 / `nosecret` サーバーの秘密鍵が未設定で使用不可 / `invalid` 復号できず要再登録）と、設定済みの場合はキーの末尾4文字 `gemini_key_hint` を含む（キー本体は返さない） |
| PUT / DELETE | `/api/users/{user_id}` | ユーザー更新 / 削除（管理者。`admin` は削除不可） |

## セキュリティ

- パスワードは PBKDF2-HMAC-SHA256（ソルト付き・600,000回）でハッシュ化して保存
- セッショントークンは `HttpOnly` / `SameSite=Lax` Cookie で管理し、DB には SHA-256 ハッシュのみ保存（最終アクセスから14日で失効）
- ログイン失敗が続いた場合は一時的にログインを拒否（15分間に5回）

## PDF 帳票について

各ページのヘッダは1行で、左に「行動記録（yyyy/m/d～m/d）　氏名：ユーザーの氏名」（期間はそのページの月曜〜日曜。年をまたぐ週は終了日にも年を付ける）、
右に「出力日：yyyy/mm/dd HH:MM:SS」（日本時間）を表示します。

日本語は PDF 標準の日本語フォント（平成角ゴシック W5）を参照しており、フォントは埋め込んでいません。
Acrobat / Chrome / Edge / macOS プレビューなど、閲覧側のフォントで表示されます。

### ダウンロード時のファイル名

| 出力 | ファイル名 |
|---|---|
| PDF 帳票 | `行動記録_開始日_終了日_出力日時.pdf`（例：`行動記録_2026-11-09_2026-11-15_20261115-093824.pdf`） |
| CSV | `行動記録_開始日_終了日_出力日時.csv`（開始日と終了日が同じ日なら `行動記録_日付_出力日時.csv`） |

- 管理者が他のユーザーを出力した場合は、`行動記録_ユーザID_開始日_終了日_出力日時.pdf` のように「行動記録_」の後にユーザIDが入ります。
- 開始日・終了日は `YYYY-MM-DD` 形式で、画面で指定した期間がそのまま入ります（PDF の各ページは、その期間を含む月〜日の週です）。
- 出力日時は `YYYYMMDD-HHMMSS` 形式の日本時間です（PDF はサーバー、CSV は端末の時計を基準にします）。Android などでは同じ名前だと以前ダウンロードした古いファイルが開かれることがあるため、出力ごとに別の名前にしています。
- 日本語のファイル名を扱えない環境では、PDF は `activity_log_開始日_終了日_出力日時.pdf` になります。

## ファイル構成

```
app/                      アプリ本体
  server.py               HTTP サーバー・API・DB スキーマ／マイグレーション
  auth.py                 パスワードハッシュ・セッション・ログイン試行制限
  pdf_report.py           週間帳票 PDF の生成
  summarize.py            Gemini API による感想の要約
  secretbox.py            Gemini API キーの暗号化
  index.html              行動記録画面
  login.html              ログイン画面
  users.html              ユーザ管理画面
  MANUAL.html             一般ユーザー向けの利用マニュアル（/manual.html でダウンロード）
  MANUAL_ADMIN.html       管理者向けマニュアル（/manual_admin.html でダウンロード。管理者のみ）
data/                     ローカル実行用の DB 置き場（中身は git 管理外）
spec/                     設計書（BasicSpecification.md：基本設計書）
podman/                   Podman Quadlet 定義（.pod / .container / .volume）とビルド・配置スクリプト
docker/                   Docker 用のファイル
  Dockerfile              コンテナイメージ（app/ の中身を /app にコピー。Podman のビルドでも使う）
  Dockerfile.dockerignore ビルド時の除外設定（ホワイトリスト方式）
  compose.yaml            Docker Compose 定義
  .env.sample             Docker Compose 用の環境変数のサンプル（docker/.env にコピーして使う）
  build.sh                イメージをビルドする（docker/.env が無ければ .env.sample から作成。秘密鍵は生成した値）
```

DB スキーマは `PRAGMA user_version` で管理され、起動時に自動でマイグレーションされます。
