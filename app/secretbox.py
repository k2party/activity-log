"""DB に保存する秘密の値（Gemini API キー）の可逆暗号化（Python 標準ライブラリのみ）

標準ライブラリには AES 等のブロック暗号が無いため、HMAC-SHA256 を擬似乱数関数として使う
認証付き暗号で実装する。
  - 暗号化: HMAC-SHA256(暗号用鍵, nonce || ブロック番号) を鍵ストリームにした CTR 方式（XOR）
  - 改ざん検知: HMAC-SHA256(認証用鍵, 版 || 関連データ || nonce || 暗号文)（Encrypt-then-MAC）
  - 暗号用鍵・認証用鍵は、サーバーの秘密鍵（環境変数 GEMINI_KEY_SECRET）から HKDF と同じ手順で導出する
関連データ（aad）にはユーザIDを入れ、暗号文を別のユーザーの行にコピーしても復号できないようにする。

保存形式: "v1:" + base64url(nonce 16バイト || 暗号文 || タグ 32バイト)
"""
import base64
import hashlib
import hmac
import os

PREFIX = "v1:"
NONCE_SIZE = 16
TAG_SIZE = 32
MIN_SECRET_LENGTH = 32   # 秘密鍵（環境変数の文字列）の最小文字数


class DecryptError(Exception):
    """復号できない（秘密鍵が違う・改ざん・形式不正）"""


def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def _keys(secret):
    """秘密鍵から暗号用鍵と認証用鍵を導出する（HKDF-SHA256 の Extract → Expand）"""
    prk = _hmac(b"activity-log/secretbox", secret.encode("utf-8"))
    return _hmac(prk, b"enc\x01"), _hmac(prk, b"mac\x01")


def _keystream(key, nonce, length):
    blocks = (length + 31) // 32
    return b"".join(_hmac(key, nonce + i.to_bytes(4, "big")) for i in range(blocks))[:length]


def _tag(key, aad, nonce, ciphertext):
    a = aad.encode("utf-8")
    return _hmac(key, PREFIX.encode() + len(a).to_bytes(4, "big") + a + nonce + ciphertext)


def encrypt(plaintext, secret, aad=""):
    """文字列を暗号化して保存用の文字列を返す"""
    enc_key, mac_key = _keys(secret)
    nonce = os.urandom(NONCE_SIZE)
    data = plaintext.encode("utf-8")
    ciphertext = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    token = nonce + ciphertext + _tag(mac_key, aad, nonce, ciphertext)
    return PREFIX + base64.urlsafe_b64encode(token).decode("ascii")


def decrypt(token, secret, aad=""):
    """encrypt() の結果を復号して元の文字列を返す。失敗時は DecryptError"""
    if not token.startswith(PREFIX):
        raise DecryptError("形式が不正です")
    try:
        raw = base64.urlsafe_b64decode(token[len(PREFIX):].encode("ascii"))
    except ValueError:
        raise DecryptError("形式が不正です")
    if len(raw) < NONCE_SIZE + TAG_SIZE:
        raise DecryptError("形式が不正です")
    nonce, ciphertext, tag = raw[:NONCE_SIZE], raw[NONCE_SIZE:-TAG_SIZE], raw[-TAG_SIZE:]
    enc_key, mac_key = _keys(secret)
    if not hmac.compare_digest(tag, _tag(mac_key, aad, nonce, ciphertext)):
        raise DecryptError("秘密鍵が違うか、改ざんされています")
    data = bytes(a ^ b for a, b in zip(ciphertext, _keystream(enc_key, nonce, len(ciphertext))))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise DecryptError("形式が不正です")


def encrypted_length(plain_length):
    """平文の長さ（ASCII）から暗号化後の文字数を求める（DB の CHECK 制約用）"""
    raw = NONCE_SIZE + plain_length + TAG_SIZE
    return len(PREFIX) + (raw + 2) // 3 * 4
