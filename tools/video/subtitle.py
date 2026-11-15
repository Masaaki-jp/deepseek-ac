#!/usr/bin/env python3
"""deck_ir.json と timing.json から SRT 字幕を生成する。"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def format_srt_time(seconds: float) -> str:
    """秒数を SRT の時刻形式 (HH:MM:SS,mmm) に変換する。"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_slide_srt(sentences: list[str], total_duration: float) -> str:
    """1スライド分のSRT文字列を生成する（文字数で比例配分）。"""
    if not sentences:
        return ""

    total_chars = sum(len(s) for s in sentences)
    if total_chars == 0:
        return ""

    srt_lines = []
    current_time = 0.0

    for i, sentence in enumerate(sentences, 1):
        proportion = len(sentence) / total_chars
        duration = total_duration * proportion
        start = current_time
        end = current_time + duration

        srt_lines.append(str(i))
        srt_lines.append(f"{format_srt_time(start)} --> {format_srt_time(end)}")
        srt_lines.append(sentence)
        srt_lines.append("")

        current_time = end

    return "\n".join(srt_lines)


def main():
    parser = argparse.ArgumentParser(description="字幕(SRT)を生成する")
    parser.add_argument("ir", help="deck_ir.json のパス")
    parser.add_argument("-t", "--timing", help="timing.json のパス（デフォルト: audio/timing.json）")
    parser.add_argument("-o", "--output", help="出力先ディレクトリ（デフォルト: audio/ と同じ階層の sub/）")
    args = parser.parse_args()

    ir_path = Path(args.ir).resolve()
    if not ir_path.exists():
        print(f"[error] ファイルが見つかりません: {ir_path}", file=sys.stderr)
        sys.exit(1)

    deck = json.loads(ir_path.read_text(encoding="utf-8"))
    slides = deck["slides"]

    timing_path = Path(args.timing).resolve() if args.timing else ir_path.parent / "audio" / "timing.json"
    if not timing_path.exists():
        print(f"[error] timing.json が見つかりません: {timing_path}", file=sys.stderr)
        sys.exit(1)

    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    timings_by_idx = {s["index"]: s for s in timing["slides"]}

    out_dir = Path(args.output).resolve() if args.output else ir_path.parent / "sub"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[info] ir: {ir_path}")
    print(f"[info] timing: {timing_path}")
    print(f"[info] out: {out_dir}")

    success = 0
    for i, slide in enumerate(slides, 1):
        t = timings_by_idx.get(i)
        if not t or t["duration_sec"] <= 0:
            print(f"[skip] slide_{i:03d}: 音声なし", file=sys.stderr)
            continue

        sentences = slide.get("narration", [])
        srt_content = generate_slide_srt(sentences, t["duration_sec"])
        if not srt_content:
            print(f"[skip] slide_{i:03d}: narration が空", file=sys.stderr)
            continue

        srt_path = out_dir / f"slide_{i:03d}.srt"
        srt_path.write_text(srt_content, encoding="utf-8")
        success += 1
        print(f"[done] slide_{i:03d}.srt ({len(sentences)}文, {t['duration_sec']}s)")

    print()
    print(f"[summary] 生成: {success}/{len(slides)} SRT")
    print(f"[summary] 出力先: {out_dir}")


if __name__ == "__main__":
    main()
