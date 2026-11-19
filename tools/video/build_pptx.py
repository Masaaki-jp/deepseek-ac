#!/usr/bin/env python3
"""deck_ir.json から deck.pptx を組み立てる。"""
import argparse
import io
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageOps
from pptx import Presentation
from pptx.util import Pt, Inches
from pptx.oxml.ns import qn
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import PP_ALIGN

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.video.schema import detect_diagram_kind  # noqa: E402
from tools.video.diagram_render import ZONES, DiagramRenderer, load_diagram_config  # noqa: E402

# ==========================================
# 定数・設定
# ==========================================
BG_COLOR = RGBColor(0x1A, 0x1B, 0x26)  # ダーク背景
TEXT_COLOR = RGBColor(0xFF, 0xFF, 0xFF) # 白文字
JP_FONT = "メイリオ"
TITLE_SIZE = 32
HERO_BULLET_SIZE = 28   # 図スキップ時の中央テキスト
SPLIT_BULLET_SIZE = 20  # 2分割時の左テキスト
COMPARE_HEADING_SIZE = 28  # 比較レイアウトの列見出し
COMPARE_ITEM_SIZE = 24     # 比較レイアウトの項目
ACCENT_COLOR = RGBColor(0x7A, 0xA2, 0xF7)   # 列見出しと下線
DIVIDER_COLOR = RGBColor(0x56, 0x5F, 0x89)  # 列の仕切り線

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

# ==========================================
# レイアウト設定関数
# ==========================================
MINIMAP = {"max_w": 3.7, "max_h": 0.9, "right": 12.9, "top": 0.3, "pad": 0.1}  # 分解図の右上に置く全体図
MINIMAP_TITLE_W = 8.4  # ミニマップを置くスライドのタイトル幅（32ptで約19文字）


def add_minimap(slide, renderer, overview: str, focus: str, slide_no: int) -> bool:
    """分解図のスライドの右上に、今見ている部分を強調した小さな全体図を置く。"""
    kind = detect_diagram_kind(overview)
    if kind != "flowchart":
        return False
    try:
        img, w_in, h_in, _, _ = renderer.render_best(overview, kind, MINIMAP["max_w"], MINIMAP["max_h"], focus=focus)
    except Exception as e:  # noqa: BLE001
        print(f"  [skip] #{slide_no} ミニマップを置けませんでした: {e}", file=sys.stderr)
        return False
    pad = MINIMAP["pad"]
    frame_w, frame_h = w_in + 2 * pad, h_in + 2 * pad
    left, top_ = MINIMAP["right"] - frame_w, MINIMAP["top"]
    frame = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top_),
                                   Inches(frame_w), Inches(frame_h))
    frame.fill.background()
    frame.line.color.rgb = DIVIDER_COLOR
    frame.line.width = Pt(1)
    frame.adjustments[0] = 0.08
    stream = io.BytesIO()
    img.save(stream, format="PNG")
    stream.seek(0)
    slide.shapes.add_picture(stream, left=Inches(left + pad), top=Inches(top_ + pad),
                             width=Inches(w_in), height=Inches(h_in))
    return True


def set_title(slide, text: str, is_cover: bool = False, width_in: float = 12.3):
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
        title_shape.width = Inches(width_in)
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

def remove_body_placeholder(slide):
    for shape in list(slide.placeholders):
        if shape.placeholder_format.idx == 1:
            shape._element.getparent().remove(shape._element)


def add_line(slide, x1: float, y1: float, x2: float, y2: float, color: RGBColor, width_pt: float):
    line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    line.line.color.rgb = color
    line.line.width = Pt(width_pt)


def set_comparison(slide, columns: list[dict]) -> bool:
    """左右2列の比較レイアウト。列の形が不正なら False を返す（箇条書きにフォールバック）。"""
    cols = [c for c in columns if isinstance(c, dict) and c.get("heading") and c.get("items")]
    if len(cols) != 2:
        return False
    remove_body_placeholder(slide)

    top, bottom = 1.7, 5.7       # 字幕と被らないよう 5.7 インチまで
    col_w, gap = 5.4, 1.0
    lefts = [0.9, 0.9 + col_w + gap]
    divider_x = lefts[0] + col_w + gap / 2
    add_line(slide, divider_x, top + 0.1, divider_x, bottom, DIVIDER_COLOR, 1.5)

    for left, col in zip(lefts, cols):
        head = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(col_w), Inches(0.7))
        tf = head.text_frame
        tf.word_wrap = True
        run = tf.paragraphs[0].add_run()
        run.text = sanitize_bullet(col["heading"])
        set_japanese_font(run, size=COMPARE_HEADING_SIZE, bold=True)
        run.font.color.rgb = ACCENT_COLOR
        add_line(slide, left, top + 0.75, left + col_w, top + 0.75, ACCENT_COLOR, 2)

        body = slide.shapes.add_textbox(Inches(left), Inches(top + 0.95), Inches(col_w), Inches(bottom - top - 0.95))
        tf = body.text_frame
        tf.word_wrap = True
        for k, item in enumerate(col["items"][:4]):
            p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
            p.space_after = Pt(12)
            run = p.add_run()
            run.text = "• " + sanitize_bullet(item)
            set_japanese_font(run, size=COMPARE_ITEM_SIZE)
    return True


