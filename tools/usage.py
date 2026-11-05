# tools/usage.py
"""DeepSeek usage の記録と集計。

chat.py から `import usage as U` で読み込まれる想定。
usage は dict（requests で受け取った JSON）を前提。
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ── レート定義（USD / 1M tokens）──────────────────────
@dataclass(frozen=True)
class Rate:
    cache_hit: float
    cache_miss: float
    output: float

# ピーク料金。オフピークは半分。
PEAK_RATES = {
    "deepseek-flash":  Rate(cache_hit=0.006, cache_miss=0.30, output=1.20),
    "deepseek-v4-pro": Rate(cache_hit=0.044, cache_miss=1.32, output=3.96),
}
DEFAULT_MODEL = "deepseek-flash"

# ── DB ────────────────────────────────────────────────
DB_PATH = Path.home() / ".deepseek-ac" / "usage.db"

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage (
            id                TEXT PRIMARY KEY,
            created_at        TEXT NOT NULL,
            model             TEXT NOT NULL,
            prompt_tokens     INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            cache_hit_tokens  INTEGER NOT NULL,
            cache_miss_tokens INTEGER NOT NULL,
            reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
            cost_usd          REAL NOT NULL,
            is_peak           INTEGER NOT NULL,
            request_id        TEXT,
            metadata          TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_created ON api_usage(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_model   ON api_usage(model)")
    conn.commit()
    return conn

# ── ピーク判定 ────────────────────────────────────────
def is_peak(ts: datetime | None = None) -> bool:
    """UTC 月〜金 01:00-04:00 / 06:00-10:00 がピーク。土日は終日オフピーク。"""
    if ts is None:
        ts = datetime.now(timezone.utc)
    if ts.weekday() >= 5:
        return False
    h = ts.hour
    return (1 <= h < 4) or (6 <= h < 10)

# ── コスト計算 ────────────────────────────────────────
def compute_cost(model: str, usage: dict, called_at: datetime | None = None) -> dict:
    ts = called_at or datetime.now(timezone.utc)
    rate = PEAK_RATES.get(model, PEAK_RATES[DEFAULT_MODEL])
    if not is_peak(ts):
        rate = Rate(rate.cache_hit / 2, rate.cache_miss / 2, rate.output / 2)

    hit    = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    miss   = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    out    = int(usage.get("completion_tokens", 0) or 0)

    details = usage.get("completion_tokens_details") or {}
    reasoning = int(details.get("reasoning_tokens", 0) or 0) if isinstance(details, dict) else 0

    # 内訳が返らない版へのフォールバック（保守的に全ミス扱い）
    if hit == 0 and miss == 0 and prompt > 0:
        miss = prompt
    elif hit + miss != prompt:
        raise ValueError(f"inconsistent usage: {hit}+{miss} != {prompt}")

    cost_hit  = hit  / 1_000_000 * rate.cache_hit
    cost_miss = miss / 1_000_000 * rate.cache_miss
    cost_out  = out  / 1_000_000 * rate.output

    return {
        "model": model,
        "hit": hit,
        "miss": miss,
        "out": out,
        "prompt": prompt,
        "reasoning": reasoning,
        "cost_usd": cost_hit + cost_miss + cost_out,
        "cost_hit": cost_hit,
        "cost_miss": cost_miss,
        "cost_out": cost_out,
        "peak": is_peak(ts),
        "called_at": ts,
    }

# ── 記録 ──────────────────────────────────────────────
def record(cost: dict, request_id: str | None = None, metadata: dict | None = None) -> None:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO api_usage
               (id, created_at, model, prompt_tokens, completion_tokens,
                cache_hit_tokens, cache_miss_tokens, reasoning_tokens,
                cost_usd, is_peak, request_id, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                cost["called_at"].isoformat(),
                cost["model"],
                cost["prompt"],
                cost["out"],
                cost["hit"],
                cost["miss"],
                cost["reasoning"],
                cost["cost_usd"],
                int(cost["peak"]),
                request_id,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()

# ── 1行表示 ───────────────────────────────────────────
def format_line(cost: dict) -> str:
    total_in = cost["hit"] + cost["miss"]
    rate = 100.0 * cost["hit"] / total_in if total_in else 0.0
    band = "PEAK" if cost["peak"] else "off"
    return (
        f"hit {cost['hit']:>5} / miss {cost['miss']:>5} / out {cost['out']:>4}"
        f"  hit率 {rate:>5.1f}%  ${cost['cost_usd']:.6f} ({band})"
    )

# ── 集計 ──────────────────────────────────────────────
def summarize_session(since_iso: str) -> dict:
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT
                COUNT(*),
                COALESCE(SUM(cache_hit_tokens), 0),
                COALESCE(SUM(cache_miss_tokens), 0),
                COALESCE(SUM(completion_tokens), 0),
                COALESCE(SUM(cost_usd), 0.0)
            FROM api_usage
            WHERE created_at >= ?
        """, (since_iso,)).fetchone()
        calls, hit, miss, out, cost = row
        total_in = hit + miss
        hit_rate = 100.0 * hit / total_in if total_in else 0.0
        return {"calls": calls, "hit": hit, "miss": miss, "out": out,
                "hit_rate": hit_rate, "cost_usd": cost}
    finally:
        conn.close()

def summarize_today() -> dict:
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT
                COUNT(*),
                COALESCE(SUM(cache_hit_tokens), 0),
                COALESCE(SUM(cache_miss_tokens), 0),
                COALESCE(SUM(cost_usd), 0.0)
            FROM api_usage
            WHERE date(created_at) = date('now')
        """).fetchone()
        calls, hit, miss, cost = row
        total_in = hit + miss
        hit_rate = 100.0 * hit / total_in if total_in else 0.0
        return {"calls": calls, "hit": hit, "miss": miss,
                "hit_rate": hit_rate, "cost_usd": cost}
    finally:
        conn.close()

def daily(days: int = 7) -> list[tuple]:
    conn = _connect()
    try:
        return conn.execute("""
            SELECT
                date(created_at) AS day,
                COUNT(*),
                ROUND(SUM(cost_usd), 6),
                ROUND(100.0 * SUM(cache_hit_tokens)
                      / NULLIF(SUM(cache_hit_tokens + cache_miss_tokens), 0), 1)
            FROM api_usage
            GROUP BY day
            ORDER BY day DESC
            LIMIT ?
        """, (days,)).fetchall()
    finally:
        conn.close()

def by_model(days: int = 7) -> list[tuple]:
    conn = _connect()
    try:
        return conn.execute("""
            SELECT
                model,
                COUNT(*),
                ROUND(SUM(cost_usd), 6),
                ROUND(100.0 * SUM(cache_hit_tokens)
                      / NULLIF(SUM(cache_hit_tokens + cache_miss_tokens), 0), 1)
            FROM api_usage
            WHERE date(created_at) >= date('now', ?)
            GROUP BY model
            ORDER BY 3 DESC
        """, (f"-{days} days",)).fetchall()
    finally:
        conn.close()

def peak_split() -> list[tuple]:
    conn = _connect()
    try:
        return conn.execute("""
            SELECT
                CASE is_peak WHEN 1 THEN 'peak' ELSE 'off-peak' END,
                COUNT(*),
                ROUND(SUM(cost_usd), 6)
            FROM api_usage
            GROUP BY 1
        """).fetchall()
    finally:
        conn.close()
