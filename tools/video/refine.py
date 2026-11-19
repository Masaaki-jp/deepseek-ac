#!/usr/bin/env python3
"""deck_ir.json の図のうち、判定で不合格になったものを DeepSeek に直させる。

DeepSeek のツール呼び出しで、小さなエージェントのループを回す。
  - render_diagram : 図を描いて、合否・問題点・直し方のヒントを返す（判定はコードが行う）
  - submit_diagram : 最終的な図を提出する（空文字なら「図にしない」）

安全装置:
  - 1枚あたりの描画回数（max_rounds）と、実行全体の API 呼び出し回数（max_api_calls）に上限
  - 呼ぶ前に tools/usage.py の予算チェック。超えたら残りは直さずに終える
  - 提出された図はコードで描き直して判定し、不合格なら採用しない
  - どこで失敗しても、元の図をそのまま残す（build_pptx.py が従来どおり扱う）

使い方:
  python tools/video/refine.py output/<slug>/deck_ir.json            # 不合格の図を直す
  python tools/video/refine.py output/<slug>/deck_ir.json --dry-run  # 判定だけ（API を呼ばない）
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.video.deepseek_api import BudgetExceeded, DeepSeekError, chat  # noqa: E402
from tools.video.diagram_render import (  # noqa: E402
    DiagramRenderer, diagram_labels, flowchart_nodes, load_diagram_config)
from tools.video.schema import detect_diagram_kind  # noqa: E402
from tools.video.schema import DeckIR  # noqa: E402

TOOLS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOLS_DIR / "config.yaml"
PROMPT_PATH = TOOLS_DIR / "prompts" / "refine.txt"

DEFAULT_REFINE_CFG = {
    "max_rounds": 3,       # 1枚の図につき、render_diagram を呼べる回数
    "max_api_calls": 30,   # 1回の実行で DeepSeek を呼ぶ回数の上限（全スライド合計）
    "max_tokens": 2000,
    "temperature": 0.2,
    "timeout": 120,
    "max_retries": 3,
}

_SPLIT_PARAMS = {
    "type": "object",
    "properties": {
        "overview": {
            "type": "object",
            "description": "全体図。各グループを1つのノードにまとめ、グループどうしのつながりだけを描く",
            "properties": {
                "source": {"type": "string", "description": "全体図の Mermaid ソース"},
                "narration": {"type": "array", "items": {"type": "string"},
                              "description": "全体図のナレーション（2〜4文、1文40文字以内）"},
            },
            "required": ["source", "narration"],
        },
        "parts": {
            "type": "array",
            "description": "分解図（2〜4枚）。元の図の要素は、どれか1枚に必ず入れる",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "スライドのタイトル（20文字以内）"},
                    "focus": {"type": "string",
                              "description": "全体図の中で、この分解図にあたるノードのID（例: G1）。右上のミニマップで強調する"},
                    "source": {"type": "string", "description": "分解図の Mermaid ソース"},
                    "bullets": {"type": "array", "items": {"type": "string"},
                                "description": "図が出せないときの代わりの要点（2〜3項目）"},
                    "narration": {"type": "array", "items": {"type": "string"},
                                  "description": "ナレーション（2〜4文、1文40文字以内）"},
                },
                "required": ["title", "focus", "source", "bullets", "narration"],
            },
        },
    },
    "required": ["overview", "parts"],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "render_diagram",
            "description": "Mermaid のソースを描画して、スライド上での読みやすさを判定する。"
                           "合否（verdict）、問題点（problems）、直し方のヒント（hints）、文字の大きさ、画面の使用率を返す。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Mermaid のソース。1行目は図の種類の宣言"},
                },
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_diagram",
            "description": "最終的な図を提出して作業を終える。図にしない方がよい場合は source を空文字にする。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "提出する Mermaid のソース。図にしないなら空文字"},
                    "reason": {"type": "string", "description": "どう直したか、または図にしない理由（40文字以内）"},
                },
                "required": ["source", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_split",
            "description": "全体図＋分解図への分割案を確かめる。各図の判定と、元の図の要素の取りこぼし、"
                           "ナレーションの形式を調べて返す（描画の回数に数える）。",
            "parameters": _SPLIT_PARAMS,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_split",
            "description": "全体図＋分解図への分割案を提出して作業を終える。元のスライドが全体図になり、"
                           "その後ろに分解図のスライドが入る。",
            "parameters": _SPLIT_PARAMS,
        },
    },
]

SPLIT_MIN_PARTS, SPLIT_MAX_PARTS = 2, 4
MAX_SENTENCE_CHARS = 50  # ナレーション1文の上限（プロンプトでは40文字と伝え、少し余裕を持たせる）


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    refine = dict(DEFAULT_REFINE_CFG)
    refine.update(cfg.get("refine") or {})
    cfg["refine"] = refine
    return cfg


def zone_for(slide: dict) -> str:
    return "hero" if slide.get("layout") == "diagram_hero" else "split"


def score(m: dict) -> tuple:
    """合格した候補どうしを比べる（画面を広く使い、文字が大きい方がよい）。"""
    return (m.get("fill_ratio") or 0, m.get("font_px") or 0)


class Session:
    """実行全体で共有する呼び出し回数とコスト。"""

    def __init__(self, cfg: dict, api_key: str, slug: str):
        self.cfg = cfg
        self.api_key = api_key
        self.slug = slug
        self.api_calls = 0
        self.cost_usd = 0.0
        self.stopped: str | None = None  # 予算切れなどで止めた理由

    def call(self, messages: list[dict], slide_no: int) -> dict:
        rcfg = self.cfg["refine"]
        if self.api_calls >= rcfg["max_api_calls"]:
            raise BudgetExceeded(f"API 呼び出しの上限（{rcfg['max_api_calls']}回）に達しました")
        self.api_calls += 1
        data, cost = chat(
            messages,
            model=self.cfg["model"],
            api_key=self.api_key,
            tools=TOOLS,
            max_tokens=rcfg["max_tokens"],
            temperature=rcfg["temperature"],
            timeout=rcfg["timeout"],
            max_retries=rcfg["max_retries"],
            metadata={"tool": "refine", "slug": self.slug, "slide": slide_no},
        )
        if cost:
            self.cost_usd += cost["cost_usd"]
        return data["choices"][0]["message"]


def _check_narration(where: str, narration, problems: list[str]) -> None:
    if not isinstance(narration, list) or not narration:
        problems.append(f"{where}: narration が空です")
        return
    if not 2 <= len(narration) <= 4:
        problems.append(f"{where}: narration は2〜4文にしてください（{len(narration)}文）")
    long = [n for n in narration if len(str(n)) > MAX_SENTENCE_CHARS]
    if long:
        problems.append(f"{where}: 長すぎる文があります（{len(long)}文、1文40文字以内）")


def check_split(args: dict, original: str, renderer: DiagramRenderer, zone: str) -> dict:
    """分割案をコードで確かめる。DeepSeek にそのまま返す。"""
    problems: list[str] = []
    overview = args.get("overview") or {}
    parts = args.get("parts") or []
    result = {"verdict": "fail", "problems": problems}

    if not SPLIT_MIN_PARTS <= len(parts) <= SPLIT_MAX_PARTS:
        problems.append(f"分解図は{SPLIT_MIN_PARTS}〜{SPLIT_MAX_PARTS}枚にしてください（{len(parts)}枚）")

    ov = renderer.measure(overview.get("source", ""), zone)
    result["overview"] = ov
    if ov["verdict"] != "pass":
        problems.append("全体図: " + "; ".join(ov["problems"]))
    _check_narration("全体図", overview.get("narration"), problems)

    ov_src = overview.get("source", "")
    ov_nodes = set(flowchart_nodes(ov_src)) if detect_diagram_kind(ov_src) == "flowchart" else None
    covered: set[str] = set()
    result["parts"] = []
    for k, part in enumerate(parts, 1):
        m = renderer.measure(part.get("source", ""), "hero")
        result["parts"].append({"title": part.get("title", ""), **m})
        if m["verdict"] != "pass":
            problems.append(f"分解図{k}: " + "; ".join(m["problems"]))
        title = (part.get("title") or "").strip()
        if not title or len(title) > 24:
            problems.append(f"分解図{k}: title は1〜20文字にしてください")
        if not part.get("bullets"):
            problems.append(f"分解図{k}: bullets が空です")
        _check_narration(f"分解図{k}", part.get("narration"), problems)
        if ov_nodes is not None and part.get("focus") not in ov_nodes:
            problems.append(f"分解図{k}: focus には全体図のノードID（{', '.join(sorted(ov_nodes))} のどれか）を指定してください")
        covered |= diagram_labels(part.get("source", ""))

    missing = sorted(diagram_labels(original) - covered)
    if missing:
        problems.append("元の図の要素が分解図に入っていません: " + "、".join(missing))
    result["missing"] = missing

    if not problems:
        result["verdict"] = "pass"
    return result


def split_to_slides(slide: dict, args: dict) -> tuple[dict, list[dict]]:
    """合格した分割案から、全体図のスライド（元のスライドを置き換え）と分解図のスライドを作る。"""
    overview = dict(slide)
    overview["diagram"] = dict(slide["diagram"], source=args["overview"]["source"],
                               reason=(slide["diagram"].get("reason") or "") + "（全体図）")
    overview["layout"] = "diagram_hero"
    overview["narration"] = args["overview"]["narration"]
    parts = []
    n = len(args["parts"])
    for k, part in enumerate(args["parts"], 1):
        parts.append({
            "layout": "diagram_hero",
            "title": part["title"],
            "bullets": part["bullets"],
            "narration": part["narration"],
            "notes": "",
            "columns": [],
            "diagram": {"type": "mermaid", "source": part["source"], "reason": f"分解図 {k}/{n}",
                        "overview": args["overview"]["source"], "focus": part.get("focus")},
        })
    return overview, parts


def build_user_message(slide: dict, source: str, measured: dict) -> str:
    return "\n".join([
        f"# スライドのタイトル\n{slide.get('title', '')}",
        "# 要点\n" + "\n".join(f"- {b}" for b in slide.get("bullets", [])),
        "# ナレーション\n" + "".join(slide.get("narration", [])),
        f"# 今の図\n```mermaid\n{source}\n```",
        "# 今の図の判定結果\n" + json.dumps(measured, ensure_ascii=False, indent=2),
    ])


def refine_slide(slide: dict, slide_no: int, renderer: DiagramRenderer, session: Session,
                 system_prompt: str) -> dict:
    """1枚の図を直す。戻り値はログ用の記録。slide は必要に応じて書き換える。"""
    rcfg = session.cfg["refine"]
    zone = zone_for(slide)
    original = slide["diagram"]["source"]
    before = renderer.measure(original, zone)
    log = {"slide": slide_no, "title": slide.get("title", ""), "before": before,
           "renders": 0, "decision": "keep", "note": ""}

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_user_message(slide, original, before)},
    ]
    best: tuple[str, dict] | None = None   # 合格した候補のうち最良のもの
    submitted: tuple[str, str] | None = None
    split_submitted: dict | None = None

    # render の上限 + 提出 + 指示に従わなかったときの催促1回 を、この図の呼び出し上限にする
    for _ in range(rcfg["max_rounds"] + 2):
        msg = session.call(messages, slide_no)
        tool_calls = msg.get("tool_calls") or []
        messages.append({k: v for k, v in msg.items() if v is not None})

        if not tool_calls:
            messages.append({"role": "user", "content": "ツールを使ってください。作業を終えるときは submit_diagram を呼んでください。"})
            continue

        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError as e:
                result = {"error": f"arguments が JSON として読めません: {e}"}
            else:
                if name == "render_diagram":
                    if log["renders"] >= rcfg["max_rounds"]:
                        result = {"error": "render_diagram の上限に達しました。submit_diagram で提出してください。"}
                    else:
                        log["renders"] += 1
                        src = args.get("source", "")
                        result = renderer.measure(src, zone)
                        mark = "合格" if result["verdict"] == "pass" else "不合格"
                        print(f"    描画 {log['renders']}/{rcfg['max_rounds']}: {mark} "
                              f"{'; '.join(result['problems'])}", file=sys.stderr)
                        if result["verdict"] == "pass" and (best is None or score(result) > score(best[1])):
                            best = (src, result)
                elif name == "check_split":
                    if log["renders"] >= rcfg["max_rounds"]:
                        result = {"error": "確認の上限に達しました。submit_split か submit_diagram で提出してください。"}
                    else:
                        log["renders"] += 1
                        result = check_split(args, original, renderer, zone)
                        mark = "合格" if result["verdict"] == "pass" else "不合格"
                        print(f"    分割の確認 {log['renders']}/{rcfg['max_rounds']}: {mark} "
                              f"{'; '.join(result['problems'])[:200]}", file=sys.stderr)
                elif name == "submit_diagram":
                    submitted = (args.get("source", ""), args.get("reason", ""))
                    result = {"status": "received"}
                elif name == "submit_split":
                    split_submitted = args
                    result = {"status": "received"}
                else:
                    result = {"error": f"知らないツールです: {name}"}
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, ensure_ascii=False)})
        if submitted or split_submitted:
            break

    # 採用するかどうかは、コードが判定し直して決める
    if split_submitted is not None:
        checked = check_split(split_submitted, original, renderer, zone)
        if checked["verdict"] == "pass":
            overview, parts = split_to_slides(slide, split_submitted)
            slide.clear()
            slide.update(overview)
            log["decision"] = "split"
            log["note"] = f"全体図＋分解図{len(parts)}枚に分けた"
            log["new_slides"] = parts
            return log
        log["note"] = f"分割案が不合格だったため採用しませんでした（{'; '.join(checked['problems'])[:200]}）"
        # 1枚の図で合格した候補があればそちらを使う。なければ元の図を残す
    if submitted is not None:
        src, reason = submitted
        log["note"] = reason
        if not src.strip():
            slide["diagram"] = None
            log["decision"] = "drop"
            return log
        after = renderer.measure(src, zone)
        if after["verdict"] == "pass":
            best = (src, after)
        elif best is None:
            log["decision"] = "keep"
            log["note"] = f"提出された図が不合格だったため元の図を残しました（{'; '.join(after['problems'])}）"
            return log
    if best is None:
        log["note"] = log["note"] or "合格する図が得られなかったため元の図を残しました"
        return log

    src, after = best
    slide["diagram"]["source"] = src
    slide["diagram"]["reason"] = (slide["diagram"].get("reason") or "") + f"（改善: {log['note'] or '判定に合格'}）"
    log["decision"] = "replace"
    log["after"] = after
    return log


def main():
    parser = argparse.ArgumentParser(description="不合格の図を DeepSeek に直させる")
    parser.add_argument("ir", help="deck_ir.json のパス")
    parser.add_argument("--dry-run", action="store_true", help="判定だけ行い、DeepSeek を呼ばない")
    args = parser.parse_args()

    ir_path = Path(args.ir).resolve()
    deck = json.loads(ir_path.read_text(encoding="utf-8"))
    cfg = load_config()
    renderer = DiagramRenderer(load_diagram_config())

    targets = []
    for i, s in enumerate(deck["slides"], 1):
        d = s.get("diagram")
        if not d or not d.get("source"):
            continue
        m = renderer.measure(d["source"], zone_for(s))
        mark = "合格" if m["verdict"] == "pass" else "不合格"
        print(f"[check] #{i} {s.get('title', '')}: {mark} {'; '.join(m['problems'])}")
        if m["verdict"] != "pass":
            targets.append(i)

    if not targets:
        print("[done] 直す必要のある図はありません")
        return
    if args.dry_run:
        print(f"[dry-run] 不合格 {len(targets)} 枚（#{', #'.join(map(str, targets))}）。DeepSeek は呼びませんでした")
        return

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("[error] DEEPSEEK_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    session = Session(cfg, api_key, ir_path.parent.name)
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8").replace("{max_rounds}", str(cfg["refine"]["max_rounds"]))
    logs = []
    for i in targets:
        slide = deck["slides"][i - 1]
        print(f"[refine] #{i} {slide.get('title', '')} を DeepSeek に直させます")
        try:
            log = refine_slide(slide, i, renderer, session, system_prompt)
        except BudgetExceeded as e:
            session.stopped = str(e)
            print(f"[stop] {e}。残りの図は元のまま残します", file=sys.stderr)
            break
        except DeepSeekError as e:
            log = {"slide": i, "title": slide.get("title", ""), "decision": "keep", "note": f"API エラー: {e}"}
            print(f"  [warn] #{i} は元の図を残します: {e}", file=sys.stderr)
        labels = {"replace": "差し替え", "drop": "図をやめて箇条書き", "keep": "元の図のまま",
                  "split": "全体図＋分解図に分割"}
        print(f"  → {labels[log['decision']]}  {log.get('note', '')}")
        logs.append(log)

    # 分解図のスライドを、元のスライドの後ろに差し込む（後ろから入れて番号のずれを防ぐ）
    for log in sorted(logs, key=lambda x: x["slide"], reverse=True):
        new_slides = log.pop("new_slides", None)
        if new_slides:
            idx = log["slide"]
            deck["slides"][idx:idx] = new_slides
            log["inserted"] = len(new_slides)

    # 元のファイルを残してから上書きする
    backup = ir_path.with_name("deck_ir.before_refine.json")
    backup.write_text(ir_path.read_text(encoding="utf-8"), encoding="utf-8")
    deck.setdefault("meta", {})["refine"] = {
        "refined_at": datetime.now(timezone.utc).isoformat(),
        "model": cfg["model"],
        "api_calls": session.api_calls,
        "cost_usd": round(session.cost_usd, 6),
        "stopped": session.stopped,
        "slides": logs,
    }
    validated = DeckIR(**deck)
    ir_path.write_text(validated.model_dump_json(indent=2, exclude_none=False), encoding="utf-8")
    print(f"[done] 保存: {ir_path}（元のファイル: {backup.name}）")
    print(f"[meta] API呼び出し {session.api_calls}回, ${session.cost_usd:.4f}")


if __name__ == "__main__":
    main()
