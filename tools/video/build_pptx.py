#!/usr/bin/env python3
"""deck_ir.json から deck.pptx を組み立てる。"""
import argparse
import base64
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import yaml
from PIL import Image, ImageOps
from pptx import Presentation
from pptx.util import Pt, Inches
from pptx.oxml.ns import qn
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.video.schema import detect_diagram_kind  # noqa: E402

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

def is_extreme_aspect(aspect: float, threshold: float = 2.4) -> bool:
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
                
            datas = list(img.getdata())
            new_data = []
            for item in datas:
                r, g, b, a = item
                if r > 200 and g > 200 and b > 200:
                    new_data.append((255, 255, 255, 0))
                else:
                    new_data.append((255, 255, 255, a))
            img.putdata(new_data)
            
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
# Mermaid レンダリング
# ==========================================
# 白黒の線画で描き、あとで「白い線・透明な背景」に変換する
_W, _B = "#ffffff", "#000000"
MERMAID_CONFIG = {
    "theme": "base",
    "fontSize": 18,  # シーケンス図の文字はこちらで決まる
    "flowchart": {"curve": "basis"},
    # 下側の登場人物の箱を省略し、文字サイズをほかの図とそろえる
    "sequence": {"mirrorActors": False, "actorFontSize": 18, "messageFontSize": 18, "noteFontSize": 18},
    "themeVariables": {
        "fontFamily": '"Noto Sans CJK JP", "Noto Sans JP", "Hiragino Kaku Gothic ProN", "Meiryo", sans-serif',
        "fontSize": "18px",
        "background": _W, "mainBkg": _W, "textColor": _B, "lineColor": _B,
        "primaryColor": _W, "primaryTextColor": _B, "primaryBorderColor": _B,
        "secondaryColor": _W, "tertiaryColor": _W, "nodeBorder": _B,
        "clusterBkg": _W, "clusterBorder": _B, "edgeLabelBackground": _W,
        # シーケンス図
        "actorBkg": _W, "actorBorder": _B, "actorTextColor": _B, "actorLineColor": _B,
        "signalColor": _B, "signalTextColor": _B, "labelBoxBkgColor": _W,
        "labelBoxBorderColor": _B, "labelTextColor": _B, "loopTextColor": _B,
        "noteBkgColor": _W, "noteBorderColor": _B, "noteTextColor": _B,
        "activationBkgColor": _W, "activationBorderColor": _B, "sequenceNumberColor": _W,
        # 状態遷移図
        "stateBkg": _W, "stateLabelColor": _B, "transitionColor": _B,
        "transitionLabelColor": _B, "compositeBackground": _W,
        "compositeTitleBackground": _W, "altBackground": _W,
        "specialStateColor": _B, "innerEndBackground": _B,
    },
}
MERMAID_BASE_FONT_PX = 18
PX_PER_INCH = 1920 / 13.333  # 動画（1920px幅）上での1インチあたりのピクセル数

TOOLS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOLS_DIR / "config.yaml"
MERMAID_DIR = TOOLS_DIR / "mermaid"

DEFAULT_DIAGRAM_CFG = {
    "renderer": "local",   # local（mermaid-cli）/ ink（mermaid.ink）
    "render_scale": 3,     # ローカル描画の倍率（大きいほど高精細）
    "min_font_px": 16,     # 動画上で文字がこれより小さくなる図は使わない
    "max_font_px": 36,     # 小さい図を拡大しすぎない上限
    "kinds": ["flowchart", "sequence", "state"],
}


