"""行動記録メモ サーバー（Python標準ライブラリのみ / SQLite保存）

起動:  python3 app/server.py [--db DBファイルのパス] [--host 127.0.0.1] [--port 8000]
  --db 省略時はプロジェクト直下の data/activity_log.db（環境変数 DB_PATH があればそれ）
  例:  python3 app/server.py --db /var/lib/activity-log/activity_log.db
  Docker では --db /data/activity_log.db で起動する（docker/Dockerfile の CMD）

環境変数でも設定可能（コマンドライン引数が優先）:
  HOST, PORT, DB_PATH
  TRUST_PROXY=1    … リバースプロキシ配下。X-Forwarded-For / X-Forwarded-Proto を使う
  COOKIE_SECURE=1  … ログインCookieに Secure 属性を常に付ける（HTTPS運用時）
  DIARY_LLM_URL    … 1日の感想の AI 要約にローカル LLM（Ollama）を使う場合の接続先 URL（例: http://llm:11434）。
                     設定すると Gemini は使わず、API キーなしで全ユーザーが AI 要約を使える
  LLM_MODEL        … ローカル LLM で使うモデル（既定: gemma3:1b）
  GEMINI_MODEL     … 1日の感想の AI 要約に使う Gemini のモデル（API キーはユーザーごとに画面から設定する）
  GEMINI_KEY_SECRET … ユーザーの Gemini API キーを DB に暗号化して保存するための秘密鍵（32文字以上）。
                     未設定のときは API キーを登録できない（AI 要約以外の機能はそのまま使える）
"""
import argparse
import datetime
import json
import os
import re
import signal
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import auth
import pdf_report
import secretbox
import summarize

BASE_DIR = Path(__file__).resolve().parent
PAGES = {name: BASE_DIR / f"{name}.html" for name in ("index", "login", "users")}
MANUAL_PATH = BASE_DIR / "MANUAL.html"   # 一般ユーザー向けの利用マニュアル（/manual.html でダウンロード）
MANUAL_ADMIN_PATH = BASE_DIR / "MANUAL_ADMIN.html"   # 管理者向けマニュアル（/manual_admin.html。管理者のみ）
DB_PATH = Path(os.environ.get("DB_PATH") or BASE_DIR.parent / "data" / "activity_log.db")
TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE") == "1"
COOKIE_NAME = "alog_session"
GEMINI_SECRET = os.environ.get("GEMINI_KEY_SECRET", "").strip()   # Gemini API キーの暗号化用の秘密鍵

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
DATETIME_FMT = "%Y-%m-%d %H:%M"
MAX_NOTE = 20
MAX_RANGE_DAYS = 400

# 行動の種類。起床は単独の記録ではなく、睡眠（sleep）の終了時刻として記録する
CATEGORIES = {"sleep": "睡眠", "meal": "食事", "medicine": "服薬", "out": "外出", "other": "その他"}
NOTE_LABELS = {"meal": "メモ", "medicine": "メモ", "out": "行先", "other": "内容"}
NO_END_CATEGORIES = {"medicine"}   # 終了時刻を持たない行動（服薬は服薬時刻のみ）
MAX_COMMENT = 1000                 # 1日の感想の最大文字数

# ユーザー。管理者権限はユーザID「admin」に与える
ADMIN_USER_ID = "admin"
ADMIN_INITIAL_PASSWORD = "actAdmin"
ADMIN_INITIAL_NAME = "管理者"
USER_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,32}$")
MAX_NAME = 50
PASSWORD_MIN, PASSWORD_MAX = 8, 128
MAX_GEMINI_KEY = 200
GEMINI_KEY_RE = re.compile(r"^[\x21-\x7e]{10,%d}$" % MAX_GEMINI_KEY)   # 空白を含まない印字可能 ASCII

# ---------------------------------------------------------------------------
# スキーマ（PRAGMA user_version で管理）
#   0〜1（旧 entries テーブル）: DROP & CREATE（データは引き継がない）
#   2〜3 → 4: ユーザー・ログイン機能を追加。既存の行動記録は admin の記録として引き継ぐ
#   4 → 5: 種類に「服薬(medicine)」を追加（activities を作り直してデータを移す）、1日の感想(daily_notes)を追加
#   5 → 6: ユーザーごとの Gemini API キー（users.gemini_api_key）を追加
#   6 → 7: users.gemini_api_key を暗号化して保存する（列名はそのまま。長さの上限を暗号文に合わせる）。
#           users を作り直し、created_at・updated_at を最後の列にする
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 7
CATEGORY_SQL = ", ".join(f"'{c}'" for c in CATEGORIES)

# 感想の AI 要約に使う本人の Gemini API キー（'' は未設定）。画面・API には返さない
# バージョン6 は平文（5 → 6 の移行でのみ使う）
GEMINI_KEY_COLUMN = (f"gemini_api_key TEXT NOT NULL DEFAULT '' CHECK (length(gemini_api_key) <= {MAX_GEMINI_KEY})")


def gemini_key_enc_column(name="gemini_api_key"):
    """バージョン7 以降の列定義。secretbox で暗号化した値（ユーザIDを関連データにする）を入れる"""
    return (f"{name} TEXT NOT NULL DEFAULT '' "
            f"CHECK (length({name}) <= {secretbox.encrypted_length(MAX_GEMINI_KEY)})")


