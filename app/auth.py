"""認証まわり（Python標準ライブラリのみ）

- パスワード: PBKDF2-HMAC-SHA256（ソルト付き）でハッシュ化して保存
- セッション: ランダムなトークンを Cookie に入れ、DBには SHA-256 ハッシュのみ保存
- ログイン試行回数の制限（メモリ上）
"""
import hashlib
import hmac
import secrets
import threading
import time

PBKDF2_ITERATIONS = 600_000
SESSION_DAYS = 14            # 最終アクセスから14日でログアウト


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


# ユーザーが存在しない場合も同じだけ時間をかける（ユーザIDの存在を推測されないように）
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def verify_dummy(password):
    verify_password(password, _DUMMY_HASH)
    return False


def new_session_token():
    """(Cookieに入れるトークン, DBに保存するハッシュ)"""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token):
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


class LoginThrottle:
    """同じ接続元・ユーザIDでのログイン失敗が多い場合に一時的に拒否する"""

    def __init__(self, max_failures=5, window_sec=15 * 60):
        self.max_failures = max_failures
        self.window = window_sec
        self.failures = {}
        self.lock = threading.Lock()

    def _recent(self, key, now):
        items = [t for t in self.failures.get(key, []) if now - t < self.window]
        if items:
            self.failures[key] = items
        else:
            self.failures.pop(key, None)
        return items

    def blocked(self, key):
        with self.lock:
            return len(self._recent(key, time.time())) >= self.max_failures

    def fail(self, key):
        with self.lock:
            now = time.time()
            self.failures[key] = self._recent(key, now) + [now]

    def reset(self, key):
        with self.lock:
            self.failures.pop(key, None)
