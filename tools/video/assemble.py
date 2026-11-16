#!/usr/bin/env python3
"""スライド画像・音声・字幕から動画(MP4)を生成する。"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 出力解像度（YouTube推奨 16:9）
TARGET_W = 1920
TARGET_H = 1080

# 字幕デフォルト
DEFAULT_FONT = "Noto Sans CJK JP"
DEFAULT_FONT_SIZE = 22
SUBTITLE_STYLE = (
    "PrimaryColour=&H00FFFFFF,"     # 白文字
    "OutlineColour=&H00000000,"     # 黒縁
    "BackColour=&H80000000,"        # 半透明の黒背景（縁取りだけだと読みにくい場合用）
    "BorderStyle=1,"                # 縁取り+影
    "Outline=3,"                    # 縁を少し太く
    "Shadow=0,MarginV=40,Alignment=2"
)


def check_ffmpeg():
    if not shutil.which("ffmpeg"):
        print("[error] ffmpeg が見つかりません: sudo apt install ffmpeg", file=sys.stderr)
        sys.exit(1)


def escape_srt_path(p: Path) -> str:
    """ffmpegのsubtitlesフィルタ用にパスをエスケープする。"""
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_segment(png: Path, wav: Path, srt: Path | None, out_mp4: Path, font: str, font_size: int):
    """1スライド分のMP4を生成する（画像 + 音声 + 字幕焼き込み）。"""
    vf_parts = [f"scale=-2:{TARGET_H}"]  # 高さ1080に揃え、幅は偶数で自動計算

    if srt is not None and srt.exists():
        style = f"FontName={font},FontSize={font_size},{SUBTITLE_STYLE}"
        vf_parts.append(f"subtitles='{escape_srt_path(srt)}':force_style='{style}'")

    vf = ",".join(vf_parts)

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(png),
        "-i", str(wav),
        "-vf", vf,
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(out_mp4),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def concat_segments(segments: list[Path], out_path: Path):
    """MP4セグメントを1本に連結する。"""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for seg in segments:
            escaped = str(seg).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
        list_path = Path(f.name)

    try:
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
    finally:
        list_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="スライド・音声・字幕からMP4を生成する")
    parser.add_argument("ir", help="deck_ir.json のパス")
    parser.add_argument("-o", "--output", help="出力MP4（デフォルト: <slug>/final.mp4）")
    parser.add_argument("--font", default=DEFAULT_FONT, help=f"字幕フォント（デフォルト: {DEFAULT_FONT}）")
    parser.add_argument("--font-size", type=int, default=DEFAULT_FONT_SIZE, help=f"字幕サイズ（デフォルト: {DEFAULT_FONT_SIZE}）")
    parser.add_argument("--no-subtitle", action="store_true", help="字幕を焼き込まない")
    args = parser.parse_args()

    check_ffmpeg()

    ir_path = Path(args.ir).resolve()
    if not ir_path.exists():
        print(f"[error] ファイルが見つかりません: {ir_path}", file=sys.stderr)
        sys.exit(1)

    base = ir_path.parent
    slides_dir = base / "slides"
    audio_dir = base / "audio"
    sub_dir = base / "sub"

    if not slides_dir.exists() or not audio_dir.exists():
        print(f"[error] slides/ または audio/ がありません", file=sys.stderr)
        sys.exit(1)

    deck = json.loads(ir_path.read_text(encoding="utf-8"))
    n = len(deck["slides"])

    print(f"[info] ir: {ir_path}")
    print(f"[info] slides: {n}")
    print(f"[info] font: {args.font} / size: {args.font_size}")
    print(f"[info] subtitle: {'off' if args.no_subtitle else 'on'}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        segments = []

        for i in range(1, n + 1):
            png = slides_dir / f"slide_{i:03d}.png"
            wav = audio_dir / f"slide_{i:03d}.wav"
            srt = None if args.no_subtitle else sub_dir / f"slide_{i:03d}.srt"

            if not png.exists() or not wav.exists():
                print(f"[skip] slide_{i:03d}: PNG または WAV なし", file=sys.stderr)
                continue

            seg_path = tmp / f"seg_{i:03d}.mp4"
            build_segment(png, wav, srt, seg_path, args.font, args.font_size)
            segments.append(seg_path)
            print(f"[done] {i}/{n} seg_{i:03d}.mp4")

        if not segments:
            print("[error] セグメントが1つも生成されませんでした", file=sys.stderr)
            sys.exit(1)

        out_path = Path(args.output).resolve() if args.output else base / "final.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        concat_segments(segments, out_path)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print()
    print(f"[summary] 出力: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
