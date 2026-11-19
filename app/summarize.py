"""1日の感想の要約（Google Gemini API またはローカル LLM（Ollama）。Python標準ライブラリのみ）

DIARY_LLM_URL が設定されていればローカル LLM に接続する（API キー不要。全ユーザーが使える）。
未設定なら Gemini を使い、API キーはユーザーごとに DB（users.gemini_api_key）に保存したものを使う。
環境変数:
  DIARY_LLM_URL    … ローカル LLM（Ollama）の接続先 URL（例: http://llm:11434）。設定すると Gemini は使わない
  LLM_MODEL        … ローカル LLM で使うモデル（既定: gemma3:1b）
  GEMINI_MODEL     … Gemini で使うモデル（既定: gemini-flash-latest。過負荷時は gemini-flash-lite-latest で再試行）
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-flash-latest"
FALLBACK_MODEL = "gemini-flash-lite-latest"   # 過負荷時の再試行に使う軽量モデル
OVERLOADED_CODES = (500, 503, 504)
RETRY_DELAY = 1                    # 秒（再試行前に待つ時間）
TIMEOUT = 20                       # 秒（1回あたり。再試行を含めてもプロキシの既定 60 秒に収まるようにする）
LOCAL_TIMEOUT = 50                 # 秒（ローカル LLM。CPU だと遅いため長めにするが、プロキシの既定 60 秒には収める）
DEFAULT_LOCAL_MODEL = "gemma3:1b"
MIN_LENGTH = 50                    # これより短い文は要約しない
RATE_LIMIT = (20, 600)             # 1ユーザーあたり 600 秒間に 20 回まで

# 要約の指示
SYSTEM_PROMPT = (
    "あなたは日記の要約を手伝うアシスタントです。"
    "ユーザーが書いた「1日の感想」を、日本語で簡潔に要約してください。\n"
    "- 元の文章の半分以下、かつ200文字以内にする\n"
    "- 書き手本人の視点（一人称）・常体のまま、事実と気持ちの要点を残す\n"
    "- 元の文章に無い内容を付け加えない\n"
    "- 見出し・箇条書き記号・Markdown・前置きは付けず、要約文だけを出力する"
)


def _local_url():
    """DIARY_LLM_URL（末尾の / は除く）。未設定は ''。http / https 以外は起動を中止する"""
    url = os.environ.get("DIARY_LLM_URL", "").strip().rstrip("/")
    if url:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise SystemExit(f"DIARY_LLM_URL は http:// または https:// で始まる URL にしてください: {url}")
    return url


LOCAL_URL = _local_url()
LOCAL_MODEL = os.environ.get("LLM_MODEL", "").strip() or DEFAULT_LOCAL_MODEL


def use_local():
    """ローカル LLM で要約するか（DIARY_LLM_URL が設定されているか）"""
    return bool(LOCAL_URL)


class SummarizeError(Exception):
    """要約に失敗した（メッセージは画面にそのまま表示する）"""


class RateLimiter:
    """ユーザーごとの呼び出し回数制限（メモリ上。再起動でリセット）"""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.calls = {}
        self.lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            recent = [t for t in self.calls.get(key, []) if now - t < self.window]
            if len(recent) >= self.limit:
                self.calls[key] = recent
                return False
            recent.append(now)
            self.calls[key] = recent
            return True


limiter = RateLimiter(*RATE_LIMIT)


class _Overloaded(Exception):
    """モデルが過負荷（500/503/504・タイムアウト）。別モデルで再試行する"""


def _request(model, body, key):
    """generateContent を1回呼んで応答 JSON を返す。過負荷なら _Overloaded、その他の失敗は SummarizeError"""
    req = urllib.request.Request(
        API_URL.format(model=model),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("error", {}).get("message", "")
        except Exception:
            pass
        print(f"Gemini API エラー {e.code}（{model}）: {detail}", flush=True)   # キー等の詳細はログのみに出す
        if e.code in (400, 401, 403) and ("API key" in detail or "API_KEY" in detail or e.code != 400):
            raise SummarizeError("Gemini の API キーが無効か、権限がありません。「Gemini設定」でキーを確認してください")
        if e.code == 429:
            raise SummarizeError("Gemini の利用上限に達したか、混み合っています。しばらくしてから再度お試しください")
        if e.code in OVERLOADED_CODES:
            raise _Overloaded()
        raise SummarizeError(f"要約サービスでエラーが発生しました（{e.code}）")
    except TimeoutError:
        print(f"Gemini API タイムアウト（{model}）", flush=True)
        raise _Overloaded()
    except (urllib.error.URLError, OSError) as e:
        if isinstance(getattr(e, "reason", None), TimeoutError):
            print(f"Gemini API タイムアウト（{model}）", flush=True)
            raise _Overloaded()
        print(f"Gemini API 接続エラー: {e}", flush=True)
        raise SummarizeError("要約サービスに接続できませんでした")
    except ValueError:
        raise SummarizeError("要約サービスの応答が不正です")


def _summarize_local(text):
    """ローカル LLM（Ollama の /api/chat）で要約する。失敗時は SummarizeError"""
    body = {
        "model": LOCAL_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 1024},
    }
    req = urllib.request.Request(
        LOCAL_URL + "/api/chat",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LOCAL_TIMEOUT) as res:
            data = json.load(res)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("error", "")
        except Exception:
            pass
        print(f"ローカル LLM エラー {e.code}（{LOCAL_MODEL}）: {detail}", flush=True)
        if e.code == 404:
            raise SummarizeError(f"ローカル LLM にモデル（{LOCAL_MODEL}）がありません。管理者に連絡してください")
        raise SummarizeError(f"ローカル LLM でエラーが発生しました（{e.code}）")
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
            print(f"ローカル LLM タイムアウト（{LOCAL_MODEL}）", flush=True)
            raise SummarizeError("ローカル LLM の要約が時間内に終わりませんでした。しばらくしてから再度お試しください")
        print(f"ローカル LLM 接続エラー（{LOCAL_URL}）: {e}", flush=True)
        raise SummarizeError("ローカル LLM に接続できませんでした")
    except ValueError:
        raise SummarizeError("ローカル LLM の応答が不正です")
    summary = ((data.get("message") or {}).get("content") or "").strip() if isinstance(data, dict) else ""
    if not summary:
        print(f"ローカル LLM 応答が空: {data.get('done_reason') if isinstance(data, dict) else '不明'}", flush=True)
        raise SummarizeError("要約を生成できませんでした")
    return summary


def summarize(text, key=""):
    """感想の本文を要約して返す。失敗時は SummarizeError

    DIARY_LLM_URL が設定されていればローカル LLM を使う（key は使わない）。
    Gemini では指定された API キーを使い、モデルが過負荷（503 など）やタイムアウトのときは
    少し待ってから軽量モデル（FALLBACK_MODEL）で1回だけ再試行する。
    """
    if use_local():
        return _summarize_local(text)
    model = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
    models = [model] + ([FALLBACK_MODEL] if model != FALLBACK_MODEL else [model])
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    for i, m in enumerate(models):
        if i:
            time.sleep(RETRY_DELAY)
        try:
            data = _request(m, body, key)
            break
        except _Overloaded:
            continue
    else:
        raise SummarizeError("Gemini が混み合っていて要約できませんでした。しばらくしてから再度お試しください")

    candidates = data.get("candidates") or []
    parts = (candidates[0].get("content") or {}).get("parts") or [] if candidates else []
    summary = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not summary:
        reason = (candidates[0].get("finishReason") if candidates else None) \
            or (data.get("promptFeedback") or {}).get("blockReason") or "不明"
        print(f"Gemini API 応答が空: {reason}", flush=True)
        raise SummarizeError("要約を生成できませんでした")
    return summary
