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

from tools.video.schema import DeckIR, SlideIR

API_URL = "https://api.deepseek.com/chat/completions"
PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.txt"
CONFIG_PATH = Path(__file__).parent / "config.yaml"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def split_sections(markdown: str) -> list[dict]:
    """## 見出しでセクションに分割する。"""
    # ## で始まる行を境界にする
    parts = re.split(r"^(## .+)$", markdown, flags=re.MULTILINE)
    sections = []
    # parts[0] は最初の ## より前（タイトルなど）
    if parts[0].strip():
        sections.append({"heading": "（冒頭）", "body": parts[0].strip()})
    for i in range(1, len(parts), 2):
        heading = parts[i].lstrip("#").strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        sections.append({"heading": heading, "body": body})
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


def build_prompt(template: str, chunk: list[dict]) -> str:
    text = "\n\n".join(f"## {s['heading']}\n{s['body']}" for s in chunk)
    char_count = len(text)
    # 1000字あたり1.5枚を目安に計算（下限2、上限5）
    base = round(char_count / 1000 * 1.5)
    target_min = max(2, base - 1)
    target_max = max(target_min + 1, min(5, base + 1))
    return (
        template
        .replace("{article_text}", text)
        .replace("{target_slides_min}", str(target_min))
        .replace("{target_slides_max}", str(target_max))
    )


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

    sections = split_sections(article)
    print(f"[info] sections: {len(sections)}")

    chunks = chunk_sections(sections, cfg["extract"]["chunk_max_chars"])
    print(f"[info] chunks: {len(chunks)}  （1チャンク = 1回のAPI呼び出し）")

    all_slides = []
    for i, chunk in enumerate(chunks, 1):
        print(f"[info] chunk {i}/{len(chunks)} を抽出中...")
        prompt = build_prompt(template, chunk)
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
