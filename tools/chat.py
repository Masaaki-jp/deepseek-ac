#!/usr/bin/env python3
import os
import sys
import json
import re
from datetime import date
from pathlib import Path
import requests

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILE = Path(__file__).parent / "system_prompt.txt"

EXCLUDE_DIRS = {".git", ".astro", "node_modules", "dist", ".next", "build", ".cache"}

EXCLUDE_FILES = {
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock",
    ".env", ".env.local", ".env.production",
}

ALLOWED_EXTS = {
    ".md", ".mdx", ".astro", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".json", ".css", ".scss", ".html", ".txt", ".yml", ".yaml", ".toml",
}

MAX_FILE_SIZE = 100 * 1024

AUTO_LOAD_FILES = [
    "templates/article_template.md",
    "src/content.config.ts",
]

WRITABLE_DIRS = ["content/articles"]
WRITABLE_EXTS = {".md"}


def load_system_prompt() -> str:
    if PROMPT_FILE.exists():
        base = PROMPT_FILE.read_text(encoding="utf-8").strip()
    else:
        base = "You are a helpful assistant."

    today = date.today().isoformat()
    return (
        base
        + f"\n\n# 今日の日付\n今日は {today} です。"
        + "記事の publishedAt には必ずこの日付を使ってください。"
        + "日付を推測したり、過去や未来の日付を書いてはいけません。\n"
    )


def normalize_command(s: str) -> str:
    """コマンド内の全角スラッシュを半角に。各種空白は正規表現側で吸収。"""
    return s.replace("／", "/")


def safe_resolve(rel_path: str):
    try:
        p = (PROJECT_ROOT / rel_path).resolve()
    except (OSError, ValueError):
        return None
    try:
        p.relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    return p


def is_excluded(p: Path) -> bool:
    rel = p.relative_to(PROJECT_ROOT)
    parts = rel.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    if p.name in EXCLUDE_FILES:
        return True
    if p.name.startswith(".env"):
        return True
    if p.suffix.lower() not in ALLOWED_EXTS:
        return True
    return False


def list_files():
    results = []
    for p in sorted(PROJECT_ROOT.rglob("*")):
        if not p.is_file():
            continue
        if is_excluded(p):
            continue
        results.append(str(p.relative_to(PROJECT_ROOT)))
    return results


def read_file(rel_path: str):
    p = safe_resolve(rel_path)
    if p is None:
        return False, "プロジェクト外のパスは読めません"
    if not p.exists():
        return False, "ファイルが存在しません"
    if not p.is_file():
        return False, "ファイルではありません"
    if is_excluded(p):
        return False, "除外対象のファイルです"
    size = p.stat().st_size
    if size > MAX_FILE_SIZE:
        return False, f"ファイルが大きすぎます（{size} bytes > {MAX_FILE_SIZE}）"
    try:
        return True, p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False, "テキストとして読めません（バイナリの可能性）"


def read_multiline_input() -> str:
    print("[paste] 複数行モード: 終了は 'EOF' のみの行（Ctrl+C でキャンセル）")
    print("... ", end="", flush=True)
    lines = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print("\n[paste] キャンセルしました\n")
            return ""
        if line.strip() == "EOF":
            break
        lines.append(line)
        print("... ", end="", flush=True)
    text = "\n".join(lines)
    print(f"[paste] {len(lines)} 行 / {len(text)} 文字を受け取りました\n")
    return text


