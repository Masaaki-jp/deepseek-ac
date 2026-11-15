#!/usr/bin/env python3
"""deck_ir.json から各スライドの音声を生成する。"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

TTS_URL = "https://api.deepseek.com/v1/tools/tts"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = Path(__file__).parent / "config.yaml"

DEFAULT_VOICE = "tide"
MIN_DURATION_SEC = 2.0
MAX_RETRIES = 3
PARALLELISM = 4


class RetryableError(Exception):
    pass


class NonRetryableError(Exception):
    pass


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def synthesize(text: str, api_key: str, voice_id: str) -> bytes:
    """1回分のTTS呼び出し。WAVのbytesを返す。"""
    r = requests.post(
        TTS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"text": text, "voice_id": voice_id, "format": "wav"},
        timeout=180,
    )
    if r.status_code == 200:
        return r.content
    if r.status_code in (429, 503, 504):
        raise RetryableError(f"HTTP {r.status_code}: {r.text[:200]}")
    raise NonRetryableError(f"HTTP {r.status_code}: {r.text[:300]}")


def probe_duration(wav_path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(wav_path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def synthesize_with_retry(text: str, api_key: str, voice_id: str, out_path: Path) -> float:
    """リトライと尺検証込みでWAVを書き出す。尺（秒）を返す。"""
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            wav_bytes = synthesize(text, api_key, voice_id)
            out_path.write_bytes(wav_bytes)
            duration = probe_duration(out_path)
            if duration < MIN_DURATION_SEC:
                raise RetryableError(f"WAVが短すぎます: {duration:.2f}s")
            return duration
        except (RetryableError, requests.RequestException, subprocess.CalledProcessError) as e:
            last_err = e
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                print(f"  [retry] {out_path.name}: {e} → {wait}秒待機", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"{out_path.name}: 全リトライ失敗: {last_err}")


def process_slide(idx: int, slide: dict, out_dir: Path, api_key: str, voice_id: str) -> dict:
    text = "".join(slide.get("narration", []))
    if not text.strip():
        print(f"  [skip] slide_{idx:03d}: narration が空", file=sys.stderr)
        return {"index": idx, "file": None, "title": slide.get("title", ""), "char_count": 0, "duration_sec": 0.0}
    out_path = out_dir / f"slide_{idx:03d}.wav"
    duration = synthesize_with_retry(text, api_key, voice_id, out_path)
    return {
        "index": idx,
        "file": out_path.name,
        "title": slide.get("title", ""),
        "char_count": len(text),
        "duration_sec": round(duration, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="deck_ir.json から音声を生成する")
    parser.add_argument("ir", help="deck_ir.json のパス")
    parser.add_argument("-o", "--output", help="音声の出力ディレクトリ（デフォルト: deck_ir.json と同じ場所の audio/）")
    parser.add_argument("--parallel", type=int, default=PARALLELISM, help=f"並列度（デフォルト: {PARALLELISM}）")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"voice_id（デフォルト: {DEFAULT_VOICE}）")
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("[error] DEEPSEEK_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    ir_path = Path(args.ir).resolve()
    if not ir_path.exists():
        print(f"[error] ファイルが見つかりません: {ir_path}", file=sys.stderr)
        sys.exit(1)

    deck = json.loads(ir_path.read_text(encoding="utf-8"))
    slides = deck["slides"]

    out_dir = Path(args.output).resolve() if args.output else ir_path.parent / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[info] ir: {ir_path}")
    print(f"[info] out: {out_dir}")
    print(f"[info] slides: {len(slides)}")
    print(f"[info] voice: {args.voice}, parallel: {args.parallel}")

    results = []
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {
            ex.submit(process_slide, i + 1, s, out_dir, api_key, args.voice): i + 1
            for i, s in enumerate(slides)
        }
        done = 0
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                result = fut.result()
                results.append(result)
                done += 1
                print(f"[done] {done}/{len(slides)} slide_{idx:03d}.wav ({result['duration_sec']}s)")
            except Exception as e:
                print(f"[error] slide_{idx:03d}: {e}", file=sys.stderr)

    results.sort(key=lambda r: r["index"])
    total_duration = sum(r["duration_sec"] for r in results)

    timing = {
        "voice_id": args.voice,
        "total_duration_sec": round(total_duration, 2),
        "slides": results,
    }
    timing_path = out_dir / "timing.json"
    timing_path.write_text(json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8")

    elapsed = time.time() - start
    print()
    print(f"[summary] 処理時間: {elapsed:.1f}秒")
    print(f"[summary] 成功: {len([r for r in results if r['file']])}/{len(slides)}")
    print(f"[summary] 合計尺: {total_duration:.1f}秒 ({total_duration/60:.2f}分)")
    print(f"[summary] timing: {timing_path}")

    if any(r["file"] is None for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
