---
title: "LLMに動画を書かせない：JSONを挟んで記事からYouTubeまで自動化するパイプライン設計"
description: "記事→JSON→PPTX→MP4→YouTubeを多段変換でつなぐ設計。LLMの出力と決定的処理をJSONで分離し、失敗を局所化する。JSONモードの空応答、枚数暴走、日本語フォント化け、字幕焼き込み、クォータまで実装の急所を整理する。"
category: implementation
tags:
  - DeepSeek
  - Python
  - python-pptx
  - ffmpeg
  - YouTube API
  - 自動化
publishedAt: 2026-11-15
draft: false
series: "DeepSeek応用"
order: 3
difficulty: advanced
minutes: 14
---

[![動画を見る](https://img.youtube.com/vi/XLAII2fGa90/0.jpg)](https://www.youtube.com/watch?v=XLAII2fGa90)


## リード

記事 Markdown から YouTube 動画を自動生成するパイプラインを、「テキスト → JSON → PPTX（＋音声・字幕）→ MP4」の多段変換として設計する。

モデルに「動画のコードを書いて」と頼む設計は捨てる。理由は**検証可能性**（モデルが何をスライドとして解釈したかを人間が確認できる）と**再試行の局所化**（失敗時に戻る範囲を限定できる）の2点。

この記事では、各フェーズの実装上の急所と、そこを外すと何が壊れるかをまとめる。

## 前提

- 想定読者: Python で外部 API を叩いた経験があり、生成物を自動処理したいエンジニア
- 必要な環境: Python 3.11+、ffmpeg（`subtitles` フィルタ有効ビルド）、Noto Sans CJK JP、YouTube Data API の OAuth2 クライアント
- 前提知識: OpenAI 互換 API の基本、`python-pptx` の初歩、SRT の形式

> API 仕様（モデル名・`response_format` の対応状況・TTS のエンドポイントやボイス名・クォータ単価）は変更される。本記事の値は実装時点の想定なので、必ず公式ドキュメントで要確認。

## なぜ「動画を書かせる」のをやめたのか

いきなり MP4 を作らせると、失敗したときに**どこが悪いのか分からない**。スライド構成が悪いのか、レイアウトが崩れたのか、音声が無音なのか、エンコードが失敗したのかが1つの成果物に潰れてしまう。

そこで境界を切る。

| 層 | 入力 | 出力 | 性質 |
|---|---|---|---|
| 構造抽出 | 記事 Markdown | `deck_ir.json` | 非決定的（LLM） |
| スライド生成 | `deck_ir.json` | `*.pptx` / `*.png` | 決定的 |
| 音声合成 | 台本テキスト | `*.wav` | 非決定的（外部API） |
| 字幕生成 | 台本＋尺 | `*.srt` | 決定的 |
| 動画合成 | PNG＋WAV＋SRT | `*.mp4` | 決定的 |
| 公開 | MP4＋メタ情報 | `video.json` | 外部API |

LLM は**最初の1層だけ**。あとは全部、入力が同じなら出力が同じ処理にする。これで「モデルを変える」「デザインを変える」「配信先を変える」が独立する。

## 1. 構造抽出層：JSONモードの使いどころと限界

記事を `##` 見出しでセクション分割し、チャンクごとに DeepSeek へ投げてスライド計画を得る。

```python
import json, re, time
from openai import OpenAI
from pydantic import BaseModel, ValidationError

client = OpenAI(api_key=API_KEY, base_url="https://api.deepseek.com")

class Slide(BaseModel):
    layout: str                      # title / bullets / section
    title: str
    bullets: list[str] = []

class Deck(BaseModel):
    slides: list[Slide]

SYSTEM = """あなたは技術記事をスライド計画に変換する編集者です。
出力は必ず次の形の JSON オブジェクトにしてください。
{"slides":[{"layout":"bullets","title":"...","bullets":["...","..."]}]}
"""
```

呼び出し側。ここに3つの防御を入れる。

```python
def plan_chunk(chunk: str, target: int, retries: int = 4) -> Deck:
    user = f"次のセクションを {target} 枚のスライドにしてください。\n\n{chunk}"
    for i in range(retries):
        try:
            res = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=2000,
            )
            choice = res.choices[0]
            if choice.finish_reason == "length":
                raise ValueError("truncated")          # 打ち切り＝壊れたJSON
            raw = choice.message.content
            if not raw or not raw.strip():
                raise ValueError("empty content")      # JSONモードの空応答
            return Deck.model_validate(json.loads(raw))
        except (ValueError, json.JSONDecodeError, ValidationError):
            if i == retries - 1:
                raise
            time.sleep(2 ** i)                         # 指数バックオフ
```

### 3つの防御が必要な理由

- **空の content**: JSON モードでも、出力プレッシャーで空文字が返ることはある。失敗としてリトライに回す
- **打ち切られJSON**: `finish_reason == "length"` は構文が閉じていない可能性が高い。パース前に弾く
- **スキーマ違反**: JSON モードが保証するのは「JSON であること」だけ。**スキーマは保証されない**ので pydantic で検証し、違反もリトライ対象にする

### 枚数の暴走を止める

「8〜15枚で」とだけ指示すると、チャンク分割時に各チャンクが律儀に8枚出して合計33枚になる。指示は**チャンク単位の目標枚数**に落とす。

```python
CHARS_PER_SLIDE = 320

def target_slides(chunk: str) -> int:
    n = len(re.sub(r"\s", "", chunk))
    return max(1, min(4, round(n / CHARS_PER_SLIDE)))
```

全体の枚数制御は LLM ではなく**呼び出し側**で持つ。合計が上限を超えたら、末尾チャンクから間引く。

### 表紙はPython側で挿入する

各チャンクに「最初は title レイアウト」と書くと、全チャンクが表紙を作る。表紙指示はプロンプトから削除し、`slides.insert(0, cover_slide(article))` で機械的に差し込む。

> 推論系モデルは `response_format` の挙動が異なる場合がある。構造抽出は非推論モデルに固定し、対応状況は公式ドキュメントで確認する。

## 2. スライド生成層：python-pptx の3つの罠

### 罠1: Markdown 記号の残存

LLM の出力に `##` や `-` が混ざる。表示前に機械的に除去する。

```python
def sanitize_title(s: str) -> str:
    return re.sub(r"^[#\-\*\s]+", "", s).strip()

def sanitize_bullet(s: str) -> str:
    return re.sub(r"^[\-\*・]\s*", "", s).strip()
```

プロンプトで「記号を付けないで」と頼むより、**除去コードを書くほうが確実**。LLM への指示は確率的、サニタイズは決定的。

### 罠2: 日本語フォントが明朝体になる

`run.font.name = "Meiryo"` は OOXML の `a:latin` しか設定しない。日本語部分は `a:ea`（東アジア文字）、複合スクリプトは `a:cs` を見るため、明朝体にフォールバックする。

```python
from pptx.oxml.ns import qn
from pptx.util import Pt

def style_run(run, typeface="Meiryo", size=Pt(20), bold=False):
    run.font.size = size
    run.font.bold = bold
    run.font.name = typeface                     # a:latin
    rPr = run._r.get_or_add_rPr()
    prev = rPr.find(qn("a:latin"))
    for tag in ("a:ea", "a:cs"):                 # 順序が重要
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            prev.addnext(el)                     # latin → ea → cs の順を守る
        el.set("typeface", typeface)
        prev = el
```

`a:rPr` の子要素順はスキーマで固定されている。末尾に `append` すると順序違反になり、PowerPoint 側で無視されたり修復ダイアログが出たりする。**必ず `a:latin` の直後から順に挿入する**。

### 罠3: 4:3 で出力される

`python-pptx` のデフォルトは 4:3。YouTube に出すなら 16:9 に固定し、プレースホルダの座標も手で置き直す（レイアウト既定値は 4:3 前提で配置されているため）。

```python
prs.slide_width  = Emu(12192000)   # 13.333 in
prs.slide_height = Emu(6858000)    # 7.5 in
```

「4:3 のままでもいいか」と迷う時間が一番もったいない。**出口（YouTube）を先に固定すれば、途中の設計は自ずと決まる。**

## 3. 音声合成と字幕：スライド単位で割り切る

### 並列度は 4 に固定する

スライドごとに TTS を叩くので、素直に並列化したくなる。ただしレート制限（429）に当たる。

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=4) as ex:
    list(ex.map(synthesize_slide, slides))
```

429 はリトライでも吸収できるが、**並列度を下げるほうが安定する**。上限を上げたくなったら、まずレイテンシではなくレート制限の実測値を見る。

> TTS はプロバイダの入れ替わりが激しい領域。`synthesize_slide()` の内部だけを差し替え可能にし、`audio/manifest.json` にプロバイダ名とボイス名を記録しておくと、後から比較・再生成しやすい。利用するエンドポイントとボイス名は公式ドキュメントで要確認。

### 字幕は文字数で比例配分する

文ごとの正確なタイムスタンプが取れない場合、スライドの総尺を文字数で按分して SRT を作る。技術解説動画ならこの精度で実用上足りる。

```python
def to_ts(t: float) -> str:
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h:02d}:{m:02d}:{t % 60:06.3f}".replace(".", ",")

def srt_from_script(script: str, total: float) -> str:
    parts = [s.strip() for s in re.split(r"(?<=[。．！？!?])", script) if s.strip()]
    weights = [max(len(p), 1) for p in parts]
    denom = sum(weights)
    t, lines = 0.0, []
    for i, (p, w) in enumerate(zip(parts, weights), start=1):
        dur = total * w / denom
        lines += [str(i), f"{to_ts(t)} --> {to_ts(t + dur)}", p, ""]
        t += dur
    return "\n".join(lines)
