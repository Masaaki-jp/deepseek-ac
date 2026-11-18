#!/usr/bin/env python3
"""記事のMarkdownから、スライド計画（deck_ir.json）を抽出する。"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

# プロジェクトルートを sys.path に追加（usage.py などを将来使うため）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.video.schema import DeckIR, SlideIR, detect_diagram_kind

API_URL = "https://api.deepseek.com/chat/completions"
PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.txt"
CONFIG_PATH = Path(__file__).parent / "config.yaml"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


DEFAULT_SUMMARY_HEADINGS = ["（冒頭）", "リード", "前提", "まとめ", "チェックリスト"]


def strip_front_matter(markdown: str) -> str:
    return re.sub(r"\A---\s*\n.*?\n---\s*\n", "", markdown, count=1, flags=re.DOTALL)


def split_sections(markdown: str, summary_headings: list[str] | None = None) -> list[dict]:
    """## と ### の見出しでセクションに分割する。

    - フロントマターと H1 は除く
    - コードブロックの中の # 行は見出しとして扱わない
    - ### は「親 / 子」という見出しにし、要約系かどうかは親の ## で決める
    """
    summary_headings = summary_headings or DEFAULT_SUMMARY_HEADINGS
    sections = []
    h2 = "（冒頭）"
    current = {"heading": h2, "lines": [], "summary": True}
    in_fence = False

    def flush():
        body = "\n".join(current["lines"]).strip()
        if body:
            sections.append({"heading": current["heading"], "body": body, "summary": current["summary"]})

    for line in strip_front_matter(markdown).splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", line)
        if m and len(m.group(1)) == 1:
            continue  # H1（記事タイトル）は表紙で使うので本文から外す
        if m:
            flush()
            text = m.group(2).strip()
            if len(m.group(1)) == 2:
                h2 = text
                heading = text
            else:
                heading = f"{h2} / {text}"
            current = {"heading": heading, "lines": [], "summary": h2 in summary_headings}
        else:
            current["lines"].append(line)
    flush()
    return sections


def chunk_sections(sections: list[dict], max_chars: int) -> list[list[dict]]:
    """セクションを max_chars 以内のチャンクにまとめる。"""
    chunks = []
    current = []
    current_len = 0
    for sec in sections:
        text = f"## {sec['heading']}\n{sec['body']}\n"
        if current and current_len + len(text) > max_chars:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(sec)
        current_len += len(text)
    if current:
        chunks.append(current)
    return chunks


def target_slide_range(chunk: list[dict]) -> tuple[int, int]:
    """チャンクで作るスライド枚数の目安。
    本文は1000字あたり1.5枚（ただし小見出しごとに1枚以上）、要約系セクションは1セクション1枚まで。"""
    body_chars = sum(len(s["body"]) for s in chunk if not s["summary"])
    n_body = sum(1 for s in chunk if not s["summary"])
    n_summary = sum(1 for s in chunk if s["summary"])
    lo = hi = 0
    if body_chars:
        # 文字数による目安と、本文の小見出しの数（1見出し1枚）の多い方
        base = max(round(body_chars / 1000 * 1.5), n_body)
        lo = max(1, base - 1)
        hi = max(2, min(6, base + 1))
    if n_summary:
        lo += 1 if not body_chars else 0
        hi += n_summary
    return lo, max(lo, hi)


def format_section(sec: dict) -> str:
    mark = " ［要約］" if sec["summary"] else ""
    return f"## {sec['heading']}{mark}\n{sec['body']}"


def build_prompt(template: str, chunk: list[dict], outline: list[str], existing: list[str]) -> str:
    text = "\n\n".join(format_section(s) for s in chunk)
    target_min, target_max = target_slide_range(chunk)
    return (
        template
        .replace("{outline}", "\n".join(f"- {h}" for h in outline))
        .replace("{existing_slides}", "\n".join(f"- {t}" for t in existing) or "（まだありません）")
        .replace("{article_text}", text)
        .replace("{target_slides_min}", str(target_min))
        .replace("{target_slides_max}", str(target_max))
    )


def describe_slide(slide: dict) -> str:
    """作成済みスライドを、重複チェック用の1行にする。"""
    desc = slide.get("title", "")
    diagram = slide.get("diagram") or {}
    if diagram.get("source"):
        desc += f"（図: {detect_diagram_kind(diagram['source']) or '図'}）"
    elif slide.get("layout") == "comparison":
        desc += "（比較）"
    return desc


def call_deepseek(prompt: str, cfg: dict, api_key: str) -> dict:
    """DeepSeek API を JSONモードで呼び出す。"""
    model = cfg["model"]
    max_tokens = cfg["extract"]["max_tokens"]
    max_retries = cfg["extract"]["max_retries"]

    system = "あなたは指示に従い、必ずJSONのみを出力するアシスタントです。"
    user = prompt + "\n\n必ず json 形式で出力してください。"

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(
                API_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                },
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            if not content or not content.strip():
                raise ValueError("空のcontentが返りました（JSONモードの失敗）")
            return json.loads(content)
        except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError) as e:
            last_err = e
            print(f"[warn] attempt {attempt + 1} 失敗: {e}", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"API呼び出しが {max_retries + 1} 回失敗しました: {last_err}")


def extract_title(article: str) -> str:
    """記事のタイトルを取得する。
    優先順位: YAMLフロントマターの title → H1見出し → 空文字
    """
    # YAMLフロントマター
    m = re.match(r"^---\s*\n(.*?)\n---", article, flags=re.DOTALL)
    if m:
        fm = m.group(1)
        t = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', fm, flags=re.MULTILINE)
        if t:
            return t.group(1).strip()
    # H1見出し
    h1 = re.search(r"^#\s+(.+)$", article, flags=re.MULTILINE)
    if h1:
        return h1.group(1).strip()
    return ""


def slug_from_path(path: Path) -> str:
    return path.stem


def main():
    parser = argparse.ArgumentParser(description="記事から deck_ir.json を生成する")
    parser.add_argument("article", help="入力する記事のMarkdownパス")
    parser.add_argument("-o", "--output", help="出力先ディレクトリ（デフォルト: output/<slug>/）")
    args = parser.parse_args()

    article_path = Path(args.article)
    if not article_path.exists():
        print(f"[error] ファイルが見つかりません: {article_path}", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("[error] DEEPSEEK_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    template = PROMPT_PATH.read_text(encoding="utf-8")
    article = article_path.read_text(encoding="utf-8")

    slug = slug_from_path(article_path)
    out_dir = Path(args.output) if args.output else PROJECT_ROOT / "output" / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[info] article: {article_path}")
    print(f"[info] slug: {slug}")
    print(f"[info] output: {out_dir}")

    summary_headings = cfg["extract"].get("summary_headings") or DEFAULT_SUMMARY_HEADINGS
    sections = split_sections(article, summary_headings)
    print(f"[info] sections: {len(sections)}（うち要約系 {sum(s['summary'] for s in sections)}）")
    outline = [s["heading"] + ("［要約］" if s["summary"] else "") for s in sections]

    chunks = chunk_sections(sections, cfg["extract"]["chunk_max_chars"])
    print(f"[info] chunks: {len(chunks)}  （1チャンク = 1回のAPI呼び出し）")

    all_slides = []
    for i, chunk in enumerate(chunks, 1):
        lo, hi = target_slide_range(chunk)
        print(f"[info] chunk {i}/{len(chunks)} を抽出中...（{lo}〜{hi}枚）")
        existing = [describe_slide(s) for s in all_slides]
        prompt = build_prompt(template, chunk, outline, existing)
        result = call_deepseek(prompt, cfg, api_key)
        slides = result.get("slides", [])
        print(f"       → {len(slides)} 枚のスライドを抽出")
        all_slides.extend(slides)

    # 表紙スライドがなければ先頭に追加
    if not any(s.get("layout") == "title" for s in all_slides):
        h1 = extract_title(article)
        cover_title = h1 if h1 else slug.replace("-", " ").title()
        cover_sub = "DeepSeek API 技術解説"
        all_slides.insert(0, {
            "layout": "title",
            "title": cover_title,
            "bullets": [cover_sub],
            "narration": [f"この動画では、{cover_title}について解説します。"],
            "notes": "",
            "diagram": None,
        })

    # 実測メタデータ
    narration_chars = sum(len("".join(s.get("narration", []))) for s in all_slides)
    per_min = cfg["video"]["narration_chars_per_min"]
    est_min = round(narration_chars / per_min, 1) if per_min else 0

    deck = {
        "meta": {
            "source": str(article_path.resolve().relative_to(PROJECT_ROOT)),
            "model": cfg["model"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "actual_slides": len(all_slides),
            "actual_narration_chars": narration_chars,
            "estimated_duration_min": est_min,
        },
        "slides": all_slides,
    }

    # Pydanticで検証
    try:
        validated = DeckIR(**deck)
    except Exception as e:
        print(f"[error] スキーマ検証に失敗: {e}", file=sys.stderr)
        sys.exit(1)

    out_path = out_dir / "deck_ir.json"
    out_path.write_text(
        validated.model_dump_json(indent=2, exclude_none=False),
        encoding="utf-8",
    )
    print(f"[done] 保存: {out_path}")
    print(f"[meta] slides={len(all_slides)}, narration_chars={narration_chars}, est_duration={est_min}分")
    kinds = [s.diagram.kind or "不明" for s in validated.slides if s.diagram]
    if kinds:
        summary = ", ".join(f"{k}={kinds.count(k)}" for k in sorted(set(kinds)))
        print(f"[meta] diagrams: {summary}")


if __name__ == "__main__":
    main()
