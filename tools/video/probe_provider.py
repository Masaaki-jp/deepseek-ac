#!/usr/bin/env python3
"""フォールバック先が、パイプラインに必要な機能に対応しているかを確かめる。

3回だけリクエストを送る（さくらの無償プランなら月3,000回の枠から3回分）。
  1. 普通の応答        … 抽出・図の改善のどちらにも必要
  2. JSONモード         … 抽出（extract.py）で使う response_format={"type": "json_object"}
  3. ツール呼び出し     … 図の改善（refine.py）で使う tools

使い方:
  export SAKURA_AI_ENGINE_TOKEN=...
  python tools/video/probe_provider.py            # config.yaml の sakura を確かめる
  python tools/video/probe_provider.py deepseek   # 比較のため公式も確かめる
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.video.deepseek_api import _record_usage, load_providers  # noqa: E402

TOOL = {
    "type": "function",
    "function": {
        "name": "get_slide_count",
        "description": "記事の見出しの数から、作るスライドの枚数を返す",
        "parameters": {"type": "object", "properties": {"headings": {"type": "integer"}}, "required": ["headings"]},
    },
}

CASES = [
    ("普通の応答", {"messages": [{"role": "user", "content": "「動画」を英語で一語だけ答えてください。"}]}),
    ("JSONモード", {"messages": [{"role": "user", "content": '{"ok": true, "word": "video"} という形の json だけを返してください。'}],
                   "response_format": {"type": "json_object"}}),
    ("ツール呼び出し", {"messages": [{"role": "user", "content": "見出しが5つある記事のスライド枚数を、ツールで調べてください。"}],
                     "tools": [TOOL]}),
]


def judge(name: str, data: dict) -> tuple[bool, str]:
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    if name == "JSONモード":
        try:
            json.loads(content)
            return True, f"JSONとして読めた: {content.strip()[:60]}"
        except json.JSONDecodeError:
            return False, f"JSONとして読めない: {content.strip()[:60]}"
    if name == "ツール呼び出し":
        calls = msg.get("tool_calls") or []
        if calls:
            fn = calls[0]["function"]
            return True, f"{fn['name']}({fn.get('arguments')})"
        return False, f"ツールを呼ばずに文章で答えた: {content.strip()[:60]}"
    return bool(content.strip()), content.strip()[:60]


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "sakura"
    providers, primary_name, _ = load_providers()
    prov = next((p for p in providers if p["name"] == target), None)
    if prov is None:
        sys.exit(f"[error] config.yaml の fallback.providers に {target} がありません")
    key = os.environ.get(prov.get("key_env", ""), "")
    if not key:
        sys.exit(f"[error] 環境変数 {prov.get('key_env')} が設定されていません")
    model = prov.get("model")
    if model is None:  # 本来の宛先は config の model を使う
        import yaml
        model = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))["model"]

    print(f"[probe] {target}  {prov['url']}  model={model}\n")
    results = {}
    for name, extra in CASES:
        body = {"model": model, "max_tokens": 200, "temperature": 0, **extra}
        t0 = time.monotonic()
        try:
            r = requests.post(prov["url"], headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                              json=body, timeout=60)
        except requests.RequestException as e:
            print(f"  {name:<8} 通信エラー: {e}")
            results[name] = None
            continue
        ms = (time.monotonic() - t0) * 1000
        if r.status_code != 200:
            print(f"  {name:<8} ✗ HTTP {r.status_code}（{ms:.0f}ms）: {r.text[:160]}")
            results[name] = False if r.status_code in (400, 422) else None
            continue
        data = r.json()
        _record_usage(prov, model, data, {"tool": "probe", "provider": target, "case": name})
        ok, note = judge(name, data)
        usage = data.get("usage") or {}
        print(f"  {name:<8} {'✓' if ok else '✗'} {ms:.0f}ms  in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}  {note}")
        results[name] = ok

    print("\nconfig.yaml の supports に書く値:")
    print(f"        json_mode: {_yaml(results.get('JSONモード'))}")
    print(f"        tools: {_yaml(results.get('ツール呼び出し'))}")
    if results.get("普通の応答") is not True:
        print("\n[warn] 普通の応答が得られませんでした。トークン・URL・モデル名を確認してください")


def _yaml(v) -> str:
    return "null   # 判定できず（通信エラーや混雑）。時間をおいて再実行" if v is None else str(v).lower()


if __name__ == "__main__":
    main()
