#!/usr/bin/env python3
"""DeepSeek API の呼び出し（再試行・予算チェック・使用量の記録つき）。

パイプラインで DeepSeek を呼ぶ箇所（extract.py / refine.py / tts.py）は、すべてここを通す。
- 呼ぶ前に tools/usage.py の check_budget() で日次・時間あたりの予算を確認する
- 混雑（429 / 5xx）とタイムアウトは、待ち時間を伸ばしながら再試行する
- 400 などリクエスト自体が悪いエラーは再試行しない
- 呼んだあとは ~/.deepseek-ac/usage.db に記録する（チャットはトークン、TTS は文字数から費用を出す）
"""
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

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


# ------------------------------------------
# 宛先（プロバイダ）とフォールバック
# ------------------------------------------
# config.yaml の providers を上から順に試す。先頭が本来の宛先（DeepSeek 公式）。
# 設定がなければ DeepSeek 公式だけ（これまでと同じ動き）。
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
DEFAULT_PROVIDERS = [{"name": "deepseek", "url": API_URL, "key_env": "DEEPSEEK_API_KEY"}]
_cooldown_until: dict[str, float] = {}       # 失敗した宛先を、しばらく後回しにする
_no_json_mode: set[str] = set()              # この実行中に JSON モード非対応とわかった宛先
_STD_MESSAGE_KEYS = {"role", "content", "name", "tool_calls", "tool_call_id"}