def extract_markdown_block(text: str):
    """テキストから最初の ```markdown ... ``` ブロックを抽出。
    入れ子のコードブロックに対応するため、貪欲マッチ（.*）で最後の ``` まで取る。"""
    m = re.search(r"```markdown\s*\n(.*)\n```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"```\s*\n(.*)\n```", text, re.DOTALL)
    if m:
        return m.group(1)
    return None


def is_writable(rel_path: str):
    p = safe_resolve(rel_path)
    if p is None:
        return False, "プロジェクト外のパスは書けません"
    rel = str(p.relative_to(PROJECT_ROOT))
    if not any(rel.startswith(d + "/") or rel == d for d in WRITABLE_DIRS):
        return False, f"書き込みは {', '.join(WRITABLE_DIRS)} 配下のみです"
    if p.suffix.lower() not in WRITABLE_EXTS:
        return False, f"拡張子は {', '.join(WRITABLE_EXTS)} のみです"
    return True, str(p)


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("Error: DEEPSEEK_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    system_prompt = load_system_prompt()
    messages = [{"role": "system", "content": system_prompt}]
    loaded_files = {}
    last_reply = ""

    print("DeepSeek チャット開始（終了: Ctrl+C または 'exit'）")
    print(f"プロジェクト: {PROJECT_ROOT}")
    print("コマンド: /ls  /read <path>  /files  /drop <path>  /paste  /save <path>  /clear  /reload")

    for rel in AUTO_LOAD_FILES:
        ok, content = read_file(rel)
        if ok:
            loaded_files[rel] = content
            print(f"[auto] {rel} を読み込みました（{len(content)} 文字）")
        else:
            print(f"[auto] {rel} の読み込みに失敗: {content}")

    print("-" * 50)

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n終了します")
            break

        user_input = normalize_command(user_input)

        if user_input.lower() in ("exit", "quit"):
            print("終了します")
            break
        if not user_input:
            continue

        if user_input == "/reload":
            system_prompt = load_system_prompt()
            messages = [{"role": "system", "content": system_prompt}]
            loaded_files.clear()
            print("[system] プロンプトを再読み込み、履歴とファイルをリセットしました\n")
            continue

        if user_input == "/clear":
            messages = [{"role": "system", "content": system_prompt}]
            loaded_files.clear()
            print("[system] 履歴とファイルをリセットしました\n")
            continue

        if user_input == "/ls":
            files = list_files()
            print(f"[system] 読み込み可能ファイル {len(files)} 件:")
            for f in files:
                mark = " *" if f in loaded_files else "  "
                print(f"{mark} {f}")
            print()
            continue

        if user_input == "/files":
            if not loaded_files:
                print("[system] 現在読み込み中のファイルはありません\n")
            else:
                print(f"[system] 読み込み中 {len(loaded_files)} 件:")
                for f in loaded_files:
                    print(f"  - {f}")
                print()
            continue

        m = re.match(r"^/read\s+(.+)$", user_input)
        if m:
            rel = m.group(1).strip()
            ok, content = read_file(rel)
            if not ok:
                print(f"[system] 読み込み失敗: {content}\n")
                continue
            loaded_files[rel] = content
            print(f"[system] {rel} を読み込みました（{len(content)} 文字）\n")
            continue

        m = re.match(r"^/drop\s+(.+)$", user_input)
        if m:
            rel = m.group(1).strip()
            if rel in loaded_files:
                del loaded_files[rel]
                print(f"[system] {rel} を外しました\n")
            else:
                print(f"[system] {rel} は読み込まれていません\n")
            continue

        if user_input == "/paste":
            user_input = read_multiline_input()
            if not user_input.strip():
                continue

        m = re.match(r"^/save\s+(.+)$", user_input)
        if m:
            if not last_reply:
                print("[save] 直前にDeepSeekの返答がありません\n")
                continue
            rel = m.group(1).strip()
            ok, result = is_writable(rel)
            if not ok:
                print(f"[save] 保存できません: {result}\n")
                continue
            content = extract_markdown_block(last_reply)
            if content is None:
                print("[save] 返答に markdown コードブロックが見つかりません\n")
                continue
            target = Path(result)
            if target.exists():
                ans = input(f"[save] {rel} は既に存在します。上書き？ [y/N]: ").strip().lower()
                if ans != "y":
                    print("[save] キャンセルしました\n")
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            print(f"[save] {rel} に保存しました（{len(content)} 文字）\n")
            continue

        api_messages = list(messages)
        if loaded_files:
            ref_parts = ["# 参考資料（プロジェクト内のファイル）\n"]
            for path, content in loaded_files.items():
                ref_parts.append(f"## {path}\n```\n{content}\n```\n")
            ref = "\n".join(ref_parts)
            api_messages.append({"role": "system", "content": ref})

        api_messages.append({"role": "user", "content": user_input})

        print("deepseek> ", end="", flush=True)

        reply_parts = []
        try:
            with requests.post(
                API_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "model": MODEL,
                    "messages": api_messages,
                    "stream": True,
                },
                stream=True,
                timeout=120,
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    line = line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        print(content, end="", flush=True)
                        reply_parts.append(content)
        except requests.exceptions.RequestException as e:
            print(f"\n[error] {e}", file=sys.stderr)
            continue

        print("\n")
        full_reply = "".join(reply_parts)
        last_reply = full_reply
        messages.append({"role": "user", "content": user_input})
        messages.append({"role": "assistant", "content": full_reply})


if __name__ == "__main__":
    main()
