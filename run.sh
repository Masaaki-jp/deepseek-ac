#!/usr/bin/env bash
# 記事からYouTube動画生成までの一括パイプライン
set -euo pipefail

# 設定
ARTICLE="${1:-}"
SKIP_UPLOAD=false
SKIP_REFINE=false
DRY_RUN=false

# 引数パース
for arg in "${@:2}"; do
  case "$arg" in
    --skip-upload) SKIP_UPLOAD=true ;;
    --skip-refine) SKIP_REFINE=true ;;
    --dry-run)     DRY_RUN=true ;;
    *) echo "[error] 不明な引数: $arg"; exit 1 ;;
  esac
done

if [[ -z "$ARTICLE" ]]; then
  echo "使い方: ./run.sh <article.md> [--skip-upload] [--skip-refine] [--dry-run]"
  echo "例:     ./run.sh content/articles/deepseek-json-schema-validation.md"
  exit 1
fi

if [[ ! -f "$ARTICLE" ]]; then
  echo "[error] ファイルが見つかりません: $ARTICLE"
  exit 1
fi

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "[error] DEEPSEEK_API_KEY が設定されていません"
  exit 1
fi

# slugの抽出（ファイル名から拡張子を除く）
SLUG=$(basename "$ARTICLE" .md)
OUT_DIR="output/${SLUG}"
IR_PATH="${OUT_DIR}/deck_ir.json"
PPTX_PATH="${OUT_DIR}/deck.pptx"
MP4_PATH="${OUT_DIR}/final.mp4"
META_PATH="${OUT_DIR}/yt_meta.json"

# 仮想環境が有効でなければ有効化
if [[ -z "${VIRTUAL_ENV:-}" ]] && [[ -f ".venv/bin/activate" ]]; then
  echo "[info] 仮想環境を有効化します"
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

echo "========================================="
echo "記事: $ARTICLE"
echo "出力: $OUT_DIR"
echo "========================================="

# 1. 抽出
echo ""
echo "[1/9] 抽出: article.md → deck_ir.json"
python tools/video/extract.py "$ARTICLE"

# 2. 図の改善（判定で不合格の図だけ DeepSeek に直させる）
echo ""
if [[ "$SKIP_REFINE" == "true" ]]; then
  echo "[2/9] 図の改善: スキップ（--skip-refine）"
else
  echo "[2/9] 図の改善: deck_ir.json の不合格の図を DeepSeek が修正"
  # 失敗しても deck_ir.json は元のまま残るので、動画づくりは止めずに続ける
  python tools/video/refine.py "$IR_PATH" || echo "[warn] 図の改善に失敗しました。元の deck_ir.json のまま続けます"
fi

# 3. PPTX生成
echo ""
echo "[3/9] PPTX生成: deck_ir.json → deck.pptx"
python tools/video/build_pptx.py "$IR_PATH"

# 4. PNG変換
echo ""
echo "[4/9] 画像化: deck.pptx → slides/*.png"
python tools/video/pptx2png.py "$PPTX_PATH"

# 5. TTS
echo ""
echo "[5/9] 音声合成: deck_ir.json → audio/*.wav"
python tools/video/tts.py "$IR_PATH"

# 6. 字幕
echo ""
echo "[6/9] 字幕生成: → sub/*.srt"
python tools/video/subtitle.py "$IR_PATH"

# 7. 動画合成
echo ""
echo "[7/9] 動画合成: → final.mp4"
python tools/video/assemble.py "$IR_PATH"

# 8. メタデータ
echo ""
echo "[8/9] YouTubeメタデータ生成: → yt_meta.json"
python tools/video/yt_meta.py "$IR_PATH" \
  --article-url "https://deepseek.ac/articles/${SLUG}" \
  -o "$META_PATH"

# 9. アップロード
if [[ "$SKIP_UPLOAD" == "true" ]]; then
  echo ""
  echo "[9/9] アップロード: スキップ（--skip-upload）"
  echo "  手動で実行する場合:"
  echo "  python tools/video/yt_upload.py $MP4_PATH -m $META_PATH"
  exit 0
fi

echo ""
echo "[9/9] YouTubeアップロード: final.mp4 → YouTube"
UPLOAD_ARGS=()
if [[ "$DRY_RUN" == "true" ]]; then
  UPLOAD_ARGS+=(--dry-run)
fi
python tools/video/yt_upload.py "$MP4_PATH" -m "$META_PATH" "${UPLOAD_ARGS[@]}"

echo ""
echo "========================================="
echo "完了しました！"
echo "  動画: $MP4_PATH"
echo "  メタ: $META_PATH"
echo "========================================="
