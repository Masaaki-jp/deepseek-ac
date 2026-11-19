#!/usr/bin/env python3
"""Mermaid の図を描いて、スライド上での読みやすさを測る。

build_pptx.py（スライドに貼る）と refine.py（DeepSeek に直させる）の両方から使う。
描画は決まった処理なので、合否の判定はここ（コード）で行い、DeepSeek には判定結果だけを返す。
"""
import base64
import copy
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.video.schema import detect_diagram_kind  # noqa: E402


def is_extreme_aspect(aspect: float, threshold: float = 2.4) -> bool:
    return aspect > threshold or aspect < (1.0 / threshold)


# スライド上で図を置ける領域（インチ）。字幕と被らない高さにしてある
ZONES = {
    "hero": {"max_w": 12.1, "max_h": 4.2, "left": 0.6, "top": 1.5},    # 図が主役
    "split": {"max_w": 6.0, "max_h": 4.5, "left": 6.8, "top": 1.8},    # 左に箇条書き、右に図
}


# 白黒の線画で描き、あとで「白い線・透明な背景」に変換する
_W, _B = "#ffffff", "#000000"
MERMAID_CONFIG = {
    "theme": "base",
    "fontSize": 18,  # シーケンス図の文字はこちらで決まる
    # useMaxWidth: False … 幅800pxを超える図が縮められ、文字の大きさの見積もりがずれるのを防ぐ
    "flowchart": {"curve": "basis", "useMaxWidth": False},
    # 下側の登場人物の箱を省略し、文字サイズをほかの図とそろえる
    "sequence": {"mirrorActors": False, "actorFontSize": 18, "messageFontSize": 18, "noteFontSize": 18,
                 "useMaxWidth": False},
    "state": {"useMaxWidth": False},
    "themeVariables": {
        "fontFamily": '"Noto Sans CJK JP", "Noto Sans JP", "Hiragino Kaku Gothic ProN", "Meiryo", sans-serif',
        "fontSize": "18px",
        "background": _W, "mainBkg": _W, "textColor": _B, "lineColor": _B,
        "primaryColor": _W, "primaryTextColor": _B, "primaryBorderColor": _B,
        "secondaryColor": _W, "tertiaryColor": _W, "nodeBorder": _B,
        "clusterBkg": _W, "clusterBorder": "#999999",  # グループの枠は控えめに "edgeLabelBackground": _W,
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
    # シーケンス図で、メッセージの文字の上を縦線が貫かないよう背景色で縁取る
    "themeCSS": ".messageText { stroke: #ffffff; stroke-width: 10px; paint-order: stroke fill; stroke-linejoin: round; }",
}
# ------------------------------------------
# 色付きで描くときの設定（subgraph のあるフローチャートと、強調表示だけ）
# 白一色の設定の「白→透明、黒→白」に置き換え、暗い背景の上に透明な背景で直接描く
# ------------------------------------------
def _dark_config() -> dict:
    cfg = copy.deepcopy(MERMAID_CONFIG)
    tv = cfg["themeVariables"]
    for k, v in list(tv.items()):
        if v == _W:
            tv[k] = "transparent"
        elif v == _B:
            tv[k] = _W
    tv.update({"background": "transparent", "clusterBkg": "transparent", "clusterBorder": "#787c99",
               "edgeLabelBackground": "transparent"})
    cfg["themeCSS"] = (".messageText { stroke: #1a1b26; stroke-width: 10px; paint-order: stroke fill; }"
                       " .flowchart-link { stroke-width: 1.5px; }")
    return cfg


DARK_CONFIG = _dark_config()
GROUP_PALETTE = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#7dcfff"]  # 青・緑・橙・紫・水色
FOCUS_COLOR = "#7aa2f7"
DIM_COLOR, DIM_TEXT = "#6b7394", "#9aa5ce"  # 強調しない部分。小さく描いても形がわかる明るさ

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
    "min_fill_ratio": 0.25,  # 図が置き場所の面積をこの割合以上使っていないと不合格（refine.py で使う）
    "max_elements": {"flowchart": 8, "sequence": 10, "state": 7},  # これを超えると複雑すぎて不合格
    "kinds": ["flowchart", "sequence", "state"],
    "color_groups": True,  # subgraph のある図をグループごとに色分けする
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


# 向きを選び直せる図の種類と、試す向き
DIRECTIONS = {"flowchart": ["TD", "LR"], "state": ["TB", "LR"]}


def directions_to_try(source: str, kind: str) -> list:
    """書いた人（DeepSeek）の向きを先に試す。読みやすさが同じなら、書いた向きが残る。"""
    options = DIRECTIONS.get(kind)
    if not options:
        return [None]
    if kind == "flowchart":
        m = re.search(r"^\s*(?:flowchart|graph)\s+(TD|TB|LR|RL|BT)", source, flags=re.MULTILINE)
        written = {"TB": "TD", "BT": "TD", "RL": "LR"}.get(m.group(1), m.group(1)) if m else "TD"
    else:
        m = re.search(r"^\s*direction\s+(TB|TD|LR|RL|BT)\s*$", source, flags=re.MULTILINE)
        written = {"TD": "TB", "BT": "TB", "RL": "LR"}.get(m.group(1), m.group(1)) if m else "TB"
    return [written] + [d for d in options if d != written]


def clean_source(source: str) -> str:
    source = re.sub(r"<\|.*?\|>", "", source)  # 特殊トークンの除去
    source = re.sub(r"^\s*%%\{init.*?\}%%\s*", "", source, flags=re.DOTALL)  # 独自の init は使わない
    # 見た目はシステム側で統一するので、図に書かれた配色の指定は外す
    source = "\n".join(ln for ln in source.splitlines()
                       if not re.match(r"^\s*(style|classDef|class|linkStyle)\b", ln))
    return source.strip() + "\n"


def render_mermaid_local(source: str, mmdc: str, scale: int,
                         config: dict | None = None, background: str = "white") -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "in.mmd").write_text(source, encoding="utf-8")
        (tmp / "config.json").write_text(json.dumps(config or MERMAID_CONFIG, ensure_ascii=False), encoding="utf-8")
        cmd = [mmdc, "-q", "-i", str(tmp / "in.mmd"), "-o", str(tmp / "out.png"),
               "-c", str(tmp / "config.json"), "-b", background, "-s", str(scale)]
        puppeteer_cfg = MERMAID_DIR / "puppeteer.json"
        if puppeteer_cfg.exists():
            cmd += ["-p", str(puppeteer_cfg)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = tmp / "out.png"
        if r.returncode != 0 or not out.exists():
            # 「Parse error on line 2:」の後ろに続く、該当行と期待されたトークンまで返す
            msg = [ln for ln in (r.stderr or r.stdout).strip().splitlines() if ln.strip()][:4]
            raise RuntimeError("mermaid-cli 失敗: " + (" / ".join(msg) if msg else f"exit {r.returncode}"))
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


# ------------------------------------------
# 図の中身を数える（複雑すぎる図を見つけ、分割したときの取りこぼしを確かめる）
# ------------------------------------------
_FLOW_SKIP = re.compile(r"^\s*(%%|(flowchart|graph|subgraph|end|direction|classDef|class|style|linkStyle|click)\b)")
_SEQ_MESSAGE = re.compile(r"^\s*[A-Za-z_][\w]*\s*(-->>|->>|-->|->|--x|-x|--\)|-\))\s*[A-Za-z_][\w]*\s*:")
_NODE_LABEL = re.compile(r"\b([A-Za-z_][\w]*)\s*(?:\[\[|\[\(|\(\(|\[|\(|\{\{|\{|>)\s*\"?([^\]\)\}\"]+?)\"?\s*(?:\]\]|\)\]|\)\)|\]|\)|\}\}|\})")


def _flow_lines(source: str):
    for line in source.splitlines():
        if line.strip() and not _FLOW_SKIP.match(line):
            yield line


def flowchart_nodes(source: str) -> dict[str, str]:
    """flowchart のノードID → ラベル（ラベルがなければID）。subgraph のIDは含めない。"""
    groups = set(re.findall(r"^\s*subgraph\s+([A-Za-z_][\w]*)", source, flags=re.MULTILINE))
    nodes: dict[str, str] = {}
    for line in _flow_lines(source):
        for node_id, label in _NODE_LABEL.findall(line):
            nodes[node_id] = label.strip()
        # ラベル・矢印上の文字を消してから、残ったIDを拾う
        bare = _NODE_LABEL.sub(lambda m: m.group(1), line)
        bare = re.sub(r"\|[^|]*\|", " ", bare)
        bare = re.sub(r"(--|==|-\.)\s*[^->=.]+?\s*(-->|==>|\.->)", " --> ", bare)
        for node_id in re.findall(r"\b([A-Za-z_][\w]*)\b", bare):
            if node_id not in groups:
                nodes.setdefault(node_id, node_id)
    return nodes


def diagram_labels(source: str) -> set[str]:
    """図に登場する要素の名前（ノードのラベル、登場者、状態）。分割時の取りこぼし確認に使う。"""
    kind = detect_diagram_kind(source or "")
    if kind == "flowchart":
        return {v for v in flowchart_nodes(source).values()}
    if kind == "sequence":
        named = re.findall(r"^\s*(?:participant|actor)\s+\w+\s+as\s+(.+?)\s*$", source, flags=re.MULTILINE)
        return set(named)
    if kind == "state":
        return set(re.findall(r'^\s*state\s+"([^"]+)"\s+as\s+\w+', source, flags=re.MULTILINE))
    return set()


def count_elements(source: str) -> tuple[str, int]:
    """図の複雑さ（数えたもの, 個数）。"""
    kind = detect_diagram_kind(source or "")
    if kind == "flowchart":
        return "ノード", len(flowchart_nodes(source))
    if kind == "sequence":
        return "メッセージ", sum(1 for ln in source.splitlines() if _SEQ_MESSAGE.match(ln))
    if kind == "state":
        ids = set()
        for a, b in re.findall(r"^\s*(\[\*\]|\w+)\s*-->\s*(\[\*\]|\w+)", source, flags=re.MULTILINE):
            ids.update(x for x in (a, b) if x != "[*]")
        return "状態", len(ids)
    return "要素", 0


def subgraph_info(source: str) -> dict[str, dict]:
    """subgraph のID → {"title": 見出し, "members": 中のノードID}（入れ子の subgraph は外側に含めない）。"""
    groups: dict[str, dict] = {}
    stack: list[str] = []
    for line in source.splitlines():
        m = re.match(r'^\s*subgraph\s+([A-Za-z_][\w]*)\s*(?:\[\s*"?(.*?)"?\s*\])?\s*$', line)
        if m:
            stack.append(m.group(1))
            title = m.group(2) if m.group(2) is not None else m.group(1)
            groups[m.group(1)] = {"title": title.strip(), "members": []}
            continue
        if re.match(r"^\s*end\s*$", line):
            if stack:
                stack.pop()
            continue
        if stack and not _FLOW_SKIP.match(line):
            members = groups[stack[-1]]["members"]
            for i in flowchart_nodes("flowchart TD\n" + line):
                if i not in groups and i not in members:
                    members.append(i)
    return groups


def named_groups(source: str) -> dict[str, list[str]]:
    """見出しのある subgraph だけ（見出しが空白の枠は、2段に折り返すための並べ方なので除く）。"""
    return {g: v["members"] for g, v in subgraph_info(source).items() if v["title"]}


def decorate(source: str, focus: str | None = None) -> str:
    """色の指定を足す。focus なし: グループごとに色分け。
    focus あり（グループかノードのID）: そこだけ青く、ほかを暗くする。"""
    info = subgraph_info(source)
    groups = {g: v["members"] for g, v in info.items()}
    named = named_groups(source)
    nodes = list(flowchart_nodes(source))
    lines: list[str] = []
    for g in groups:
        if g not in named:  # 並べ方のための枠は控えめな線にする
            lines.append(f"style {g} fill:transparent,stroke:{DIM_COLOR},stroke-width:1px,color:{DIM_COLOR}")
    if focus is None or (focus not in groups and focus not in nodes):
        for i, (g, members) in enumerate(named.items()):
            c = GROUP_PALETTE[i % len(GROUP_PALETTE)]
            lines.append(f"style {g} fill:transparent,stroke:{c},stroke-width:2px,color:{c}")
            if members:
                lines.append(f"classDef grp{i} stroke:{c},stroke-width:1.5px")
                lines.append(f"class {','.join(members)} grp{i}")
    else:
        lit = set(groups[focus]) if focus in groups else {focus}
        for g in named:
            if g == focus:
                lines.append(f"style {g} fill:{FOCUS_COLOR}33,stroke:{FOCUS_COLOR},stroke-width:3px,color:{FOCUS_COLOR}")
            else:
                lines.append(f"style {g} fill:transparent,stroke:{DIM_COLOR},stroke-width:1px,color:{DIM_COLOR}")
        if focus not in groups:
            lines.append(f"classDef focus fill:{FOCUS_COLOR}33,stroke:{FOCUS_COLOR},stroke-width:3px")
            lines.append(f"class {focus} focus")
        dim = [n for n in nodes if n not in lit]
        if dim:
            lines.append(f"classDef dim stroke:{DIM_COLOR},color:{DIM_TEXT}")
            lines.append(f"class {','.join(dim)} dim")
    return source.rstrip() + "\n" + "\n".join(lines) + "\n"


def trim_transparent(png_bytes: bytes) -> Image.Image:
    with Image.open(io.BytesIO(png_bytes)) as src:
        img = src.convert("RGBA")
    bbox = img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    return img.crop(bbox) if bbox else img


class DiagramRenderer:
    """図を描いて、ゾーンに収まる最も読みやすい向きを選ぶ。"""

    def __init__(self, cfg: dict, quiet: bool = False):
        self.cfg = cfg
        self.mmdc = find_mmdc() if cfg["renderer"] == "local" else None
        if cfg["renderer"] == "local" and not self.mmdc:
            print("[warn] mermaid-cli が見つからないため mermaid.ink で描画します"
                  "（tools/video/mermaid で npm install を実行してください）", file=sys.stderr)
        if not quiet:
            print(f"[info] 図の描画: {'mermaid-cli (' + self.mmdc + ')' if self.mmdc else 'mermaid.ink'}",
                  file=sys.stderr)

    def use_color(self, source: str, kind: str | None, focus: str | None) -> bool:
        """色付きで描くのは、subgraph のあるフローチャートか、強調を指定したときだけ。"""
        if not (self.mmdc and self.cfg.get("color_groups", True) and kind == "flowchart"):
            return False
        return bool(focus) or bool(named_groups(source))

    def _render(self, source: str, kind: str | None = None,
                focus: str | None = None) -> tuple[Image.Image, float | None]:
        if self.mmdc:
            scale = int(self.cfg["render_scale"])
            if self.use_color(source, kind, focus):
                png = render_mermaid_local(decorate(source, focus), self.mmdc, scale,
                                           config=DARK_CONFIG, background="transparent")
                return trim_transparent(png), MERMAID_BASE_FONT_PX * scale
            png = render_mermaid_local(source, self.mmdc, scale)
            return to_line_art(png), MERMAID_BASE_FONT_PX * scale
        return to_line_art(render_mermaid_ink(source)), None  # mermaid.ink は倍率が不明（色なし）

    def render_best(self, source: str, kind: str, max_w: float, max_h: float, focus: str | None = None):
        """向きを変えて描き比べ、最も文字が大きくなるものを返す（合否は判定しない）。
        戻り値: (画像, 幅in, 高さin, 文字px, 向き)"""
        source = clean_source(source)
        best = None
        for d in directions_to_try(source, kind):
            src = set_direction(source, kind, d) if d else source
            img, font_in_image = self._render(src, kind, focus)
            w_in, h_in, font_px = fit_to_zone(*img.size, max_w, max_h, font_in_image, self.cfg["max_font_px"])
            score = font_px if font_px is not None else w_in * h_in
            if best is None or score > best[0]:
                best = (score, img, w_in, h_in, font_px, d)
            if font_px is not None and font_px >= self.cfg["max_font_px"]:
                break  # これ以上は大きくならない
        return best[1:]

    def render(self, source: str, kind: str, max_w: float, max_h: float):
        """スライドに貼る用。文字が小さすぎるなど、使えないときは理由付きで例外。"""
        img, w_in, h_in, font_px, d = self.render_best(source, kind, max_w, max_h)
        if font_px is not None and font_px < self.cfg["min_font_px"]:
            raise ValueError(f"文字が小さすぎる（{font_px:.1f}px < {self.cfg['min_font_px']}px）")
        if font_px is None and is_extreme_aspect(img.width / img.height):
            raise ValueError(f"縦横比が極端（{img.width / img.height:.2f}）")
        return img, w_in, h_in, font_px, d

    def measure(self, source: str, zone_name: str = "hero") -> dict:
        """図を描いて、合否とその理由・直し方のヒントを返す。DeepSeek にそのまま渡す想定。"""
        zone = ZONES[zone_name]
        cfg = self.cfg
        kind = detect_diagram_kind(source or "")
        result = {"verdict": "fail", "kind": kind, "problems": [], "hints": []}

        if kind not in cfg["kinds"]:
            result["problems"].append(f"使えない図の種類です（{kind or '判定できない'}）")
            result["hints"].append(f"1行目を {' / '.join(cfg['kinds'])} のいずれかの宣言にしてください"
                                   "（flowchart TD, sequenceDiagram, stateDiagram-v2）")
            return result
        try:
            img, w_in, h_in, font_px, direction = self.render_best(source, kind, zone["max_w"], zone["max_h"])
        except Exception as e:
            result["problems"].append(f"描画に失敗しました: {str(e)[:300]}")
            result["hints"].append("Mermaid の文法を見直してください。ラベルに半角の括弧・引用符・セミコロンがあると壊れます")
            return result

        w_use, h_use = w_in / zone["max_w"], h_in / zone["max_h"]
        fill = w_use * h_use
        result.update({
            "direction": direction,
            "font_px": round(font_px, 1) if font_px is not None else None,
            "width_use": round(w_use, 2),
            "height_use": round(h_use, 2),
            "fill_ratio": round(fill, 2),
            "limits": {"min_font_px": cfg["min_font_px"], "min_fill_ratio": cfg["min_fill_ratio"]},
        })

        unit, count = count_elements(source)
        max_count = (cfg.get("max_elements") or {}).get(kind)
        result["elements"] = {"unit": unit, "count": count, "max": max_count}
        too_complex = max_count is not None and count > max_count
        too_small = font_px is not None and font_px < cfg["min_font_px"]
        if too_complex:
            result["problems"].append(f"{unit}が多すぎます（{count}個、{max_count}個まで）")
        if too_small:
            result["problems"].append(f"文字が小さすぎます（{font_px:.1f}px、{cfg['min_font_px']}px以上が必要）")
        if too_complex or too_small:
            result["hints"].append("全体図と分解図に分けてください（check_split で確かめてから submit_split）。"
                                   "細部がそれほど重要でなければ、要素を減らして1枚に収めても構いません")
        # 文字がすでに上限まで大きいなら、要素が少ないだけなので画面が余っても問題にしない
        font_capped = font_px is not None and font_px >= cfg["max_font_px"] - 0.5
        if fill < cfg["min_fill_ratio"] and not font_capped:
            result["problems"].append(f"画面の使い方が小さすぎます（面積の{fill:.0%}、{cfg['min_fill_ratio']:.0%}以上が必要）")
            if w_use > 0.9 and h_use < 0.4:
                result["hints"].append(
                    "横一列に細長くなっています。flowchart なら subgraph を2つ作って各段を direction LR にし、"
                    "subgraph どうしを矢印でつないで2段に折り返してください")
            elif h_use > 0.9 and w_use < 0.4:
                result["hints"].append("縦に細長くなっています。分岐を作って横に広げるか、2列に分けてください")
            else:
                result["hints"].append("要素が少なすぎて図が小さくなっています。図にせず箇条書きにする（submit で source を空にする）方がよいかもしれません")

        if not result["problems"]:
            result["verdict"] = "pass"
        return result
