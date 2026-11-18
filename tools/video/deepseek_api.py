#!/usr/bin/env python3
"""DeepSeek API の呼び出し（再試行・予算チェック・使用量の記録つき）。

- 呼ぶ前に tools/usage.py の check_budget() で日次・時間あたりの予算を確認する
- 混雑（429 / 5xx）とタイムアウトは、待ち時間を伸ばしながら再試行する
- 400 などリクエスト自体が悪いエラーは再試行しない
- 呼んだあとは compute_cost() / record() で ~/.deepseek-ac/usage.db に記録する
"""
import random
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools import usage as U  # noqa: E402

API_URL = "https://api.deepseek.com/chat/completions"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class BudgetExceeded(RuntimeError):
    """予算の上限に達したので呼び出しを止めた。"""


class DeepSeekError(RuntimeError):
    """再試行しても成功しなかった、または再試行してはいけないエラー。"""


def chat(
    messages: list[dict],
    *,
    model: str,
    api_key: str,
    tools: list[dict] | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.2,
    timeout: float = 120,
    max_retries: int = 3,
    metadata: dict | None = None,
) -> tuple[dict, dict | None]:
    """Chat Completions を1回呼ぶ。戻り値は (レスポンスJSON, コスト情報)。"""
    ok, reason = U.check_budget()
    if not ok:
        raise BudgetExceeded(reason)

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools

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
