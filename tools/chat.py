#!/usr/bin/env python3
import os
import sys
import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
import requests

# 自作モジュール（tools/ 直下にあるので script の dir 経由で import 可能）
sys.path.insert(0, str(Path(__file__).parent))
import usage as U

API_URL = "https://api.deepseek.com/chat/completions"
SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
MODEL = "deepseek-flash"  # 10/17以降の最新モデル

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILE = Path(__file__).parent / "system_prompt.txt"

EXCLUDE_DIRS = {".git", ".astro", "node_modules", "dist", ".next", "build", ".cache"}

EXCLUDE_FILES = {
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock",
    ".env", ".env.local", ".env.production",
}

ALLOWED_EXTS = {
    ".md", ".mdx", ".astro", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".json", ".css", ".scss", ".html", ".txt", ".yml", ".yaml", ".toml",
}

MAX_FILE_SIZE = 100 * 1024

AUTO_LOAD_FILES = [
    "templates/article_template.md",
    "src/content.config.ts",
]

WRITABLE_DIRS = ["content/articles"]
WRITABLE_EXTS = {".md"}

# 検索の設定（記事執筆用途では常時ONを前提に、タイムアウトとキャッシュで運用を安定させる）
SEARCH_TIMEOUT_S = 8.0
SEARCH_CACHE_MAX = 32

# セッション内検索キャッシュ（同一クエリの連投を吸収）
_search_cache: dict[tuple, list] = {}


def load_system_prompt() -> str:
    if PROMPT_FILE.exists():
        base = PROMPT_FILE.read_text(encoding="utf-8").strip()
    else:
        base = "You are a helpful assistant."

    today = date.today().isoformat()
    return (
        base
        + f"\n\n# 今日の日付\n今日は {today} です。"
        + "記事の publishedAt には必ずこの日付を使ってください。"
        + "日付を推測したり、過去や未来の日付を書いてはいけません。\n"
    )


def normalize_command(s: str) -> str:
    """コマンド内の全角スラッシュを半角に。各種空白は正規表現側で吸収。"""
    return s.replace("／", "/")


def safe_resolve(rel_path: str):
    try:
        p = (PROJECT_ROOT / rel_path).resolve()
    except (OSError, ValueError):
        return None
    try:
        p.relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    return p


def is_excluded(p: Path) -> bool:
    rel = p.relative_to(PROJECT_ROOT)
    parts = rel.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    if p.name in EXCLUDE_FILES:
        return True
    if p.name.startswith(".env"):
        return True
    if p.suffix.lower() not in ALLOWED_EXTS:
        return True
    return False


def list_files():
    results = []
    for p in sorted(PROJECT_ROOT.rglob("*")):
        if not p.is_file():
            continue
        if is_excluded(p):
            continue
        results.append(str(p.relative_to(PROJECT_ROOT)))
    return results


def read_file(rel_path: str):
    p = safe_resolve(rel_path)
    if p is None:
        return False, "プロジェクト外のパスは読めません"
    if not p.exists():
        return False, "ファイルが存在しません"
    if not p.is_file():
        return False, "ファイルではありません"
    if is_excluded(p):
        return False, "除外対象のファイルです"
    size = p.stat().st_size
    if size > MAX_FILE_SIZE:
        return False, f"ファイルが大きすぎます（{size} bytes > {MAX_FILE_SIZE}）"
    try:
        return True, p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False, "テキストとして読めません（バイナリの可能性）"


def read_multiline_input() -> str:
    print("[paste] 複数行モード: 終了は 'EOF' のみの行（Ctrl+C でキャンセル）")
    print("... ", end="", flush=True)
    lines = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print("\n[paste] キャンセルしました\n")
            return ""
        if line.strip() == "EOF":
            break
        lines.append(line)
        print("... ", end="", flush=True)
    text = "\n".join(lines)
    print(f"[paste] {len(lines)} 行 / {len(text)} 文字を受け取りました\n")
    return text


