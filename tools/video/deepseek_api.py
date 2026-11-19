#!/usr/bin/env python3
"""DeepSeek API の呼び出し（再試行・予算チェック・使用量の記録つき）。

パイプラインで DeepSeek を呼ぶ箇所（extract.py / refine.py / tts.py）は、すべてここを通す。
- 呼ぶ前に tools/usage.py の check_budget() で日次・時間あたりの予算を確認する
- 混雑（429 / 5xx）とタイムアウトは、待ち時間を伸ばしながら再試行する
- 400 などリクエスト自体が悪いエラーは再試行しない
- 呼んだあとは ~/.deepseek-ac/usage.db に記録する（チャットはトークン、TTS は文字数から費用を出す）
"""
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools import usage as U  # noqa: E402

API_URL = "https://api.deepseek.com/chat/completions"
TTS_URL = "https://api.deepseek.com/v1/tools/tts"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class BudgetExceeded(RuntimeError):
    """予算の上限に達したので呼び出しを止めた。"""


class RetryableAPIError(RuntimeError):
    """混雑やサーバー側の一時的なエラー。時間をおけば成功する見込みがある。"""


class DeepSeekError(RuntimeError):
    """再試行しても成功しなかった、または再試行してはいけないエラー。"""


def ensure_budget() -> None:
    """予算の上限に達していたら BudgetExceeded を出す。"""
    ok, reason = U.check_budget()
    if not ok:
        raise BudgetExceeded(reason)


def chat(
    messages: list[dict],
    *,
    model: str,
    api_key: str,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.2,
    timeout: float = 120,
    max_retries: int = 3,
    metadata: dict | None = None,
) -> tuple[dict, dict | None]:
    """Chat Completions を1回呼ぶ。戻り値は (レスポンスJSON, コスト情報)。"""
    ensure_budget()

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
    if response_format:
        body["response_format"] = response_format

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        wait = None
        try:
            r = requests.post(
                API_URL,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                json=body,
                timeout=timeout,
            )
            if r.status_code in RETRYABLE_STATUS:
                last_err = DeepSeekError(f"HTTP {r.status_code}: {r.text[:200]}")
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
            elif r.status_code >= 400:
                # リクエストの中身が悪い。何度送っても同じなので再試行しない
                raise DeepSeekError(f"HTTP {r.status_code}: {r.text[:500]}")
            else:
                data = r.json()
                cost = _record_usage(model, data, metadata)
                return data, cost
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e

        if attempt < max_retries:
            wait = wait if wait is not None else min(30, 2 ** (attempt + 1)) + random.uniform(0, 1)
            print(f"  [retry] DeepSeek が混雑しています（{last_err}）。{wait:.0f}秒後に再試行 "
                  f"({attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(wait)

    raise DeepSeekError(f"{max_retries + 1} 回試しても失敗しました: {last_err}")


def _record_usage(model: str, data: dict, metadata: dict | None) -> dict | None:
    """使用量を記録する。記録に失敗しても本処理は止めない。"""
    usage = data.get("usage")
    if not usage:
        return None
    try:
        cost = U.compute_cost(model, usage)
        U.record(cost, request_id=data.get("id"), metadata=metadata)
        return cost
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 使用量の記録に失敗しました: {e}", file=sys.stderr)
        return None


def tts(
    text: str,
    *,
    api_key: str,
    voice_id: str,
    price_per_10k_chars: float,
    timeout: float = 180,
    metadata: dict | None = None,
) -> bytes:
    """TTS を1回呼んで WAV の bytes を返す。再試行は呼び出し側（音声の長さの確認とまとめて）で行う。

    混雑など再試行してよいエラーは RetryableAPIError、それ以外は DeepSeekError を出す。
    TTS はトークン数を返さないので、文字数 × 単価で費用を出して記録する。
    """
    ensure_budget()
    r = requests.post(
        TTS_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"text": text, "voice_id": voice_id, "format": "wav"},
        timeout=timeout,
    )
    if r.status_code in RETRYABLE_STATUS:
        raise RetryableAPIError(f"HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code != 200:
        raise DeepSeekError(f"HTTP {r.status_code}: {r.text[:300]}")
    record_by_units("tts", len(text), "chars", price_per_10k_chars / 10_000,
                    request_id=r.headers.get("x-request-id"), metadata=metadata)
    return r.content


def record_by_units(model: str, units: int, unit: str, unit_price_usd: float,
                    request_id: str | None = None, metadata: dict | None = None) -> dict | None:
    """トークン以外（文字数など）で課金される API の使用量を、チャットと同じ表に記録する。
    単位の数は prompt_tokens の列に入れ、metadata に単位を残す。"""
    now = datetime.now(timezone.utc)
    cost = {
        "model": model, "prompt": units, "out": 0, "hit": 0, "miss": units, "reasoning": 0,
        "cost_usd": units * unit_price_usd, "peak": U.is_peak(now), "called_at": now,
    }
    try:
        U.record(cost, request_id=request_id, metadata={**(metadata or {}), "unit": unit, "units": units})
        return cost
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 使用量の記録に失敗しました: {e}", file=sys.stderr)
        return None
