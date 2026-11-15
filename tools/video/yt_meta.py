#!/usr/bin/env python3
"""deck_ir.json と timing.json から YouTube 用のタイトル・説明・タグを生成する。"""
import argparse
import json
import sys
from pathlib import Path

ARTICLE_BASE_URL = "https://deepseek.ac/articles"


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def build_chapters(slides: list[dict], timings: list[dict]) -> list[str]:
    lines = []
    cumulative = 0.0
    for i, (slide, t) in enumerate(zip(slides, timings), 1):
        ts = format_timestamp(cumulative)
        title = slide.get("title", f"スライド{i}")
        lines.append(f"{ts} {title}")
        cumulative += t.get("duration_sec", 0.0)
    return lines


def main():
    parser = argparse.ArgumentParser(description="YouTubeメタデータを生成する")
    parser.add_argument("ir", help="deck_ir.json のパス")
    parser.add_argument("-t", "--timing", help="timing.json（デフォルト: audio/timing.json）")
    parser.add_argument("-o", "--output", help="出力先（デフォルト: 標準出力）")
    parser.add_argument("--article-url", default="", help="元記事のURL（指定時は説明欄に載せる）")
    parser.add_argument("--title", default="", help="タイトルを明示指定（省略時は記事タイトルから生成）")
    args = parser.parse_args()

    ir_path = Path(args.ir).resolve()
    deck = json.loads(ir_path.read_text(encoding="utf-8"))

    timing_path = Path(args.timing).resolve() if args.timing else ir_path.parent / "audio" / "timing.json"
    timing = json.loads(timing_path.read_text(encoding="utf-8"))

    meta = deck.get("meta", {})
    source = meta.get("source", "")
    slides = deck["slides"]
    timings = timing["slides"]

    # タイトル
    if args.title:
        title = args.title
    else:
        base = slides[0].get("title", "").split("：")[0].split(":")[0]
        title = f"{base}｜JSON Schemaと検証をセットで設計"

    # チャプター
    chapters = build_chapters(slides, timings)

    # 説明文
    desc_lines = []
    desc_lines.append("LLMの構造化出力は「スキーマを定義する」だけでは壊れます。")
    desc_lines.append("この動画では、DeepSeek APIでJSON Schemaを定義し、検証し、失敗時にどうリトライするかまでを1本にまとめます。")
    desc_lines.append("")
    desc_lines.append("▼ この動画で扱う内容")
    for s in slides[1:]:  # 表紙を除く
        desc_lines.append(f"・{s.get('title', '')}")
    desc_lines.append("")
    if args.article_url:
        desc_lines.append(f"▼ 元記事\n{args.article_url}\n")
    desc_lines.append("▼ チャプター")
    desc_lines.extend(chapters)
    desc_lines.append("")
    desc_lines.append("#DeepSeek #LLM #JSONSchema #構造化出力 #API")

    description = "\n".join(desc_lines)

    output = {
        "title": title,
        "description": description,
        "tags": [
            "DeepSeek", "DeepSeek API", "LLM", "JSON Schema",
            "構造化出力", "Pydantic", "Zod", "バリデーション",
            "API設計", "プロンプトエンジニアリング",
        ],
        "category_id": "28",  # Science & Technology
        "privacy_status": "unlisted",
    }

    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"[done] {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
