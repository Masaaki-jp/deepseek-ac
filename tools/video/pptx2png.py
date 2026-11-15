#!/usr/bin/env python3
"""PPTXをPNG画像に変換する（LibreOffice + pdftoppm）。"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def check_tools():
    missing = []
    if not shutil.which("soffice") and not shutil.which("libreoffice"):
        missing.append("libreoffice")
    if not shutil.which("pdftoppm"):
        missing.append("pdftoppm (poppler-utils)")
    if missing:
        print(f"[error] 必要なツールが見つかりません: {', '.join(missing)}", file=sys.stderr)
        print("  sudo apt install libreoffice poppler-utils", file=sys.stderr)
        sys.exit(1)


def pptx_to_png(pptx_path: Path, out_dir: Path, dpi: int = 150, prefix: str = "slide"):
    """PPTXをPNGに変換する。"""
    check_tools()
    out_dir.mkdir(parents=True, exist_ok=True)

    soffice = shutil.which("soffice") or shutil.which("libreoffice")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        print(f"[info] PPTX → PDF ({pptx_path.name})")
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf",
             "--outdir", str(tmp), str(pptx_path)],
            check=True, capture_output=True,
        )
        pdf_path = tmp / (pptx_path.stem + ".pdf")
        if not pdf_path.exists():
            print(f"[error] PDF変換に失敗: {pdf_path}", file=sys.stderr)
            sys.exit(1)

        print(f"[info] PDF → PNG (DPI={dpi})")
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi),
             str(pdf_path), str(out_dir / prefix)],
            check=True, capture_output=True,
        )

    files = sorted(out_dir.glob(f"{prefix}-*.png"))
    for f in files:
        try:
            num = int(f.stem.split("-")[-1])
            new_name = out_dir / f"{prefix}_{num:03d}.png"
            if f != new_name:
                f.rename(new_name)
        except ValueError:
            pass

    final = sorted(out_dir.glob(f"{prefix}_*.png"))
    print(f"[done] {len(final)}枚のPNGを生成: {out_dir}")
    for f in final[:3]:
        print(f"  - {f.name} ({f.stat().st_size // 1024} KB)")
    if len(final) > 3:
        print(f"  - ... 他 {len(final) - 3}枚")


def main():
    parser = argparse.ArgumentParser(description="PPTXをPNGに変換する")
    parser.add_argument("pptx", help="入力PPTXのパス")
    parser.add_argument("-o", "--output", help="PNG出力ディレクトリ（デフォルト: PPTXと同じ階層の slides/）")
    parser.add_argument("--dpi", type=int, default=150, help="解像度（デフォルト: 150）")
    args = parser.parse_args()

    pptx_path = Path(args.pptx).resolve()
    if not pptx_path.exists():
        print(f"[error] ファイルが見つかりません: {pptx_path}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output).resolve() if args.output else pptx_path.parent / "slides"
    print(f"[info] input:  {pptx_path}")
    print(f"[info] output: {out_dir}")
    print(f"[info] dpi:    {args.dpi}")

    pptx_to_png(pptx_path, out_dir, dpi=args.dpi)


if __name__ == "__main__":
    main()
