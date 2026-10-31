"""시뮬레이터용 GLM 최소 클라이언트(로컬 중계 경유).

- 중계: POST http://127.0.0.1:18090/v1/chat/completions (OpenAI 호환)
- 토큰은 ~/.config/hackathon-relay/token 에서 읽기만 하고 로그·예외 메시지에 넣지 않는다.
- GLM 은 제3자 서버라 합성 카드와 서비스 응답만 보낸다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

RELAY_URL = os.environ.get("SIM_GLM_URL", "http://127.0.0.1:18090/v1/chat/completions")
MODEL = os.environ.get("SIM_GLM_MODEL", "zai-org/GLM-5.3-Flash")
TOKEN_PATH = Path(os.environ.get("SIM_GLM_TOKEN_FILE", "~/.config/hackathon-relay/token")).expanduser()
TIMEOUT = 120.0
RETRIES = 2


class GLMError(RuntimeError):
    pass


def _token() -> str:
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as e:  # 경로만 알리고 내용은 절대 출력하지 않는다
        raise GLMError(f"token file unreadable: {TOKEN_PATH} ({e.__class__.__name__})") from None


def chat(
    messages: list[dict],
    *,
    temperature: float = 0.7,
    max_tokens: int = 6000,
    seed: int | None = None,
    timeout: float = TIMEOUT,
    retries: int = RETRIES,
) -> str:
    """content 문자열만 돌려준다. 실패하면 retries 번 재시도 후 GLMError."""
    body: dict = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if seed is not None:
        body["seed"] = seed
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            RELAY_URL,
            data=data,
            headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.load(r)
            choice = resp["choices"][0]
            content = (choice.get("message") or {}).get("content") or ""
            if content.strip():
                return content
            last = f"empty content (finish_reason={choice.get('finish_reason')})"
            body["max_tokens"] = min(int(body["max_tokens"]) * 2, 32000)
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as e:
            last = e.__class__.__name__
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise GLMError(f"GLM call failed after {retries + 1} attempts: {last}")
