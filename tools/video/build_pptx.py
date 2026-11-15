#!/usr/bin/env python3
"""deck_ir.json から deck.pptx を組み立てる。"""
import argparse
import json
import re
import sys
from pathlib import Path

from pptx import Presentation
from pptx.util import Pt
from pptx.oxml.ns import qn

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 日本語フォント設定
JP_FONT = "メイリオ"
TITLE_SIZE = 32
BODY_SIZE = 20
BULLET_SIZE = 18




def sanitize_title(text: str) -> str:
    """先頭の Markdown 見出し記号を除去する。"""
    return re.sub(r"^#+\s*", "", text).strip()


def sanitize_bullet(text: str) -> str:
    """先頭の箇条書き記号（・、*、-、• など）を除去する。"""
    return re.sub(r"^[・\-\*•]\s*", "", text).strip()


def set_japanese_font(run, font_name: str = JP_FONT, size: int | None = None, bold: bool = False):
    """runに日本語フォントを確実に適用する（a:latin, a:ea, a:csの3つ）。"""
    if size is not None:
        run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = font_name  # a:latin
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", font_name)


def set_title(slide, text: str):
    """タイトルプレースホルダーに日本語フォントでテキストを入れる。"""
    title_shape = slide.shapes.title
    if title_shape is None:
        return
    tf = title_shape.text_frame
    tf.text = ""
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = sanitize_title(text)
    set_japanese_font(run, size=TITLE_SIZE, bold=True)


def set_bullets(slide, bullets: list[str]):
    """本文プレースホルダーに箇条書きを入れる。"""
    if not bullets:
        return
    # 本文プレースホルダーを探す（index 1 が一般的）
    body = None
    for shape in slide.placeholders:
        if shape.placeholder_format.idx == 1:
            body = shape
            break
    if body is None:
        return

    tf = body.text_frame
    tf.clear()
    for i, b in enumerate(bullets):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.level = 0
        run = p.add_run()
        run.text = sanitize_bullet(b)
        set_japanese_font(run, size=BULLET_SIZE)


def set_notes(slide, notes: str):
    """発表者ノートを設定する。"""
    if not notes:
        return
    notes_slide = slide.notes_slide
    tf = notes_slide.notes_text_frame
    tf.text = notes


def build(ir_path: Path, out_path: Path, template: Path | None = None):
    deck = json.loads(ir_path.read_text(encoding="utf-8"))
    prs = Presentation(str(template)) if template else Presentation()

    for i, s in enumerate(deck["slides"]):
        layout_name = s.get("layout", "title_and_content")
        layout_idx = 0 if layout_name == "title" else 1
        slide = prs.slides.add_slide(prs.slide_layouts[layout_idx])

        set_title(slide, s.get("title", ""))
        set_bullets(slide, s.get("bullets", []))
        set_notes(slide, s.get("notes", ""))

        # 図解は Phase 5 で実装。今は null 前提でスキップ。
        if s.get("diagram"):
            print(f"[skip] #{i+1} diagram は Phase 5 で対応予定", file=sys.stderr)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    print(f"[done] 保存: {out_path} ({len(deck['slides'])} 枚)")


def main():
    parser = argparse.ArgumentParser(description="deck_ir.json から PPTX を生成する")
    parser.add_argument("ir", help="入力する deck_ir.json のパス")
    parser.add_argument("-o", "--output", help="出力する .pptx のパス（デフォルト: 同ディレクトリの deck.pptx）")
    parser.add_argument("--template", help="既存の .pptx テンプレート（任意）")
    args = parser.parse_args()

    ir_path = Path(args.ir).resolve()
    if not ir_path.exists():
        print(f"[error] ファイルが見つかりません: {ir_path}", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.output).resolve() if args.output else ir_path.parent / "deck.pptx"
    template = Path(args.template).resolve() if args.template else None

    print(f"[info] input:  {ir_path}")
    print(f"[info] output: {out_path}")
    if template:
        print(f"[info] template: {template}")

    build(ir_path, out_path, template)


if __name__ == "__main__":
    main()