def set_notes(slide, notes: str):
    if not notes:
        return
    notes_slide = slide.notes_slide
    tf = notes_slide.notes_text_frame
    tf.text = notes

# ==========================================
# ロゴ
# ==========================================

def add_logo(slide):
    """スライドの右下にロゴを配置する。ダークテーマ用に白抜きにする。"""
    try:
        logo_path = Path(__file__).parent / "assets" / "logo.png"
        if not logo_path.exists():
            print(f"[warn] ロゴが見つかりません: {logo_path}", file=sys.stderr)
            return

        with Image.open(logo_path) as img:
            img = img.convert("RGBA")
            if img.width > 800:
                img = img.resize((800, int(800 * img.height / img.width)), Image.LANCZOS)
                
            # 明るい部分（R,G,B すべて200超）を透明に、それ以外を白にする
            r, g, b, a = img.split()
            light = ImageChops.multiply(
                ImageChops.multiply(*(ch.point(lambda v: 255 if v > 200 else 0) for ch in (r, g))),
                b.point(lambda v: 255 if v > 200 else 0),
            )
            alpha = ImageChops.multiply(a, ImageOps.invert(light))
            img = Image.new("RGBA", img.size, (255, 255, 255, 0))
            img.putalpha(alpha)

            stream = io.BytesIO()
            img.save(stream, format="PNG")
            stream.seek(0)
            
            slide.shapes.add_picture(
                stream,
                left=Inches(11.0),
                top=Inches(6.5),
                width=Inches(1.8)
            )
            print(f"[done] ロゴを配置しました", file=sys.stderr)
    except Exception as e:
        print(f"[error] ロゴ配置失敗: {e}", file=sys.stderr)

# ==========================================
# メイン組み立てロジック
# ==========================================
def build(ir_path: Path, out_path: Path, template: Path | None = None):
    deck = json.loads(ir_path.read_text(encoding="utf-8"))
    renderer = DiagramRenderer(load_diagram_config())
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
        is_compare = (layout_name == "comparison")
        
        diagram = s.get("diagram")
        has_minimap = bool(diagram and diagram.get("overview") and diagram.get("focus"))
        # ミニマップを置くスライドは、タイトルが重ならないよう幅を詰める
        set_title(slide, s.get("title", ""), is_cover=is_cover,
                  width_in=MINIMAP_TITLE_W if has_minimap else 12.3)
        if has_minimap and add_minimap(slide, renderer, diagram["overview"], diagram["focus"], i + 1):
            print(f"[done] #{i+1} ミニマップを置きました（強調: {diagram['focus']}）", file=sys.stderr)
        set_notes(slide, s.get("notes", ""))

        # 図解のレンダリングと配置
        has_image = False
        
        if diagram and diagram.get("type", "mermaid") == "mermaid" and diagram.get("source"):
            kind = detect_diagram_kind(diagram["source"])
            zone = ZONES["hero" if is_hero else "split"]
            max_w, max_h = zone["max_w"], zone["max_h"]
            zone_left, zone_top = zone["left"], zone["top"]
            try:
                if kind not in renderer.cfg["kinds"]:
                    raise ValueError(f"対応していない図の種類（{kind or '不明'}）")
                img, final_w_in, final_h_in, font_px, direction = renderer.render(
                    diagram["source"], kind, max_w, max_h)
                stream = io.BytesIO()
                img.save(stream, format="PNG")
                stream.seek(0)
                actual_left = zone_left + (max_w - final_w_in) / 2
                actual_top = zone_top + (max_h - final_h_in) / 2
                slide.shapes.add_picture(
                    stream,
                    left=Inches(actual_left),
                    top=Inches(actual_top),
                    width=Inches(final_w_in),
                    height=Inches(final_h_in),
                )
                has_image = True
                detail = f"{kind}{' ' + direction if direction else ''}"
                if font_px is not None:
                    detail += f", 文字{font_px:.0f}px"
                print(f"[done] #{i+1} 図を貼り付けました ({detail}, 配置:w{final_w_in:.1f} h{final_h_in:.1f})", file=sys.stderr)
            except Exception as e:
                print(f"  [skip] #{i+1} 図を使わず箇条書きにします: {e}", file=sys.stderr)

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
                
        elif is_compare and not has_image:
            if set_comparison(slide, s.get("columns") or []):
                print(f"[done] #{i+1} 比較レイアウトを配置しました", file=sys.stderr)
            else:
                print(f"  [skip] #{i+1} 比較の列が2つそろっていないため箇条書きにします", file=sys.stderr)
                set_centered_bullets(slide, bullets, HERO_BULLET_SIZE)

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

        # すべてのスライドの右下にロゴを追加
        add_logo(slide)

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
