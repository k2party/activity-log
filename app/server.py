"""行動記録メモ サーバー（Python標準ライブラリのみ / SQLite保存）

起動:  python3 app/server.py [--db DBファイルのパス] [--host 127.0.0.1] [--port 8000]
  --db 省略時はプロジェクト直下の data/activity_log.db（環境変数 DB_PATH があればそれ）
  例:  python3 app/server.py --db /var/lib/activity-log/activity_log.db
  Docker では --db /data/activity_log.db で起動する（Dockerfile の CMD）

環境変数でも設定可能（コマンドライン引数が優先）:
  HOST, PORT, DB_PATH
  TRUST_PROXY=1    … リバースプロキシ配下。X-Forwarded-For / X-Forwarded-Proto を使う
  COOKIE_SECURE=1  … ログインCookieに Secure 属性を常に付ける（HTTPS運用時）
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

BASE_DIR = Path(__file__).resolve().parent
PAGES = {name: BASE_DIR / f"{name}.html" for name in ("index", "login", "users")}
DB_PATH = Path(os.environ.get("DB_PATH") or BASE_DIR.parent / "data" / "activity_log.db")
TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE") == "1"
COOKIE_NAME = "alog_session"

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

# ---------------------------------------------------------------------------
# スキーマ（PRAGMA user_version で管理）
#   0〜1（旧 entries テーブル）: DROP & CREATE（データは引き継がない）
#   2〜3 → 4: ユーザー・ログイン機能を追加。既存の行動記録は admin の記録として引き継ぐ
#   4 → 5: 種類に「服薬(medicine)」を追加（activities を作り直してデータを移す）、1日の感想(daily_notes)を追加
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 5
CATEGORY_SQL = ", ".join(f"'{c}'" for c in CATEGORIES)

USERS_DDL = f"""
-- ユーザー（ユーザID・パスワード・氏名のみ管理）
CREATE TABLE users (
    user_id        TEXT PRIMARY KEY,      -- ログインID（'admin' が管理者）
    password_hash  TEXT NOT NULL,         -- PBKDF2-SHA256 ハッシュ
    name           TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND {MAX_NAME}),  -- 氏名
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

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
        }
        if path not in routes:
            return self.send_json(404, {"error": "見つかりません"})
        self.handle_errors(lambda: (self.check_csrf(), routes[path]()))

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/me/password":
            return self.handle_errors(lambda: (self.check_csrf(), self.change_own_password()))
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
        self.send_json(200, self.require_user())

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
        self.send_json(200, [user_to_dict(r) for r in rows])

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
    # 行動記録（ログイン中のユーザー自身の記録のみ）
    # ==================================================================
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
        （睡眠は就寝 18:00 以降なら翌日の行動として表示する）。"""
        user = self.require_user()
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
        """?from=YYYY-MM-DD&to=YYYY-MM-DD を含む月〜日の週ごとの帳票PDF（A4横・1週1ページ）"""
        user = self.require_user()
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
        filename = f"行動記録_{d1.isoformat()}_{d2.isoformat()}.pdf"
        self.send_body(200, body, "application/pdf", [(
            "Content-Disposition",
            f"inline; filename=\"activity_log_{d1.isoformat()}_{d2.isoformat()}.pdf\"; filename*=UTF-8''{quote(filename)}",
        )])

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
        """?from=YYYY-MM-DD&to=YYYY-MM-DD（両端含む）の感想"""
        user = self.require_user()
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