def load_providers() -> tuple[list[dict], str, float]:
    """(試す順の宛先, 本来の宛先の名前, 休ませる秒数)"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}
    fb = cfg.get("fallback") or {}
    providers = fb.get("providers") or DEFAULT_PROVIDERS
    primary_name = providers[0]["name"]
    order = os.environ.get("VIDEO_LLM_PROVIDER")  # 試験用: 指定した宛先から始める
    if order:
        providers = sorted(providers, key=lambda p: p["name"] != order)
    return providers, primary_name, float(fb.get("cooldown_sec", 120))


class Kind:
    """エラーの分類（記事「フォールバックの実装」と同じ4種類）。"""
    TRANSIENT = "一時的"   # 混雑・タイムアウト: 同じ宛先で再試行し、だめなら次へ
    WALL = "壁"           # 認証エラー・長い待ちの429: 待っても無駄なので、すぐ次へ
    INVALID = "不正"      # リクエストが悪い: どこに送っても同じなので止める
    UNKNOWN = "不明"


def classify(status: int | None, retry_after: str | None) -> str:
    if status is None:
        return Kind.TRANSIENT                  # タイムアウト・接続エラー
    if status in (401, 403):
        return Kind.WALL
    if status == 429:
        try:
            return Kind.WALL if retry_after is not None and float(retry_after) >= 60 else Kind.TRANSIENT
        except ValueError:
            return Kind.TRANSIENT
    if status in (408, 409, 500, 502, 503, 504):
        return Kind.TRANSIENT
    if status in (400, 404, 413, 422):
        return Kind.INVALID
    return Kind.UNKNOWN


def _sanitize(messages: list[dict]) -> list[dict]:
    """DeepSeek 公式以外に送るときは、標準のキーだけにする（reasoning_content など独自のキーを外す）。"""
    return [{k: v for k, v in m.items() if k in _STD_MESSAGE_KEYS} for m in messages]


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
    """Chat Completions を呼ぶ。戻り値は (レスポンスJSON, コスト情報)。

    宛先は config.yaml の fallback.providers を上から順に試す。
    レスポンスJSONには、実際に応答した宛先を "_provider"、本来の宛先以外なら "_degraded": True で残す。
    """
    providers, primary_name, cooldown = load_providers()
    now = time.monotonic()
    # 休ませている宛先は後ろに回す（全部休んでいれば、そのまま順番どおり）
    ready = [p for p in providers if _cooldown_until.get(p["name"], 0) <= now]
    resting = [p for p in providers if p not in ready]
    errors: list[str] = []

    for idx, prov in enumerate(ready + resting):
        name = prov["name"]
        primary = name == primary_name
        # 呼び出し側から渡されたキーとモデルは、本来の宛先（DeepSeek 公式）のもの
        key = api_key if primary else os.environ.get(prov.get("key_env", ""), "")
        if not key:
            errors.append(f"{name}: トークン（{prov.get('key_env')}）が設定されていないので飛ばしました")
            continue
        if tools and prov.get("supports", {}).get("tools") is False:
            errors.append(f"{name}: ツール呼び出しに対応していないので飛ばしました")
            continue

        body = {
            "model": model if primary else prov["model"],
            "messages": messages if name == "deepseek" else _sanitize(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
        json_off = prov.get("supports", {}).get("json_mode") is False or name in _no_json_mode
        if response_format and not json_off:
            body["response_format"] = response_format

        try:
            data = _post_with_retry(prov, key, body, timeout, max_retries)
        except _InvalidRequest as e:
            # 本来の宛先以外で JSON モードが原因の 400 なら、外して1回だけ送り直す
            if "response_format" in body and not primary:
                print(f"  [fallback] {name} は JSON モードに対応していないようです。外して送り直します", file=sys.stderr)
                _no_json_mode.add(name)
                body.pop("response_format")
                try:
                    data = _post_with_retry(prov, key, body, timeout, max_retries)
                except _InvalidRequest as e2:
                    raise DeepSeekError(f"{name}: {e2}") from e2
                except _GiveUp as e2:
                    errors.append(f"{name}: {e2}")
                    _cooldown_until[name] = time.monotonic() + cooldown
                    continue
            else:
                raise DeepSeekError(f"{name}: {e}") from e
        except _GiveUp as e:
            errors.append(f"{name}: {e}")
            _cooldown_until[name] = time.monotonic() + cooldown
            if idx + 1 < len(providers):
                print(f"  [fallback] {name} をあきらめて次の宛先に切り替えます（{e}）", file=sys.stderr)
            continue

        data["_provider"] = name
        data["_degraded"] = not primary
        if not primary:
            print(f"  [fallback] {name}（{body['model']}）が応答しました", file=sys.stderr)
        cost = _record_usage(prov, body["model"], data, {**(metadata or {}), "provider": name})
        return data, cost

    raise DeepSeekError("すべての宛先で失敗しました: " + " / ".join(errors))


class _InvalidRequest(RuntimeError):
    """400 など。リクエストを直さない限り、何度送っても同じ。"""


class _GiveUp(RuntimeError):
    """この宛先では成功しそうにない（再試行の上限・壁・不明）。次の宛先に進む。"""


def _post_with_retry(prov: dict, key: str, body: dict, timeout: float, max_retries: int) -> dict:
    name = prov["name"]
    last = ""
    for attempt in range(max_retries + 1):
        ensure_budget()
        status, retry_after = None, None
        try:
            r = requests.post(prov["url"], headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                              json=body, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            status, retry_after = r.status_code, r.headers.get("Retry-After")
            last = f"HTTP {status}: {r.text[:300]}"
        except (requests.Timeout, requests.ConnectionError) as e:
            last = f"{type(e).__name__}: {e}"

        kind = classify(status, retry_after)
        if kind == Kind.INVALID:
            raise _InvalidRequest(last)
        if kind in (Kind.WALL, Kind.UNKNOWN):
            raise _GiveUp(f"{kind}: {last}")
        if attempt < max_retries:
            wait = int(retry_after) if retry_after and retry_after.isdigit() else min(30, 2 ** (attempt + 1)) + random.uniform(0, 1)
            print(f"  [retry] {name} が混雑しています（{last[:80]}）。{wait:.0f}秒後に再試行 ({attempt + 1}/{max_retries})",
                  file=sys.stderr)
            time.sleep(wait)
    raise _GiveUp(f"{max_retries + 1} 回試しても失敗: {last}")


def _record_usage(prov: dict, model: str, data: dict, metadata: dict | None) -> dict | None:
    """使用量を記録する。記録に失敗しても本処理は止めない。
    DeepSeek 公式は usage.py の料金表、ほかの宛先は config の料金（円建て）で費用を出す。"""
    usage = data.get("usage")
    if not usage:
        return None
    try:
        if prov["name"] == "deepseek":
            cost = U.compute_cost(model, usage)
        else:
            cost = _cost_from_config(prov, model, usage)
        U.record(cost, request_id=data.get("id"), metadata=metadata)
        return cost
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 使用量の記録に失敗しました: {e}", file=sys.stderr)
        return None


def _cost_from_config(prov: dict, model: str, usage: dict) -> dict:
    now = datetime.now(timezone.utc)
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    out = int(usage.get("completion_tokens", 0) or 0)
    price = prov.get("price") or {}
    if prov.get("free_plan"):
        usd = 0.0  # 無償プランは、枠を超えると 429 で止まり課金されない
    else:
        jpy = prompt / 10_000 * price.get("input_jpy_per_10k", 0) + out / 10_000 * price.get("output_jpy_per_10k", 0)
        usd = jpy / float(price.get("jpy_per_usd", 150))
    return {"model": f"{prov['name']}:{model}", "prompt": prompt, "out": out, "hit": 0, "miss": prompt,
            "reasoning": 0, "cost_usd": usd, "peak": U.is_peak(now), "called_at": now}


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
