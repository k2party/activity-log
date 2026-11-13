# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 概要

睡眠・食事・服薬・外出などの行動を記録し、週間帳票 PDF / CSV を出力する Web アプリ。
**Python 標準ライブラリのみ**（外部パッケージ禁止）+ SQLite。フロントエンドもビルド工程なしの素の HTML/JS。
UI 文言・エラーメッセージ・コメント・コミットメッセージは日本語。

## コマンド

```bash
python3 app/server.py                       # http://127.0.0.1:8000/ （DB: data/activity_log.db）
python3 app/server.py --port 8001 --db /tmp/test.db   # 使い捨て DB で動作確認
docker compose up -d --build                # コンテナで起動（127.0.0.1:8000）
podman build -t localhost/activity-log-app:latest .   # Podman Quadlet 用イメージ
```

テスト・リンター・ビルドは存在しない。動作確認はサーバーを起動してブラウザ / curl で行う。
更新系 API（POST/PUT/DELETE）は `X-Requested-With: fetch` ヘッダーが必須（CSRF 対策）。
初期管理者は `admin` / `actAdmin`。

## アーキテクチャ

- `app/server.py` — `http.server` の `BaseHTTPRequestHandler` ベース。`do_GET`/`do_POST`/`do_PUT`/`do_DELETE` 内の routes dict・正規表現でルーティングし、`handle_errors` で `ValidationError`/`HttpError` を JSON エラーに変換する。DB スキーマ定義とマイグレーションもここにある。
- `app/auth.py` — PBKDF2 パスワードハッシュ、セッショントークン（DB には SHA-256 のみ保存）、ログイン試行制限。
- `app/pdf_report.py` — PDF を自前で組み立てる（ライブラリ不使用）。日本語は非埋め込みの標準フォント（HeiseiKakuGo-W5）を参照。1週（月〜日）＝A4横1ページ。
- `app/index.html` — 行動記録画面。JS はインライン。CSV 出力はクライアント側で生成する。
- `login.html` / `users.html` — ログイン、ユーザ管理（管理者のみ）。

### 重要な横断仕様

- **睡眠の「属する日」**：就寝が 18:00 以降で日をまたぐ（起床が未入力、または翌日以降）場合は翌日の記録として扱い、24:00 前に起床した場合は就寝日の記録とする。このロジックは **`pdf_report.py` の `belongs_date`（Python）と `index.html` の JS（`SLEEP_DAY_START`）の両方に実装**されており、一覧・PDF 帳票・CSV の基準日がすべて一致している必要がある。変更時は両方を合わせて修正すること。期間取得 API も前日 18:00 以降の就寝を含めて取得する。
- 種類（`CATEGORIES`）は `server.py` と `pdf_report.py`（`CATEGORY_JA`）、`index.html`（`CAT`・CSS 色・ボタン）にそれぞれ定義があり、DB の CHECK 制約にも埋め込まれている。服薬（`medicine`）は終了時刻を持たない（`NO_END_CATEGORIES`）。
- 日時は文字列 `YYYY-MM-DD HH:MM`（ローカル時刻）で保存する。

### DB スキーマとマイグレーション

`PRAGMA user_version` でバージョン管理（現在 `SCHEMA_VERSION = 5`）。起動時の `init_db()` が自動移行する。
スキーマを変更するときは `SCHEMA_VERSION` を上げ、`migrate_X_to_Y_script()` を追加して `init_db()` に段階的な移行を足す。CHECK 制約の変更などはテーブルを作り直して（`activities_new` → RENAME）データを移す方式を取っている。`create_script()` は常に最新スキーマを作る。

### デプロイ上の注意

- `Dockerfile` は `app/` 内のファイルを**個別に列挙して COPY** している。`app/` にファイルを追加したら Dockerfile も更新すること。
- コンテナは `TRUST_PROXY=1` でリバースプロキシ配下を想定。Podman Quadlet 定義は `podman/`（名前は `diary`）。
- README.md にユーザー向けの機能・API 一覧があるので、機能や API を変更したら README も更新する。
