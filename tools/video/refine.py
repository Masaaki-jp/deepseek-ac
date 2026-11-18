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
from tools.video.diagram_render import DiagramRenderer, load_diagram_config  # noqa: E402
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
]


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
                elif name == "submit_diagram":
                    submitted = (args.get("source", ""), args.get("reason", ""))
                    result = {"status": "received"}
                else:
                    result = {"error": f"知らないツールです: {name}"}
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, ensure_ascii=False)})
        if submitted:
            break

    # 採用するかどうかは、コードが判定し直して決める
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
        labels = {"replace": "差し替え", "drop": "図をやめて箇条書き", "keep": "元の図のまま"}
        print(f"  → {labels[log['decision']]}  {log.get('note', '')}")
        logs.append(log)

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
