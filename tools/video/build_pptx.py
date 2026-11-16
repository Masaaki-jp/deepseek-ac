#!/usr/bin/env python3
"""deck_ir.json から deck.pptx を組み立てる。"""
import argparse
import base64
import io
import json
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image
from pptx import Presentation
from pptx.util import Pt, Inches
from pptx.oxml.ns import qn
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

# ==========================================
# 定数・設定
# ==========================================
BG_COLOR = RGBColor(0x1A, 0x1B, 0x26)  # ダーク背景
TEXT_COLOR = RGBColor(0xFF, 0xFF, 0xFF) # 白文字
JP_FONT = "メイリオ"
TITLE_SIZE = 32
HERO_BULLET_SIZE = 28   # 図スキップ時の中央テキスト
SPLIT_BULLET_SIZE = 20  # 2分割時の左テキスト

# ==========================================
# ユーティリティ関数
# ==========================================
def sanitize_title(text: str) -> str:
    return re.sub(r"^#+\s*", "", text).strip()

def sanitize_bullet(text: str) -> str:
    return re.sub(r"^[・\-\*•]\s*", "", text).strip()

def set_japanese_font(run, font_name: str = JP_FONT, size: int | None = None, bold: bool = False):
    if size is not None:
        run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = font_name
    run.font.color.rgb = TEXT_COLOR
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", font_name)

def is_extreme_aspect(aspect: float, threshold: float = 3.0) -> bool:
    return aspect > threshold or aspect < (1.0 / threshold)

# ==========================================
# レイアウト設定関数
# ==========================================
def set_title(slide, text: str, is_cover: bool = False):
    title_shape = slide.shapes.title
    if title_shape is None:
        return

    if is_cover:
        title_shape.left = Inches(0.8)
        title_shape.top = Inches(2.5)
        title_shape.width = Inches(11.7)
        title_shape.height = Inches(2.0)
    else:
        title_shape.left = Inches(0.5)
        title_shape.top = Inches(0.3)
        title_shape.width = Inches(12.3)
        title_shape.height = Inches(1.0)

    tf = title_shape.text_frame
    tf.text = ""
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER if is_cover else PP_ALIGN.LEFT
    run = p.add_run()
    run.text = sanitize_title(text)
    set_japanese_font(run, size=TITLE_SIZE + (4 if is_cover else 0), bold=True)

def set_centered_bullets(slide, bullets: list[str], size: int):
    """画面中央にテキストを大きく配置（図なし・フォールバック用）"""
    if not bullets:
        return
    body = None
    for shape in slide.placeholders:
        if shape.placeholder_format.idx == 1:
            body = shape
            break
    if body is None:
        return

    body.left = Inches(1.5)
    body.top = Inches(2.0)
    body.width = Inches(10.3)
    body.height = Inches(4.5)

    tf = body.text_frame
    tf.clear()
    for i, b in enumerate(bullets):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.level = 0
        p.alignment = PP_ALIGN.LEFT
        run = p.add_run()
        run.text = sanitize_bullet(b)
        set_japanese_font(run, size=size)

def set_split_bullets(slide, bullets: list[str]):
    """2分割レイアウトの左側テキスト"""
    if not bullets:
        return
    body = None
    for shape in slide.placeholders:
        if shape.placeholder_format.idx == 1:
            body = shape
            break
    if body is None:
        return

    body.left = Inches(0.5)
    body.top = Inches(1.8)
    body.width = Inches(6.0)
    body.height = Inches(4.5)

    tf = body.text_frame
    tf.clear()
    for i, b in enumerate(bullets):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.level = 0
        p.alignment = PP_ALIGN.LEFT
        run = p.add_run()
        run.text = sanitize_bullet(b)
        set_japanese_font(run, size=SPLIT_BULLET_SIZE)

def set_notes(slide, notes: str):
    if not notes:
        return
    notes_slide = slide.notes_slide
    tf = notes_slide.notes_text_frame
    tf.text = notes

# ==========================================
# Mermaid レンダリング
# ==========================================
def trim_whitespace(img: Image.Image) -> Image.Image:
    """画像の白背景を透明にし、暗い色の線や文字を白に変換する。"""
    img = img.convert("RGBA")
    datas = img.getdata()
    new_data = []
    for item in datas:
        r, g, b, a = item
        # 背景（明るい色）を完全な透明にする
        if r > 200 and g > 200 and b > 200:
            new_data.append((255, 255, 255, 0))
        else:
            # 線や文字（暗い色）を白（#ffffff）にする
            new_data.append((255, 255, 255, a))
    img.putdata(new_data)
    
    # 透明部分を基準にトリミング
    bbox = img.getbbox()
    if bbox:
        return img.crop(bbox)
    return img


def choose_flow_direction(source: str) -> str:
    """安定した表示のため、常に縦方向（TD）を強制する。"""
    return "TD"