def load_diagram_config() -> dict:
    cfg = dict(DEFAULT_DIAGRAM_CFG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update((yaml.safe_load(f) or {}).get("diagram") or {})
    except FileNotFoundError:
        pass
    return cfg


def find_mmdc() -> str | None:
    """tools/video/mermaid に入れた mermaid-cli を優先し、なければ PATH から探す。"""
    local = MERMAID_DIR / "node_modules" / ".bin" / "mmdc"
    if local.exists():
        return str(local)
    return shutil.which("mmdc")


def to_line_art(png_bytes: bytes) -> Image.Image:
    """白背景・黒線の画像を「白い線・透明な背景」に変換し、余白を切り落とす。
    明るさをそのまま透明度に使うので、線の縁もなめらかになる。"""
    with Image.open(io.BytesIO(png_bytes)) as src:
        gray = src.convert("RGBA")
        bg = Image.new("RGBA", gray.size, (255, 255, 255, 255))
        gray = Image.alpha_composite(bg, gray).convert("L")
    alpha = ImageOps.invert(gray)
    img = Image.new("RGBA", gray.size, (255, 255, 255, 0))
    img.putalpha(alpha)
    bbox = alpha.point(lambda v: 255 if v > 8 else 0).getbbox()
    return img.crop(bbox) if bbox else img


def set_direction(source: str, kind: str, direction: str) -> str:
    """図の向きを書き換える。flowchart は1行目、state は direction 行で指定する。"""
    if kind == "flowchart":
        return re.sub(
            r"^(\s*)(flowchart|graph)\b[ \t]*(TD|TB|BT|LR|RL)?",
            lambda m: f"{m.group(1)}flowchart {direction}",
            source, count=1, flags=re.MULTILINE,
        )
    if kind == "state":
        body = re.sub(r"^\s*direction\s+(TB|TD|BT|LR|RL)\s*$\n?", "", source, count=1, flags=re.MULTILINE)
        return re.sub(r"^(\s*stateDiagram(-v2)?\s*)$", rf"\1\n  direction {direction}",
                      body, count=1, flags=re.MULTILINE)
    return source


# 向きを選び直せる図の種類と、試す順番
DIRECTIONS = {"flowchart": ["TD", "LR"], "state": ["TB", "LR"]}


def clean_source(source: str) -> str:
    source = re.sub(r"<\|.*?\|>", "", source)  # 特殊トークンの除去
    source = re.sub(r"^\s*%%\{init.*?\}%%\s*", "", source, flags=re.DOTALL)  # 独自の init は使わない
    return source.strip() + "\n"


def render_mermaid_local(source: str, mmdc: str, scale: int) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "in.mmd").write_text(source, encoding="utf-8")
        (tmp / "config.json").write_text(json.dumps(MERMAID_CONFIG, ensure_ascii=False), encoding="utf-8")
        cmd = [mmdc, "-q", "-i", str(tmp / "in.mmd"), "-o", str(tmp / "out.png"),
               "-c", str(tmp / "config.json"), "-b", "white", "-s", str(scale)]
        puppeteer_cfg = MERMAID_DIR / "puppeteer.json"
        if puppeteer_cfg.exists():
            cmd += ["-p", str(puppeteer_cfg)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = tmp / "out.png"
        if r.returncode != 0 or not out.exists():
            msg = (r.stderr or r.stdout).strip().splitlines()
            raise RuntimeError("mermaid-cli 失敗: " + (msg[0] if msg else f"exit {r.returncode}"))
        return out.read_bytes()


def render_mermaid_ink(source: str, retries: int = 3) -> bytes:
    init = json.dumps(MERMAID_CONFIG, ensure_ascii=False)
    full = f"%%{{init: {init}}}%%\n{source}"
    encoded = base64.urlsafe_b64encode(full.encode("utf-8")).decode("ascii")
    url = f"https://mermaid.ink/img/{encoded}?type=png&bgColor=FFFFFF&width=2000"

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


def fit_to_zone(img_w: int, img_h: int, max_w: float, max_h: float,
                font_in_image_px: float | None, max_font_px: float) -> tuple[float, float, float | None]:
    """図をゾーンに収めたときの (幅in, 高さin, 動画上の文字px) を返す。
    文字の大きさが分かるときは、max_font_px を超えないように拡大を抑える。"""
    k = min(max_w * PX_PER_INCH / img_w, max_h * PX_PER_INCH / img_h)  # 画像px → 動画px
    font_px = None
    if font_in_image_px:
        font_px = font_in_image_px * k
        if font_px > max_font_px:
            k *= max_font_px / font_px
            font_px = max_font_px
    return img_w * k / PX_PER_INCH, img_h * k / PX_PER_INCH, font_px


class DiagramRenderer:
    """図を描いて、ゾーンに収まる最も読みやすい画像を選ぶ。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mmdc = find_mmdc() if cfg["renderer"] == "local" else None
        if cfg["renderer"] == "local" and not self.mmdc:
            print("[warn] mermaid-cli が見つからないため mermaid.ink で描画します"
                  "（tools/video/mermaid で npm install を実行してください）", file=sys.stderr)
        print(f"[info] 図の描画: {'mermaid-cli (' + self.mmdc + ')' if self.mmdc else 'mermaid.ink'}", file=sys.stderr)

    def _render(self, source: str) -> tuple[Image.Image, float | None]:
        if self.mmdc:
            scale = int(self.cfg["render_scale"])
            png = render_mermaid_local(source, self.mmdc, scale)
            return to_line_art(png), MERMAID_BASE_FONT_PX * scale
        return to_line_art(render_mermaid_ink(source)), None  # mermaid.ink は倍率が不明

    def render(self, source: str, kind: str, max_w: float, max_h: float):
        """戻り値: (画像, 幅in, 高さin, 文字px, 向き)。使えないときは理由付きで例外。"""
        source = clean_source(source)
        best = None
        for d in DIRECTIONS.get(kind, [None]):
            src = set_direction(source, kind, d) if d else source
            img, font_in_image = self._render(src)
            w_in, h_in, font_px = fit_to_zone(*img.size, max_w, max_h, font_in_image, self.cfg["max_font_px"])
            score = font_px if font_px is not None else w_in * h_in
            if best is None or score > best[0]:
                best = (score, img, w_in, h_in, font_px, d)
            if font_px is not None and font_px >= self.cfg["max_font_px"]:
                break  # これ以上は大きくならない
        _, img, w_in, h_in, font_px, d = best
        if font_px is not None and font_px < self.cfg["min_font_px"]:
            raise ValueError(f"文字が小さすぎる（{font_px:.1f}px < {self.cfg['min_font_px']}px）")
        if font_px is None and is_extreme_aspect(img.width / img.height):
            raise ValueError(f"縦横比が極端（{img.width / img.height:.2f}）")
        return img, w_in, h_in, font_px, d


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
        
        set_title(slide, s.get("title", ""), is_cover=is_cover)
        set_notes(slide, s.get("notes", ""))

        # 図解のレンダリングと配置
        diagram = s.get("diagram")
        has_image = False
        
        if diagram and diagram.get("type", "mermaid") == "mermaid" and diagram.get("source"):
            kind = detect_diagram_kind(diagram["source"])
            if is_hero:
                # 字幕と被らないよう、高さを4.2インチに制限
                max_w, max_h = 12.1, 4.2
                zone_left, zone_top = 0.6, 1.5
            else:
                max_w, max_h = 6.0, 4.5
                zone_left, zone_top = 6.8, 1.8
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