def extract_markdown_block(text: str):
    """テキストから最初の ```markdown ... ``` ブロックを抽出。
    入れ子のコードブロックに対応するため、貪欲マッチ（.*）で最後の ``` まで取る。"""
    m = re.search(r"```markdown\s*\n(.*)\n```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"```\s*\n(.*)\n```", text, re.DOTALL)
    if m:
        return m.group(1)
    return None


def is_writable(rel_path: str):
    p = safe_resolve(rel_path)
    if p is None:
        return False, "プロジェクト外のパスは書けません"
    rel = str(p.relative_to(PROJECT_ROOT))
    if not any(rel.startswith(d + "/") or rel == d for d in WRITABLE_DIRS):
        return False, f"書き込みは {', '.join(WRITABLE_DIRS)} 配下のみです"
    if p.suffix.lower() not in WRITABLE_EXTS:
        return False, f"拡張子は {', '.join(WRITABLE_EXTS)} のみです"
    return True, str(p)


def _make_cache_key(query: str, count: int) -> tuple:
    """Search API の query は 1〜128 文字。キャッシュキーも同じ切り詰めで揃える。"""
    q = query.strip()
    if len(q) > 100:
        q = q[:100]
    return (q, count)


def search_web(query: str, api_key: str, count: int = 5):
    """DeepSeek Search API でウェブ検索し、結果のリストを返す。失敗時は空リスト。"""
    q = query.strip()
    if len(q) > 100:
        q = q[:100]
    if not q:
        return []
    try:
        r = requests.post(
            SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={"query": q, "count": count, "need_content": False},
            timeout=SEARCH_TIMEOUT_S,
        )
        r.raise_for_status()
        return r.json().get("results", [])
    except requests.exceptions.RequestException as e:
        print(f"[search error] {e}", file=sys.stderr)
        return []


def search_with_cache(query: str, api_key: str, count: int = 5):
    """セッション内キャッシュ経由で検索。(results, cached) を返す。"""
    key = _make_cache_key(query, count)
    if key in _search_cache:
        return _search_cache[key], True
    res = search_web(query, api_key, count)
    _search_cache[key] = res
    # 素朴な上限（LRUではなくFIFOで十分）
    if len(_search_cache) > SEARCH_CACHE_MAX:
        oldest = next(iter(_search_cache))
        del _search_cache[oldest]
    return res, False


def build_api_messages(messages, loaded_files):
    """prefix を安定させるため、[system] [参考資料] を先頭に固定し、
    会話履歴（user/assistant）はその後に置く。"""
    api_messages = [messages[0]]  # system プロンプト
    if loaded_files:
        ref_parts = ["# 参考資料（プロジェクト内のファイル）\n"]
        for path, content in loaded_files.items():
            ref_parts.append(f"## {path}\n```\n{content}\n```\n")
        api_messages.append(
            {"role": "system", "content": "\n".join(ref_parts)}
        )
    api_messages.extend(messages[1:])  # 会話履歴は後ろ
    return api_messages


def print_cost_summary(session_started: str, turn_count: int) -> None:
    s = U.summarize_session(session_started)
    t = U.summarize_today()
    elapsed = (
        datetime.now(timezone.utc)
        - datetime.fromisoformat(session_started)
    ).total_seconds()
    print("[cost] session:")
    print(f"  turns       {turn_count}")
    print(f"  hit / miss  {s['hit']} / {s['miss']}  → hit率 {s['hit_rate']:.1f}%")
    print(f"  out         {s['out']}")
    print(f"  cost        ${s['cost_usd']:.6f}")
    print(f"  elapsed     {elapsed/60:.1f} 分")
    print("[cost] today:")
    print(f"  calls       {t['calls']}")
    print(f"  hit率       {t['hit_rate']:.1f}%")
    print(f"  cost        ${t['cost_usd']:.6f}\n")


def print_latency_summary(session_started: str) -> None:
    s = U.latency_summary(session_started)
    if s.get("n", 0) == 0:
        print("[latency] まだ計測データがありません\n")
        return

    def fmt(v):
        return f"{v:.0f}ms" if isinstance(v, (int, float)) else "—"

    print(f"[latency] session (n={s['n']})")
    for key in ("ttft_ms", "tpot_ms", "e2e_ms", "search_ms"):
        p50 = s.get(f"{key}_p50")
        p95 = s.get(f"{key}_p95")
        print(f"  {key:<10} p50 {fmt(p50):>8}  p95 {fmt(p95):>8}")
    print()


def print_stats() -> None:
    print("[stats] daily (last 7 days):")
    rows = U.daily(7)
    if not rows:
        print("  （まだ記録がありません）")
    for day, calls, cost, hr in rows:
        print(f"  {day}  {calls:>4} calls  ${cost:>10.6f}  hit率 {hr or 0:>5.1f}%")

    print("[stats] latency daily (TTFT p50/p95, search p50):")
    lat_rows = U.latency_daily(7)
    if not lat_rows:
        print("  （まだレイテンシ記録がありません）")
    for day, ttft50, ttft95, search50 in lat_rows:
        def fmt(v):
            return f"{v:.0f}ms" if isinstance(v, (int, float)) else "—"
        print(
            f"  {day}  ttft p50 {fmt(ttft50):>8} / p95 {fmt(ttft95):>8}"
            f"  search p50 {fmt(search50):>8}"
        )

    print("[stats] by model:")
    for model, calls, cost, hr in U.by_model(7):
        print(f"  {model:<20} {calls:>4} calls  ${cost:>10.6f}  hit率 {hr or 0:>5.1f}%")
    print("[stats] peak split:")
    for band, calls, cost in U.peak_split():
        print(f"  {band:<10} {calls:>4} calls  ${cost:>10.6f}")
    print()


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("Error: DEEPSEEK_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    system_prompt = load_system_prompt()
    messages = [{"role": "system", "content": system_prompt}]
    loaded_files = {}
    last_reply = ""
    search_enabled = True

    session_started = datetime.now(timezone.utc).isoformat()
    turn_count = 0

    print("DeepSeek チャット開始（終了: Ctrl+C または 'exit'）")
    print(f"プロジェクト: {PROJECT_ROOT}")
    print(f"DB: {U.DB_PATH}")
    print("コマンド: /ls  /read <path>  /files  /drop <path>  /paste  /save <path>"
          "  /clear  /reload  /search  /cost  /latency  /stats")

    for rel in AUTO_LOAD_FILES:
        ok, content = read_file(rel)
        if ok:
            loaded_files[rel] = content
            print(f"[auto] {rel} を読み込みました（{len(content)} 文字）")
        else:
            print(f"[auto] {rel} の読み込みに失敗: {content}")

    print("-" * 50)

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n終了します")
            break

        user_input = normalize_command(user_input)

        if user_input.lower() in ("exit", "quit"):
            print("終了します")
            break
        if not user_input:
            continue

        if user_input == "/reload":
            system_prompt = load_system_prompt()
            messages = [{"role": "system", "content": system_prompt}]
            loaded_files.clear()
            print("[system] プロンプトを再読み込み、履歴とファイルをリセットしました\n")
            continue

        if user_input == "/clear":
            messages = [{"role": "system", "content": system_prompt}]
            loaded_files.clear()
            print("[system] 履歴とファイルをリセットしました\n")
            continue

        if user_input == "/search":
            search_enabled = not search_enabled
            print(f"[system] 検索を{'有効' if search_enabled else '無効'}にしました\n")
            continue

        if user_input == "/cost":
            print_cost_summary(session_started, turn_count)
            continue

        if user_input == "/latency":
            print_latency_summary(session_started)
            continue

        if user_input == "/stats":
            print_stats()
            continue

        if user_input == "/ls":
            files = list_files()
            print(f"[system] 読み込み可能ファイル {len(files)} 件:")
            for f in files:
                mark = " *" if f in loaded_files else "  "
                print(f"{mark} {f}")
            print()
            continue

        if user_input == "/files":
            if not loaded_files:
                print("[system] 現在読み込み中のファイルはありません\n")
            else:
                print(f"[system] 読み込み中 {len(loaded_files)} 件:")
                for f in loaded_files:
                    print(f"  - {f}")
                print()
            continue

        m = re.match(r"^/read\s+(.+)$", user_input)
        if m:
            rel = m.group(1).strip()
            ok, content = read_file(rel)
            if not ok:
                print(f"[system] 読み込み失敗: {content}\n")
                continue
            loaded_files[rel] = content
            print(f"[system] {rel} を読み込みました（{len(content)} 文字）\n")
            continue

        m = re.match(r"^/drop\s+(.+)$", user_input)
        if m:
            rel = m.group(1).strip()
            if rel in loaded_files:
                del loaded_files[rel]
                print(f"[system] {rel} を外しました\n")
            else:
                print(f"[system] {rel} は読み込まれていません\n")
            continue

        if user_input == "/paste":
            user_input = read_multiline_input()
            if not user_input.strip():
                continue

        m = re.match(r"^/save\s+(.+)$", user_input)
        if m:
            if not last_reply:
                print("[save] 直前にDeepSeekの返答がありません\n")
                continue
            rel = m.group(1).strip()
            ok, result = is_writable(rel)
            if not ok:
                print(f"[save] 保存できません: {result}\n")
                continue
            content = extract_markdown_block(last_reply)
            if content is None:
                print("[save] 返答に markdown コードブロックが見つかりません\n")
                continue
            target = Path(result)
            if target.exists():
                ans = input(f"[save] {rel} は既に存在します。上書き？ [y/N]: ").strip().lower()
                if ans != "y":
                    print("[save] キャンセルしました\n")
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            print(f"[save] {rel} に保存しました（{len(content)} 文字）\n")
            continue

        # ── prefix を安定させた組み立て ──
        api_messages = build_api_messages(messages, loaded_files)

        # --- ウェブ検索を実行し、結果をシステムメッセージとして注入 ---
        search_ms = None
        search_cached = False
        if search_enabled:
            ts = time.perf_counter()
            results, search_cached = search_with_cache(user_input, api_key)
            search_ms = (time.perf_counter() - ts) * 1000

            tag = "cache" if search_cached else "web"
            q_disp = user_input if len(user_input) <= 40 else user_input[:40] + "…"
            if results:
                print(f"[search:{tag}] '{q_disp}' → {len(results)} 件（{search_ms:.0f}ms）")
                ctx_parts = ["# ウェブ検索結果（以下の情報を根拠に記事を書いてください）\n"]
                for i, r in enumerate(results, 1):
                    title = r.get("title", "")
                    url = r.get("url", "")
                    summary = (r.get("summary") or r.get("snippet") or "").strip()
                    if len(summary) > 800:
                        summary = summary[:800] + "…"
                    ctx_parts.append(f"### {i}. {title}\nURL: {url}\n{summary}\n")
                api_messages.append({"role": "system", "content": "\n".join(ctx_parts)})
            else:
                print(f"[search:{tag}] '{q_disp}' → 結果なし（{search_ms:.0f}ms）")

        api_messages.append({"role": "user", "content": user_input})

        print("deepseek> ", end="", flush=True)

        reply_parts = []
        usage = None
        req_id = None

        # ── レイテンシ計測 ──
        t0 = time.perf_counter()
        ttft_ms = None
        first_at = None
        last_at = None
        chunk_count = 0

        try:
            with requests.post(
                API_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "model": MODEL,
                    "messages": api_messages,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
                stream=True,
                timeout=120,
            ) as r:
                r.raise_for_status()
                # chunk_size=1 でデフォルトバッファ（512）によるTTFTの歪みを避ける
                for line in r.iter_lines(chunk_size=1):
                    if not line:
                        continue
                    line = line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("id"):
                        req_id = chunk["id"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        now = time.perf_counter()
                        if ttft_ms is None:
                            first_at = now
                            ttft_ms = (now - t0) * 1000
                        last_at = now
                        chunk_count += 1
                        print(content, end="", flush=True)
                        reply_parts.append(content)
        except requests.exceptions.RequestException as e:
            print(f"\n[error] {e}", file=sys.stderr)
            continue

        e2e_ms = (time.perf_counter() - t0) * 1000

        # ── TPOTはトークン基準、チャンク間隔は別名で保持 ──
        output_tokens = (
            int(usage.get("completion_tokens", 0))
            if usage and usage.get("completion_tokens") is not None else 0
        )
        chunk_interval_ms = (
            (last_at - first_at) * 1000 / (chunk_count - 1)
            if chunk_count > 1 and last_at is not None and first_at is not None
            else None
        )
        tpot_ms = (
            (last_at - first_at) * 1000 / (output_tokens - 1)
            if output_tokens > 1 and last_at is not None and first_at is not None
            else None
        )

        print("\n")

        latency = {
            "search_ms": search_ms,
            "ttft_ms": ttft_ms,
            "e2e_ms": e2e_ms,
            "chunks": chunk_count,
            "chunk_interval_ms": chunk_interval_ms,
            "tpot_ms": tpot_ms,
        }

        if usage:
            try:
                cost = U.compute_cost(MODEL, usage)
                turn_count += 1
                U.record(
                    cost,
                    request_id=req_id,
                    metadata={
                        "purpose": "chat",
                        "turn": turn_count,
                        "search": search_enabled,
                        "search_cached": search_cached,
                        "files": list(loaded_files.keys()),
                    },
                    latency=latency,
                )
                print(f"  └─ {U.format_line(cost)}")
                print(f"  └─ {U.format_latency(latency)}")
                if chunk_interval_ms is not None:
                    print(f"  └─ chunk_interval {chunk_interval_ms:.0f}ms")
                print()
            except ValueError as e:
                print(f"  └─ [usage error] {e}\n")
        else:
            print("  └─ usage が返りませんでした（stream_options を確認）\n")
            print(f"  └─ {U.format_latency(latency)}\n")

        full_reply = "".join(reply_parts)
        last_reply = full_reply
        messages.append({"role": "user", "content": user_input})
        messages.append({"role": "assistant", "content": full_reply})


if __name__ == "__main__":
    main()