def render_mermaid(source: str, retries: int = 3) -> bytes:
    # 特殊トークンの除去
    source = re.sub(r'<\|.*?\|>', '', source)
    
    # 方向自動判定
    direction = choose_flow_direction(source)
    source = re.sub(r'flowchart\s+(LR|TD)', f'flowchart {direction}', source)

    init_directive = """%%{init: {
  'theme': 'base',
  'flowchart': { 'curve': 'basis' },
  'themeVariables': {
    'fontFamily': '"Noto Sans JP", "Hiragino Kaku Gothic ProN", "Meiryo", sans-serif',
    'fontSize': '18px',
    'primaryTextColor': '#ffffff',
    'lineColor': '#9aa5ce',
    'primaryColor': '#24283b',
    'primaryBorderColor': '#7aa2f7',
    'background': 'transparent'
  }
}}%%
"""
    if not source.startswith("%%{init"):
        source = init_directive + source

    encoded = base64.urlsafe_b64encode(source.encode("utf-8")).decode("ascii")
    url = f"https://mermaid.ink/img/{encoded}?bgColor=transparent&width=2000"

    for i in range(retries):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                return r.content
            elif r.status_code in (503, 504):
                print(f"  [retry] mermaid.ink 503/504 エラー。リトライします ({i+1}/{retries})", file=sys.stderr)
                time.sleep(2 ** i)
            else:
                r.raise_for_status()
        except requests.RequestException as e:
            if i == retries - 1:
                raise RuntimeError(f"mermaid.ink 取得失敗: {e}")
            time.sleep(2 ** i)
    raise RuntimeError("mermaid.ink からの画像取得に失敗しました")

# ==========================================
# メイン組み立てロジック
# ==========================================
def build(ir_path: Path, out_path: Path, template: Path | None = None):
    deck = json.loads(ir_path.read_text(encoding="utf-8"))
    prs = Presentation(str(template)) if template else Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    for i, s in enumerate(deck["slides"]):
        layout_name = s.get("layout", "title_and_content")
        layout_idx = 0 if layout_name == "title" else 1
        slide = prs.slides.add_slide(prs.slide_layouts[layout_idx])
        
        # ダーク背景適用
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = BG_COLOR

        is_cover = (layout_name == "title")
        is_hero = (layout_name == "diagram_hero")
        
        set_title(slide, s.get("title", ""), is_cover=is_cover)
        set_notes(slide, s.get("notes", ""))

        # 図解のレンダリングと配置
        diagram = s.get("diagram")
        has_image = False
        
        if diagram and diagram.get("type") == "mermaid" and diagram.get("source"):
            try:
                png_bytes = render_mermaid(diagram["source"])
                image_stream = io.BytesIO(png_bytes)
                with Image.open(image_stream) as img:
                    img = trim_whitespace(img)
                    trimmed_stream = io.BytesIO()
                    img.save(trimmed_stream, format="PNG")
                    trimmed_bytes = trimmed_stream.getvalue()
                    w, h = img.size
                    aspect = w / h

                if is_extreme_aspect(aspect):
                    print(f"  [skip] #{i+1} 図のアスペクト比が極端（{aspect:.2f}）のためスキップ", file=sys.stderr)
                else:
                    has_image = True
                    if is_hero:
                        # 字幕と被らないよう、高さを4.2インチに制限
                        max_w, max_h = 12.1, 4.2
                        zone_left, zone_top = 0.6, 1.5
                    else:
                        max_w, max_h = 6.0, 4.5
                        zone_left, zone_top = 6.8, 1.8

                    if aspect > (max_w / max_h):
                        final_w_in = max_w
                        final_h_in = max_w / aspect
                    else:
                        final_h_in = max_h
                        final_w_in = max_h * aspect

                    actual_left = zone_left + (max_w - final_w_in) / 2
                    actual_top = zone_top + (max_h - final_h_in) / 2

                    trimmed_stream = io.BytesIO(trimmed_bytes)
                    slide.shapes.add_picture(
                        trimmed_stream,
                        left=Inches(actual_left),
                        top=Inches(actual_top),
                        width=Inches(final_w_in),
                        height=Inches(final_h_in)
                    )
                    print(f"[done] #{i+1} diagram を貼り付けました (画像:{w}x{h}, 配置:w{final_w_in:.1f} h{final_h_in:.1f}, x{actual_left:.1f} y{actual_top:.1f})", file=sys.stderr)
            except Exception as e:
                print(f"[warn] #{i+1} diagram のレンダリング失敗: {e}", file=sys.stderr)

        # テキストの配置（図があるかどうかで完全分離）
        bullets = s.get("bullets", [])
        
        if is_cover:
            if bullets:
                body = slide.placeholders[1]
                body.left = Inches(0.8)
                body.top = Inches(4.5)
                body.width = Inches(11.7)
                body.height = Inches(1.0)
                body.text_frame.text = ""
                p = body.text_frame.paragraphs[0]
                p.alignment = PP_ALIGN.CENTER
                run = p.add_run()
                run.text = sanitize_bullet(bullets[0])
                set_japanese_font(run, size=24)
                
        elif is_hero:
            if not has_image:
                # 図がスキップされた or 無い場合：中央に大きくテキスト
                set_centered_bullets(slide, bullets, HERO_BULLET_SIZE)
            # 図が成功した場合は何もしない（タイトルと図だけ表示）
            
        else: # title_and_content
            if has_image:
                # 図がある場合：左にテキスト、右に図
                set_split_bullets(slide, bullets)
            else:
                # 図がない場合：中央に大きくテキスト
                set_centered_bullets(slide, bullets, HERO_BULLET_SIZE)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    print(f"[done] 保存: {out_path} ({len(deck['slides'])} 枚)")

def main():
    parser = argparse.ArgumentParser(description="deck_ir.json から PPTX を生成する")
    parser.add_argument("ir", help="入力する deck_ir.json のパス")
    parser.add_argument("-o", "--output", help="出力する .pptx のパス")
    parser.add_argument("--template", help="既存の .pptx テンプレート（任意）")
    args = parser.parse_args()

    ir_path = Path(args.ir).resolve()
    if not ir_path.exists():
        print(f"[error] ファイルが見つかりません: {ir_path}", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.output).resolve() if args.output else ir_path.parent / "deck.pptx"
    template = Path(args.template).resolve() if args.template else None
    build(ir_path, out_path, template)

if __name__ == "__main__":
    main()