def users_table_ddl(name):
    """users テーブル（作り直し時は users_new などの名前で作ってから RENAME する）。created_at・updated_at は最後の列"""
    return f"""
-- ユーザー（ユーザID・パスワード・氏名・Gemini API キー（暗号化））
CREATE TABLE {name} (
    user_id        TEXT PRIMARY KEY,      -- ログインID（'admin' が管理者）
    password_hash  TEXT NOT NULL,         -- PBKDF2-SHA256 ハッシュ
    name           TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND {MAX_NAME}),  -- 氏名
    {gemini_key_enc_column()},
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


USERS_DDL = users_table_ddl("users") + """
-- ログインセッション（トークンはハッシュのみ保存）
CREATE TABLE sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    expires_at  TEXT NOT NULL,            -- 'YYYY-MM-DD HH:MM:SS'
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
"""


def activities_ddl(name):
    return f"""
-- 行動記録（1行 = 1つの行動）
CREATE TABLE {name} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,  -- 記録したユーザー
    category    TEXT NOT NULL CHECK (category IN ({CATEGORY_SQL})),          -- 睡眠 / 食事 / 服薬 / 外出 / その他
    start_time  TEXT NOT NULL,             -- 開始日時 'YYYY-MM-DD HH:MM'（睡眠は就寝）
    end_time    TEXT,                      -- 終了日時 'YYYY-MM-DD HH:MM'（睡眠は起床）。未入力・服薬は NULL
    note        TEXT NOT NULL DEFAULT '' CHECK (length(note) <= {MAX_NOTE}),  -- メモ・行先・内容
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (end_time IS NULL OR end_time > start_time)
);"""


DAILY_NOTES_DDL = f"""
-- 1日の感想（ユーザー × 日付 で1件）
CREATE TABLE daily_notes (
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    date        TEXT NOT NULL,             -- 'YYYY-MM-DD'
    comment     TEXT NOT NULL CHECK (length(comment) BETWEEN 1 AND {MAX_COMMENT}),
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (user_id, date)
);
"""

ACTIVITIES_INDEX_DDL = """
CREATE INDEX idx_activities_user_start ON activities(user_id, start_time);
CREATE INDEX idx_activities_user_open  ON activities(user_id, category, end_time);
"""


def admin_insert_sql():
    # ハッシュは英数字と $ のみなので SQL リテラルに埋め込んでよい
    return (f"INSERT INTO users (user_id, password_hash, name) VALUES "
            f"('{ADMIN_USER_ID}', '{auth.hash_password(ADMIN_INITIAL_PASSWORD)}', '{ADMIN_INITIAL_NAME}');")


def create_script():
    """新規作成（旧版のテーブルは削除）"""
    return f"""
BEGIN;
DROP TABLE IF EXISTS entries;
DROP TABLE IF EXISTS daily_notes;
DROP TABLE IF EXISTS activities;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS users;
{USERS_DDL}
{admin_insert_sql()}
{activities_ddl("activities")}
{ACTIVITIES_INDEX_DDL}
{DAILY_NOTES_DDL}
PRAGMA user_version = {SCHEMA_VERSION};
COMMIT;
"""


def migrate_to_4_script():
    """バージョン2・3 → 4。users を作り、activities に user_id を追加（既存の記録は admin のもの）"""
    return f"""
BEGIN;
{USERS_DDL}
{admin_insert_sql()}
{activities_ddl("activities_new")}
INSERT INTO activities_new (id, user_id, category, start_time, end_time, note, created_at, updated_at)
    SELECT id, '{ADMIN_USER_ID}', category, start_time, end_time, note, created_at, updated_at FROM activities;
DROP TABLE activities;
ALTER TABLE activities_new RENAME TO activities;
{ACTIVITIES_INDEX_DDL}
PRAGMA user_version = 4;
COMMIT;
"""


def migrate_4_to_5_script():
    """バージョン4 → 5。activities を作り直して種類「服薬」を追加し、daily_notes を作成"""
    return f"""
BEGIN;
{activities_ddl("activities_new")}
INSERT INTO activities_new (id, user_id, category, start_time, end_time, note, created_at, updated_at)
    SELECT id, user_id, category, start_time, end_time, note, created_at, updated_at FROM activities;
DROP TABLE activities;
ALTER TABLE activities_new RENAME TO activities;
{ACTIVITIES_INDEX_DDL}
{DAILY_NOTES_DDL}
PRAGMA user_version = 5;
COMMIT;
"""


def migrate_5_to_6_script():
    """バージョン5 → 6。users に Gemini API キーの列を追加"""
    return f"""
