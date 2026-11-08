# tools/usage.py
"""DeepSeek usage の記録と集計。

chat.py から `import usage as U` で読み込まれる想定。
usage は dict（requests で受け取った JSON）を前提。
"""

import json
import sqlite3
import uuid
from collections import defaultdict
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

# レイテンシ列（既存DBへは _migrate で後付けする）
_LATENCY_COLUMNS = [
    ("search_ms", "REAL"),
    ("ttft_ms",   "REAL"),
    ("tpot_ms",   "REAL"),
    ("e2e_ms",    "REAL"),
    ("chunks",    "INTEGER"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """既存DBにレイテンシ列がなければ追加する（冪等）。"""
    have = {row[1] for row in conn.execute("PRAGMA table_info(api_usage)")}
    for name, typ in _LATENCY_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE api_usage ADD COLUMN {name} {typ}")


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
            metadata          TEXT,
            search_ms         REAL,
            ttft_ms           REAL,
            tpot_ms           REAL,
            e2e_ms            REAL,
            chunks            INTEGER
        )
    """)
    _migrate(conn)
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
def record(
    cost: dict,
    request_id: str | None = None,
    metadata: dict | None = None,
    latency: dict | None = None,
) -> None:
    """1呼び出し分を記録する。

    latency は任意。与える場合は次のキーを想定（ms 単位、chunks は個数）:
        {"search_ms": float, "ttft_ms": float, "tpot_ms": float,
         "e2e_ms": float, "chunks": int}
    未指定のキーは NULL として保存される。
    """
    lat = latency or {}
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO api_usage
               (id, created_at, model, prompt_tokens, completion_tokens,
                cache_hit_tokens, cache_miss_tokens, reasoning_tokens,
                cost_usd, is_peak, request_id, metadata,
                search_ms, ttft_ms, tpot_ms, e2e_ms, chunks)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                lat.get("search_ms"),
                lat.get("ttft_ms"),
                lat.get("tpot_ms"),
                lat.get("e2e_ms"),
                lat.get("chunks"),
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


def format_latency(latency: dict | None) -> str:
    """レイテンシの1行表示。未計測の項目は '—' で埋める。"""
    if not latency:
        return "ttft — / tpot — / e2e — / search — / chunks —"

    def ms(v):
        return f"{v:.0f}ms" if isinstance(v, (int, float)) else "—"

    return (
        f"ttft {ms(latency.get('ttft_ms'))}"
        f" / tpot {ms(latency.get('tpot_ms'))}"
        f" / e2e {ms(latency.get('e2e_ms'))}"
        f" / search {ms(latency.get('search_ms'))}"
        f" / chunks {latency.get('chunks', '—')}"
    )


# ── 集計（コスト）─────────────────────────────────────
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


# ── 集計（レイテンシ）─────────────────────────────────
def _pct(vals: list[float | None], p: float) -> float | None:
    """線形補間によるパーセンタイル。None は除外。空なら None。"""
    clean = sorted(v for v in vals if v is not None)
    if not clean:
        return None
    k = (len(clean) - 1) * p
    f = int(k)
    c = min(f + 1, len(clean) - 1)
    return clean[f] + (clean[c] - clean[f]) * (k - f)


def latency_summary(since_iso: str) -> dict:
    """指定時刻以降のレイテンシ P50/P95 を返す。未計測行（ttft_ms IS NULL）は無視。

    返り値のキー:
        n, ttft_ms_p50, ttft_ms_p95, tpot_ms_p50, tpot_ms_p95,
        e2e_ms_p50, e2e_ms_p95, search_ms_p50, search_ms_p95
    データがない場合は {"n": 0} のみ。
    """
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT ttft_ms, tpot_ms, e2e_ms, search_ms
            FROM api_usage
            WHERE created_at >= ? AND ttft_ms IS NOT NULL
        """, (since_iso,)).fetchall()
    finally:
        conn.close()

    out: dict = {"n": len(rows)}
    if not rows:
        return out

    for i, name in enumerate(("ttft_ms", "tpot_ms", "e2e_ms", "search_ms")):
        col = [r[i] for r in rows]
        out[f"{name}_p50"] = _pct(col, 0.50)
        out[f"{name}_p95"] = _pct(col, 0.95)
    return out


def latency_daily(days: int = 7) -> list[tuple]:
    """日別の TTFT P50/P95 と search 時間 P50。推移を見る用。

    返り値: [(day, ttft_p50, ttft_p95, search_p50), ...] 新しい日付が先頭。
    """
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT date(created_at) AS day, ttft_ms, search_ms
            FROM api_usage
            WHERE date(created_at) >= date('now', ?)
              AND ttft_ms IS NOT NULL
            ORDER BY day DESC
        """, (f"-{days} days",)).fetchall()
    finally:
        conn.close()

    bucket: dict = defaultdict(lambda: {"ttft": [], "search": []})
    for day, ttft, search in rows:
        bucket[day]["ttft"].append(ttft)
        bucket[day]["search"].append(search)

    return [
        (
            day,
            _pct(b["ttft"], 0.50),
            _pct(b["ttft"], 0.95),
            _pct(b["search"], 0.50),
        )
        for day, b in sorted(bucket.items(), reverse=True)
    ]


# ── 予算チェック（呼び出し前）────────────────────────
DEFAULT_DAILY_LIMIT_USD = 1.0
DEFAULT_HOURLY_LIMIT_USD = 0.20


def check_budget(
    daily_limit: float = DEFAULT_DAILY_LIMIT_USD,
    hourly_limit: float = DEFAULT_HOURLY_LIMIT_USD,
) -> tuple[bool, str]:
    """呼び出し前チェック。(ok, reason) を返す。"""
    t = summarize_today()
    if t["cost_usd"] >= daily_limit:
        return False, (
            f"daily limit reached: ${t['cost_usd']:.4f} >= ${daily_limit:.4f}"
        )
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT COALESCE(SUM(cost_usd), 0.0)
            FROM api_usage
            WHERE created_at >= datetime('now', '-1 hour')
        """).fetchone()
        hourly = row[0]
    finally:
        conn.close()
    if hourly >= hourly_limit:
        return False, (
            f"hourly limit reached: ${hourly:.4f} >= ${hourly_limit:.4f}"
        )
    return True, ""


def summarize_recent(hours: int = 1) -> dict:
    """直近 N 時間のコストと呼び出し回数。"""
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(cost_usd), 0.0)
            FROM api_usage
            WHERE created_at >= datetime('now', ?)
        """, (f"-{hours} hours",)).fetchone()
        return {"calls": row[0], "cost_usd": row[1]}
    finally:
        conn.close()


def summarize_baseline_per_hour(days: int = 7) -> float:
    """過去 N 日間の「1時間あたり平均コスト」。データ不足なら 0.0。"""
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT COALESCE(SUM(cost_usd), 0.0)
            FROM api_usage
            WHERE created_at >= datetime('now', ?)
        """, (f"-{days} days",)).fetchone()
        total = row[0]
        if total <= 0:
            return 0.0
        return total / (days * 24)
    finally:
        conn.close()