BEGIN;
ALTER TABLE users ADD COLUMN {GEMINI_KEY_COLUMN};
PRAGMA user_version = 6;
COMMIT;
"""


def migrate_6_to_7(conn):
    """バージョン6 → 7。users を作り直して gemini_api_key を暗号化した値に置き換える（列名は変えない）

    CHECK 制約（長さの上限）を暗号文に合わせて広げ、列の並びを created_at・updated_at が最後になるよう揃えるため、
    users_new を作ってデータを移し、旧テーブルを削除してから RENAME する（外部キー制約は無効のまま実行する）。
    """
    rows = conn.execute("SELECT user_id, gemini_api_key FROM users WHERE gemini_api_key <> ''").fetchall()
    if rows and not GEMINI_SECRET:
        raise SystemExit(f"暗号化されていない Gemini API キーが {len(rows)} 件あります。"
                         "環境変数 GEMINI_KEY_SECRET（32文字以上）を設定してから起動してください")
    conn.isolation_level = None   # BEGIN〜COMMIT を明示的に制御する
    conn.execute("BEGIN")
    try:
        conn.execute(users_table_ddl("users_new"))
        conn.execute("INSERT INTO users_new (user_id, password_hash, name, gemini_api_key, created_at, updated_at) "
                     "SELECT user_id, password_hash, name, '', created_at, updated_at FROM users")
        for user_id, key in rows:
            conn.execute("UPDATE users_new SET gemini_api_key = ? WHERE user_id = ?",
                         (secretbox.encrypt(key, GEMINI_SECRET, user_id), user_id))
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")
        if bad := conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError(f"users の作り直しで外部キーの不整合が発生しました: {bad[:5]}")
        conn.execute("PRAGMA user_version = 7")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("VACUUM")   # 削除した旧テーブルの平文がファイル上に残らないようにする
    return len(rows)


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = sqlite3.connect(DB_PATH)   # 移行中は外部キー制約を無効のまま実行する
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            print(f"DBを作成します（旧バージョン {version} のテーブルは削除されます）", flush=True)
            conn.executescript(create_script())
        elif version > SCHEMA_VERSION:
            raise SystemExit(f"DBのバージョン({version})がこのプログラム({SCHEMA_VERSION})より新しいため起動できません")
        else:
            if version in (2, 3):
                print(f"DBをバージョン{version}から4に更新します（ユーザー・ログイン機能を追加）", flush=True)
                conn.executescript(migrate_to_4_script())
            if version <= 4:
                print("DBをバージョン5に更新します（服薬・1日の感想を追加）", flush=True)
                conn.executescript(migrate_4_to_5_script())
            if version <= 5:
                print("DBをバージョン6に更新します（ユーザーごとの Gemini API キーを追加）", flush=True)
                conn.executescript(migrate_5_to_6_script())
            if version <= 6:
                print("DBをバージョン7に更新します（Gemini API キーを暗号化して保存）", flush=True)
                n = migrate_6_to_7(conn)
                if n:
                    print(f"  Gemini API キー {n} 件を暗号化しました", flush=True)
        if version < 4:
            print(f"管理者ユーザー: {ADMIN_USER_ID} / 初期パスワード: {ADMIN_INITIAL_PASSWORD}（ログイン後に変更してください）", flush=True)
    finally:
        conn.close()


def now_str(delta_days=0):
    return (datetime.datetime.now() + datetime.timedelta(days=delta_days)).strftime("%Y-%m-%d %H:%M:%S")


class ValidationError(Exception):
    pass


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def parse_date(val, label="日付"):
    """YYYY-MM-DD 形式かつ実在する日付かを確認する"""
    if not isinstance(val, str) or not DATE_RE.match(val):
        raise ValidationError(f"{label}の形式が不正です")
    try:
        return datetime.date.fromisoformat(val)
    except ValueError:
        raise ValidationError(f"{label}が存在しない日付です")


def parse_datetime(val, label):
    if not isinstance(val, str) or not DATETIME_RE.match(val):
        raise ValidationError(f"{label}は 'YYYY-MM-DD HH:MM' 形式で指定してください")
    try:
        return datetime.datetime.strptime(val, DATETIME_FMT)
    except ValueError:
        raise ValidationError(f"{label}が存在しない日時です")


def validate_activity(data):
    """行動記録1件の入力チェック（全項目）。DB保存用の dict を返す。"""
    if not isinstance(data, dict):
        raise ValidationError("JSONオブジェクトを送信してください")
    category = data.get("category")
    if category not in CATEGORIES:
        raise ValidationError("行動の種類が不正です")

    start_label, end_label = {"sleep": ("就寝時刻", "起床時刻"), "medicine": ("服薬時刻", "")}.get(category, ("開始時刻", "終了時刻"))
    start = parse_datetime(data.get("start_time"), start_label)
    end = None if category in NO_END_CATEGORIES else data.get("end_time")
    if end is not None:
        if parse_datetime(end, end_label) <= start:
            raise ValidationError(f"{end_label}は{start_label}より後にしてください")

    note = data.get("note") or ""
    if not isinstance(note, str):
        raise ValidationError("メモの形式が不正です")
    note = note.strip()
    if category == "sleep":
        note = ""
    elif len(note) > MAX_NOTE:
        raise ValidationError(f"{NOTE_LABELS[category]}は{MAX_NOTE}文字以内にしてください")

    return {"category": category, "start_time": data["start_time"], "end_time": end, "note": note}


def validate_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("氏名を入力してください")
    name = name.strip()
    if len(name) > MAX_NAME:
        raise ValidationError(f"氏名は{MAX_NAME}文字以内にしてください")
    return name


def validate_password(pw, label="パスワード"):
    if not isinstance(pw, str) or not (PASSWORD_MIN <= len(pw) <= PASSWORD_MAX):
        raise ValidationError(f"{label}は{PASSWORD_MIN}〜{PASSWORD_MAX}文字で入力してください")
    return pw


FIELDS = ("id", "category", "start_time", "end_time", "note")


def row_to_dict(row):
    return {k: row[k] for k in FIELDS}


def user_to_dict(row):
    return {"user_id": row["user_id"], "name": row["name"], "is_admin": row["user_id"] == ADMIN_USER_ID}


def decrypt_gemini_key(user_id, stored, log=True):
    """DB の gemini_api_key（暗号文）を復号する。未設定は ''、復号できない場合は None"""
    if not stored:
        return ""
    if not GEMINI_SECRET:
        return None   # 秘密鍵が無いと復号できない（起動時に警告済み）
    try:
        return secretbox.decrypt(stored, GEMINI_SECRET, user_id)
    except secretbox.DecryptError as e:
        # 秘密鍵を変えた場合など。本人には未設定として扱い、再登録してもらう
        if log:
            print(f"Gemini API キーを復号できません（ユーザー {user_id}）: {e}", flush=True)
        return None


def gemini_key_status(row):
    """ユーザ一覧用の Gemini API キーの状態と、設定済みなら末尾4文字

    状態は set（設定済み）/ unset（未設定）/ nosecret（登録はあるがサーバーに秘密鍵が未設定）/
    invalid（秘密鍵が違うなどで復号できない）
    """
    if row["gemini_api_key"] and not GEMINI_SECRET:
        return "nosecret", ""
    key = decrypt_gemini_key(row["user_id"], row["gemini_api_key"], log=False)
    if key is None:
        return "invalid", ""
    return ("set", key[-4:]) if key else ("unset", "")


throttle = auth.LoginThrottle()


class Handler(BaseHTTPRequestHandler):
    server_version = "ActivityLog/3.0"

    def address_string(self):
        # プロキシ配下では、ログに実際のクライアントIPを出す
        return self.client_ip()

    def client_ip(self):
        if TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    # ---- レスポンス ----
    def send_body(self, status, body, content_type, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload, headers=()):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_body(status, body, "application/json; charset=utf-8", headers)

    def send_page(self, name):
        self.send_body(200, PAGES[name].read_bytes(), "text/html; charset=utf-8",
                       [("X-Frame-Options", "DENY"), ("Referrer-Policy", "same-origin")])

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValidationError("リクエストが大きすぎます")
        try:
            return json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError:
            raise ValidationError("JSONの形式が不正です")

    def read_object(self):
        data = self.read_json()
        if not isinstance(data, dict):
            raise ValidationError("JSONオブジェクトを送信してください")
        return data

    def handle_errors(self, fn, *args):
        try:
            fn(*args)
        except ValidationError as e:
            self.send_json(400, {"error": str(e)})
        except HttpError as e:
            self.send_json(e.status, {"error": str(e)})
        except sqlite3.IntegrityError as e:
            self.log_error("DB integrity error: %s", e)
            self.send_json(400, {"error": "入力内容がデータベースの制約に違反しています"})
        except sqlite3.Error as e:
            self.log_error("DB error: %s", e)
            self.send_json(500, {"error": "データベースエラー"})

    # ---- セッション ----
    def session_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME and v:
                return v
        return None

    def current_user(self):
        """ログイン中のユーザー（dict）または None。有効期限は最終アクセスから延長する"""
        if hasattr(self, "_user"):
            return self._user
        self._user = None
        token = self.session_token()
        if token:
            th = auth.token_hash(token)
            with connect() as conn:
                row = conn.execute(
                    "SELECT u.* FROM sessions s JOIN users u ON u.user_id = s.user_id"
                    " WHERE s.token_hash = ? AND s.expires_at > ?", (th, now_str())).fetchone()
                if row:
                    conn.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                                 (now_str(auth.SESSION_DAYS), th))
                    self._user = user_to_dict(row)
        return self._user

    def require_user(self):
        user = self.current_user()
        if not user:
            raise HttpError(401, "ログインしてください")
        return user

    def require_admin(self):
        user = self.require_user()
        if not user["is_admin"]:
            raise HttpError(403, "管理者のみ利用できます")
        return user

    def is_https(self):
        return COOKIE_SECURE or (TRUST_PROXY and self.headers.get("X-Forwarded-Proto") == "https")

    def session_cookie(self, token, max_age):
        attrs = [f"{COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if self.is_https():
            attrs.append("Secure")
        return ("Set-Cookie", "; ".join(attrs))

    def check_csrf(self):
        """更新系リクエストは fetch からのもの（独自ヘッダー付き）のみ受け付ける"""
        if self.headers.get("X-Requested-With") != "fetch":
            raise HttpError(403, "不正なリクエストです")

    # ==================================================================
    # ルーティング
    # ==================================================================
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path
        # ---- 画面 ----
        if path in ("/", "/index.html"):
            return self.send_page("index") if self.current_user() else self.redirect("/login")
        if path == "/login":
            return self.redirect("/") if self.current_user() else self.send_page("login")
        if path == "/users":
            user = self.current_user()
            if not user:
                return self.redirect("/login")
            return self.send_page("users") if user["is_admin"] else self.redirect("/")
        if path == "/healthz":
            return self.handle_errors(self.healthz)
        if path == "/manual.html":
            return self.manual()
        if path == "/manual_admin.html":
            user = self.current_user()
            if not user:
                return self.redirect("/login")
            return self.manual_admin() if user["is_admin"] else self.redirect("/")
        # ---- API ----
        routes = {
            "/api/me": self.me,
            "/api/users": self.list_users,
            "/api/activities": lambda: self.list_activities(query),
            "/api/activities/open-sleep": self.open_sleep,
            "/api/history": lambda: self.history(query),
            "/api/daily-notes": lambda: self.list_daily_notes(query),
            "/api/report.pdf": lambda: self.report_pdf(query),
        }
        if path in routes:
            return self.handle_errors(routes[path])
        self.send_json(404, {"error": "見つかりません"})

    def do_POST(self):
        path = urlparse(self.path).path
        routes = {
            "/api/login": self.login,
            "/api/logout": self.logout,
            "/api/users": self.create_user,
            "/api/activities": self.create_activity,
            "/api/summarize": self.summarize_comment,
        }
        if path not in routes:
            return self.send_json(404, {"error": "見つかりません"})
        self.handle_errors(lambda: (self.check_csrf(), routes[path]()))

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/me/password":
            return self.handle_errors(lambda: (self.check_csrf(), self.change_own_password()))
        if path == "/api/me/gemini-key":
            return self.handle_errors(lambda: (self.check_csrf(), self.save_gemini_key()))
        if m := re.fullmatch(r"/api/activities/(\d+)", path):
            return self.handle_errors(lambda: (self.check_csrf(), self.update_activity(int(m.group(1)))))
        if m := re.fullmatch(r"/api/users/([^/]+)", path):
            return self.handle_errors(lambda: (self.check_csrf(), self.update_user(unquote(m.group(1)))))
        if m := re.fullmatch(r"/api/daily-notes/(\d{4}-\d{2}-\d{2})", path):
            return self.handle_errors(lambda: (self.check_csrf(), self.save_daily_note(m.group(1))))
        self.send_json(404, {"error": "見つかりません"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if m := re.fullmatch(r"/api/activities/(\d+)", path):
            return self.handle_errors(lambda: (self.check_csrf(), self.delete_activity(int(m.group(1)))))
        if m := re.fullmatch(r"/api/users/([^/]+)", path):
            return self.handle_errors(lambda: (self.check_csrf(), self.delete_user(unquote(m.group(1)))))
        self.send_json(404, {"error": "見つかりません"})

    def manual(self):
        """利用マニュアル（HTML）をダウンロードさせる。ログイン不要（ログインできない人も読めるように）"""
        self.send_body(200, MANUAL_PATH.read_bytes(), "text/html; charset=utf-8", [
            ("Content-Disposition",
             f"attachment; filename=\"activity_log_manual.html\"; filename*=UTF-8''{quote('行動記録メモ_利用マニュアル.html')}"),
        ])

    def manual_admin(self):
        """管理者向けマニュアルをダウンロードさせる。初期パスワードを含むため管理者のみ"""
        self.send_body(200, MANUAL_ADMIN_PATH.read_bytes(), "text/html; charset=utf-8", [
            ("Content-Disposition",
             f"attachment; filename=\"activity_log_manual_admin.html\"; filename*=UTF-8''{quote('行動記録メモ_管理者マニュアル.html')}"),
        ])

    def healthz(self):
        with connect() as conn:
            conn.execute("SELECT 1")
        self.send_json(200, {"status": "ok"})

    # ==================================================================
    # ログイン・ログアウト・自分の情報
    # ==================================================================
    def login(self):
        data = self.read_object()
        user_id, password = data.get("user_id"), data.get("password")
        if not isinstance(user_id, str) or not isinstance(password, str) or not user_id or not password:
            raise ValidationError("ユーザIDとパスワードを入力してください")
        key = f"{self.client_ip()}|{user_id.lower()}"
        if throttle.blocked(key):
            raise HttpError(429, "ログインの失敗が続いたため、しばらく時間をおいてから再度お試しください")
        with connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            ok = auth.verify_password(password, row["password_hash"]) if row else auth.verify_dummy(password)
            if not ok:
                throttle.fail(key)
                raise HttpError(401, "ユーザIDまたはパスワードが違います")
            throttle.reset(key)
            token, th = auth.new_session_token()
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_str(),))
            conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                         (th, row["user_id"], now_str(auth.SESSION_DAYS)))
        user = user_to_dict(row)
        # 管理者が初期パスワードのままなら画面で変更を促す
        user["initial_password"] = user["is_admin"] and password == ADMIN_INITIAL_PASSWORD
        self.send_json(200, user, [self.session_cookie(token, auth.SESSION_DAYS * 86400)])

    def logout(self):
        token = self.session_token()
        if token:
            with connect() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (auth.token_hash(token),))
        self.send_json(200, {"ok": True}, [self.session_cookie("", 0)])

    def me(self):
        user = self.require_user()
        key = self.gemini_key(user)
        local = summarize.use_local()
        # キー本体は返さず、設定の有無と末尾4文字（どのキーか見分ける用）だけ返す。
        # ローカル LLM のときは API キーなしで要約でき、Gemini の設定は使わない
        self.send_json(200, {**user, "gemini_key_set": bool(key), "gemini_key_hint": key[-4:] if key else "",
                             "summarizer": "local" if local else "gemini",
                             "summarize_enabled": local or bool(key)})

    def gemini_key(self, user):
        """本人の Gemini API キーを復号して返す。未設定・復号できない場合は ''"""
        with connect() as conn:
            row = conn.execute("SELECT gemini_api_key FROM users WHERE user_id = ?", (user["user_id"],)).fetchone()
        return (decrypt_gemini_key(user["user_id"], row["gemini_api_key"]) or "") if row else ""

    def save_gemini_key(self):
        """自分の Gemini API キーを保存（空なら削除）"""
        user = self.require_user()
        key = self.read_object().get("api_key")
        if not isinstance(key, str):
            raise ValidationError("API キーの形式が不正です")
        key = key.strip()
        if key and not GEMINI_KEY_RE.match(key):
            raise ValidationError(f"API キーは空白を含まない半角英数字・記号（10〜{MAX_GEMINI_KEY}文字）で入力してください")
        if key and not GEMINI_SECRET:
            raise ValidationError("サーバーに暗号化用の秘密鍵（GEMINI_KEY_SECRET）が設定されていないため、API キーを登録できません。管理者に連絡してください")
        stored = secretbox.encrypt(key, GEMINI_SECRET, user["user_id"]) if key else ""
        with connect() as conn:
            conn.execute("UPDATE users SET gemini_api_key = ?, updated_at = datetime('now', 'localtime') WHERE user_id = ?",
                         (stored, user["user_id"]))
        self.send_json(200, {"gemini_key_set": bool(key), "gemini_key_hint": key[-4:] if key else ""})

    def change_own_password(self):
        user = self.require_user()
        data = self.read_object()
        current = data.get("current_password")
        new = validate_password(data.get("new_password"), "新しいパスワード")
        with connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user["user_id"],)).fetchone()
            if not isinstance(current, str) or not auth.verify_password(current, row["password_hash"]):
                raise ValidationError("現在のパスワードが違います")
            conn.execute("UPDATE users SET password_hash = ?, updated_at = datetime('now', 'localtime') WHERE user_id = ?",
                         (auth.hash_password(new), user["user_id"]))
            # 他の端末のログインは無効にする
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?",
                         (user["user_id"], auth.token_hash(self.session_token())))
        self.send_json(200, {"ok": True})

    # ==================================================================
    # ユーザ管理（管理者のみ）
    # ==================================================================
    def list_users(self):
        self.require_admin()
        with connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY user_id = ? DESC, user_id",
                                (ADMIN_USER_ID,)).fetchall()
        # 管理者には Gemini API キーの設定状態と末尾4文字だけを返す（キー本体は返さない。登録・変更は本人のみ）
        result = []
        for r in rows:
            status, hint = gemini_key_status(r)
            result.append({**user_to_dict(r), "gemini_key": status, "gemini_key_hint": hint})
        self.send_json(200, result)

    def create_user(self):
        self.require_admin()
        data = self.read_object()
        user_id = data.get("user_id")
        if not isinstance(user_id, str) or not USER_ID_RE.match(user_id):
            raise ValidationError("ユーザIDは半角英数字と _ . @ - の32文字以内で入力してください")
        name = validate_name(data.get("name"))
        password = validate_password(data.get("password"))
        with connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE lower(user_id) = lower(?)", (user_id,)).fetchone():
                raise ValidationError("そのユーザIDは既に使われています")
            conn.execute("INSERT INTO users (user_id, password_hash, name) VALUES (?, ?, ?)",
                         (user_id, auth.hash_password(password), name))
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        self.send_json(201, user_to_dict(row))

    def update_user(self, user_id):
        """氏名・パスワードの変更（ユーザIDは変更不可）。パスワード変更時は対象ユーザーのログインを無効にする"""
        self.require_admin()
        data = self.read_object()
        sets, params = [], []
        if "name" in data:
            sets.append("name = ?")
            params.append(validate_name(data["name"]))
        if data.get("password"):
            sets.append("password_hash = ?")
            params.append(auth.hash_password(validate_password(data["password"])))
        if not sets:
            raise ValidationError("変更する項目がありません")
        with connect() as conn:
            cur = conn.execute(
                f"UPDATE users SET {', '.join(sets)}, updated_at = datetime('now', 'localtime') WHERE user_id = ?",
                (*params, user_id))
            if cur.rowcount == 0:
                raise HttpError(404, "ユーザーが見つかりません")
            if data.get("password"):
                conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?",
                             (user_id, auth.token_hash(self.session_token())))
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        self.send_json(200, user_to_dict(row))

    def delete_user(self, user_id):
        """ユーザーを削除する（そのユーザーの行動記録も削除される）。admin は削除不可"""
        self.require_admin()
        if user_id == ADMIN_USER_ID:
            raise ValidationError("管理者ユーザーは削除できません")
        with connect() as conn:
            cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        if cur.rowcount == 0:
            raise HttpError(404, "ユーザーが見つかりません")
        self.send_json(200, {"deleted": user_id})

    # ==================================================================
    # 行動記録（ログイン中のユーザー自身の記録のみ。閲覧・出力は管理者が任意のユーザーを指定できる）
    # ==================================================================
    def target_user(self, query):
        """閲覧・出力の対象ユーザー。?user_id= の指定は管理者のみ可（未指定・本人なら自分）"""
        user = self.require_user()
        user_id = (query.get("user_id") or [""])[0]
        if not user_id or user_id == user["user_id"]:
            return user
        if not user["is_admin"]:
            raise HttpError(403, "他のユーザーの記録は管理者のみ閲覧できます")
        with connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            raise HttpError(404, "ユーザーが見つかりません")
        return user_to_dict(row)

    def parse_range(self, query):
        get = lambda k: (query.get(k) or [None])[0]
        d1, d2 = parse_date(get("from"), "開始日"), parse_date(get("to"), "終了日")
        if d1 > d2:
            raise ValidationError("開始日は終了日以前にしてください")
        if (d2 - d1).days >= MAX_RANGE_DAYS:
            raise ValidationError(f"期間は{MAX_RANGE_DAYS}日以内にしてください")
        return d1, d2

    def list_activities(self, query):
        """?from=YYYY-MM-DD&to=YYYY-MM-DD（両端含む）の期間にかかる行動記録。
        期間内に開始したもの＋期間より前に開始して期間内まで続いたもの＋前日18:00以降に就寝した睡眠
        （睡眠は就寝 18:00 以降なら翌日の行動として表示する）。管理者は &user_id= で他のユーザーを指定できる"""
        user = self.target_user(query)
        d1, d2 = self.parse_range(query)
        range_start = f"{d1.isoformat()} 00:00"
        sleep_start = f"{(d1 - datetime.timedelta(days=1)).isoformat()} 18:00"
        range_end = f"{(d2 + datetime.timedelta(days=1)).isoformat()} 00:00"
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM activities"
                " WHERE user_id = ? AND start_time < ?"
                "   AND (start_time >= ? OR end_time > ? OR (category = 'sleep' AND start_time >= ?))"
                " ORDER BY start_time, id",
                (user["user_id"], range_end, range_start, range_start, sleep_start),
            ).fetchall()
        self.send_json(200, [row_to_dict(r) for r in rows])

    def report_pdf(self, query):
        """?from=YYYY-MM-DD&to=YYYY-MM-DD を含む月〜日の週ごとの帳票PDF（A4横・1週1ページ）。
        管理者は &user_id= で他のユーザーを指定できる"""
        me = self.require_user()
        user = self.target_user(query)
        d1, d2 = self.parse_range(query)
        with connect() as conn:
            def fetch(start, end):
                # start は週の前日 18:00（その時刻以降に就寝した睡眠は週の初日の記録）。表示する日は帳票側で判定
                rows = conn.execute(
                    "SELECT * FROM activities WHERE user_id = ? AND start_time >= ? AND start_time < ?"
                    " ORDER BY start_time, id",
                    (user["user_id"], start, end),
                ).fetchall()
                return [row_to_dict(r) for r in rows]

            def fetch_comments(date_from, date_to):
                rows = conn.execute(
                    "SELECT date, comment FROM daily_notes WHERE user_id = ? AND date BETWEEN ? AND ?",
                    (user["user_id"], date_from, date_to),
                ).fetchall()
                return {r["date"]: r["comment"] for r in rows}

            body = pdf_report.build_report(d1, d2, fetch, fetch_comments, owner_name=user["name"])
        # ファイル名に生成日時を付けて毎回別名にする（Android 等では PDF がダウンロード扱いになり、
        # 同名だと過去にダウンロードした古いファイルが開かれることがあるため）
        stamp = datetime.datetime.now(pdf_report.JST).strftime("%Y%m%d-%H%M%S")  # 帳票の出力日と同じ日本時間
        base = f"{d1.isoformat()}_{d2.isoformat()}_{stamp}.pdf"
        if user["user_id"] != me["user_id"]:   # 管理者が他のユーザーを出力するときはユーザIDも付ける
            base = f"{user['user_id']}_{base}"
        self.send_body(200, body, "application/pdf", [
            ("Content-Disposition",
             f"inline; filename=\"activity_log_{base}\"; filename*=UTF-8''{quote('行動記録_' + base)}"),
            ("Pragma", "no-cache"),
            ("Expires", "0"),
        ])

    def open_sleep(self):
        """起床が未入力の最新の睡眠（無ければ null）"""
        user = self.require_user()
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM activities WHERE user_id = ? AND category = 'sleep' AND end_time IS NULL"
                " ORDER BY start_time DESC, id DESC LIMIT 1", (user["user_id"],)
            ).fetchone()
        self.send_json(200, row_to_dict(row) if row else None)

    def history(self, query):
        """入力候補用：?category=meal|out|other の最近使ったメモ（重複なし・最大50件）"""
        user = self.require_user()
        category = (query.get("category") or [""])[0]
        if category not in NOTE_LABELS:
            raise ValidationError(f"category は {' / '.join(NOTE_LABELS)} のいずれかを指定してください")
        with connect() as conn:
            rows = conn.execute(
                "SELECT note FROM activities WHERE user_id = ? AND category = ? AND note <> ''"
                " GROUP BY note ORDER BY MAX(id) DESC LIMIT 50",
                (user["user_id"], category),
            ).fetchall()
        self.send_json(200, [r["note"] for r in rows])

    # ==================================================================
    # 1日の感想（ログイン中のユーザー自身のもの）
    # ==================================================================
    def list_daily_notes(self, query):
        """?from=YYYY-MM-DD&to=YYYY-MM-DD（両端含む）の感想。管理者は &user_id= で他のユーザーを指定できる"""
        user = self.target_user(query)
        d1, d2 = self.parse_range(query)
        with connect() as conn:
            rows = conn.execute(
                "SELECT date, comment FROM daily_notes WHERE user_id = ? AND date BETWEEN ? AND ? ORDER BY date",
                (user["user_id"], d1.isoformat(), d2.isoformat()),
            ).fetchall()
        self.send_json(200, [{"date": r["date"], "comment": r["comment"]} for r in rows])

    def save_daily_note(self, date):
        """感想を保存（空なら削除）"""
        user = self.require_user()
        parse_date(date)
        comment = self.read_object().get("comment")
        if not isinstance(comment, str):
            raise ValidationError("感想の形式が不正です")
        comment = comment.strip()
        if len(comment) > MAX_COMMENT:
            raise ValidationError(f"感想は{MAX_COMMENT}文字以内にしてください")
        with connect() as conn:
            if comment:
                conn.execute(
                    "INSERT INTO daily_notes (user_id, date, comment) VALUES (?, ?, ?)"
                    " ON CONFLICT (user_id, date) DO UPDATE SET comment = excluded.comment,"
                    " updated_at = datetime('now', 'localtime')",
                    (user["user_id"], date, comment),
                )
            else:
                conn.execute("DELETE FROM daily_notes WHERE user_id = ? AND date = ?", (user["user_id"], date))
        self.send_json(200, {"date": date, "comment": comment})

    def summarize_comment(self):
        """感想の本文（保存前の入力中のものでもよい）を要約させて返す。保存はしない

        DIARY_LLM_URL が設定されていればローカル LLM（API キー不要）、未設定なら本人の API キーで Gemini を使う。
        """
        user = self.require_user()
        key = "" if summarize.use_local() else self.gemini_key(user)
        if not key and not summarize.use_local():
            raise ValidationError("Gemini の API キーが設定されていません（「Gemini設定」から設定してください）")
        text = self.read_object().get("text")
        if not isinstance(text, str):
            raise ValidationError("要約する文章の形式が不正です")
        text = text.strip()
        if len(text) < summarize.MIN_LENGTH:
            raise ValidationError(f"要約できるのは{summarize.MIN_LENGTH}文字以上の文章です")
        if len(text) > MAX_COMMENT:
            raise ValidationError(f"要約できるのは{MAX_COMMENT}文字以内の文章です")
        if not summarize.limiter.allow(user["user_id"]):
            raise HttpError(429, "要約の回数が多すぎます。しばらく時間をおいてから再度お試しください")
        try:
            summary = summarize.summarize(text, key)
        except summarize.SummarizeError as e:
            raise HttpError(502, str(e))
        self.send_json(200, {"summary": summary[:MAX_COMMENT]})

    def create_activity(self):
        user = self.require_user()
        a = validate_activity(self.read_json())
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO activities (user_id, category, start_time, end_time, note) VALUES (?, ?, ?, ?, ?)",
                (user["user_id"], a["category"], a["start_time"], a["end_time"], a["note"]),
            )
            row = conn.execute("SELECT * FROM activities WHERE id = ?", (cur.lastrowid,)).fetchone()
        self.send_json(201, row_to_dict(row))

    def update_activity(self, activity_id):
        """指定された項目だけ更新する（既存の値とマージしてから全体を検証）"""
        user = self.require_user()
        data = self.read_object()
        if not data:
            raise ValidationError("更新する項目がありません")
        with connect() as conn:
            row = conn.execute("SELECT * FROM activities WHERE id = ? AND user_id = ?",
                               (activity_id, user["user_id"])).fetchone()
            if row is None:
                raise HttpError(404, "記録が見つかりません")
            merged = {**row_to_dict(row), **{k: data[k] for k in FIELDS[1:] if k in data}}
            a = validate_activity(merged)
            conn.execute(
                "UPDATE activities SET category = ?, start_time = ?, end_time = ?, note = ?,"
                " updated_at = datetime('now', 'localtime') WHERE id = ? AND user_id = ?",
                (a["category"], a["start_time"], a["end_time"], a["note"], activity_id, user["user_id"]),
            )
            row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        self.send_json(200, row_to_dict(row))

    def delete_activity(self, activity_id):
        user = self.require_user()
        with connect() as conn:
            cur = conn.execute("DELETE FROM activities WHERE id = ? AND user_id = ?", (activity_id, user["user_id"]))
        if cur.rowcount == 0:
            raise HttpError(404, "記録が見つかりません")
        self.send_json(200, {"deleted": activity_id})


def main():
    global DB_PATH
    parser = argparse.ArgumentParser(description="行動記録メモ サーバー")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", default=str(DB_PATH), metavar="PATH",
                        help=f"SQLiteのDBファイルのパス（省略時: {DB_PATH}）。無ければ作成する")
    args = parser.parse_args()

    if GEMINI_SECRET and len(GEMINI_SECRET) < secretbox.MIN_SECRET_LENGTH:
        raise SystemExit(f"GEMINI_KEY_SECRET は{secretbox.MIN_SECRET_LENGTH}文字以上にしてください")
    if summarize.use_local():
        print(f"AI 要約: ローカル LLM（{summarize.LOCAL_URL}、モデル {summarize.LOCAL_MODEL}）を使います", flush=True)
    elif not GEMINI_SECRET:
        print("注意: GEMINI_KEY_SECRET が未設定のため、Gemini API キーの登録と AI 要約（キー使用）は使えません", flush=True)

    DB_PATH = Path(args.db).expanduser().resolve()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db()

    # docker stop（SIGTERM）で速やかに終了する（コンテナのPID 1はシグナルの既定動作が無効なため）
    def on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"行動記録メモ: http://{args.host}:{args.port}/  (DB: {DB_PATH})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")


if __name__ == "__main__":
    main()
