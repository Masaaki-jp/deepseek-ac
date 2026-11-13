---
title: "DeepSeek APIで日本語スライドを生成する：JSON出力から.pptxまで"
description: "DeepSeek APIで日本語スライドの構成・本文・スピーカーノートをJSONで生成し、python-pptxやMarpで.pptxに変換する実装手順。文字数制約、日本語フォント指定、JSONパース失敗時のリトライまでをコード付きで解説します。"
category: pattern
tags:
  - DeepSeek
  - API
  - Python
  - python-pptx
  - スライド
publishedAt: 2026-11-13
draft: false
series: "DeepSeek応用"
order: 1
difficulty: intermediate
minutes: 12
---

## リード

DeepSeek API で日本語スライドを作るとき、いきなり「パワポを作って」と頼むと失敗します。この記事では、**スライドの中身を JSON で受け取り、ファイル化は別レイヤーに任せる**パイプラインを組みます。日本語特有の文字数制約とフォント指定まで、動くコードで示します。

## 前提

- 想定読者: Python で API を叩いたことがあるエンジニア。社内勉強会・提案資料の自動生成を検討している人
- 必要な環境: Python 3.10+、`openai`、`python-pptx`、DeepSeek API キー
- 前提知識: Chat Completions API、JSON モード、PowerPoint の基本操作

```bash
pip install openai python-pptx
export DEEPSEEK_API_KEY="sk-..."
```

## 本文

### まず「できること／できないこと」を切り分ける

DeepSeek は**テキスト入力・テキスト出力の LLM** です。スライド作成に関して、できることとできないことは次のように分かれます。

| できる | できない |
|---|---|
| 構成（アウトライン）の設計 | `.pptx` バイナリの生成 |
| スライド単位の見出し・箇条書き | レイアウト・配色・アイコンの決定 |
| スピーカーノート／口頭原稿 | 画像生成（ネイティブ機能なし） |
| 想定 Q&A リストの作成 | 既存デッキの直接編集 |

つまり設計は「**DeepSeek = 中身、レンダラ = 見た目**」の分業です。この分離を最初に決めておくと、後でデザインを差し替えても LLM 側を触らずに済みます。

> モデル名・料金・JSON モードの仕様は更新が早い領域です。実装前に公式ドキュメントで要確認してください（本記事のコードは `deepseek-chat` を例にしています）。

### パイプライン全体像

```
素材（Markdown / 議事録 / 要件メモ）
   ↓  DeepSeek API（response_format=json_object）
deck.json  ← 構造化された中間表現
   ↓  バリデーション（文字数・枚数）
   ↓  レンダラ（python-pptx / Marp）
deck.pptx / deck.md
```

**中間表現を JSON に固定するのが肝**です。Markdown を直接吐かせると、レイアウト情報（表紙・区切り・まとめ）を後段で復元できません。

### ステップ1：出力スキーマを先に決める

LLM に自由記述させず、スキーマをプロンプトで固定します。

```json
{
  "deck_title": "40文字以内",
  "slides": [
    {
      "no": 1,
      "layout": "title | section | content | summary",
      "title": "20文字以内",
      "bullets": ["32文字以内", "..."],
      "speaker_note": "60〜120文字"
    }
  ]
}
```

`layout` を列挙値にしておくと、レンダラ側で「表紙は大きく、本文は箇条書き」と分岐できます。

### ステップ2：日本語向けプロンプト

日本語スライドで崩れる原因はほぼ**文字数**です。プロンプトで上限を明示し、後段でも検証します。

```python
SYSTEM = """あなたは日本語テクニカルプレゼンの構成担当です。
与えられた素材だけを根拠に、スライド構成をJSONで出力します。
素材に書かれていない数値・固有名詞・事例は絶対に追加しないでください。"""

USER_TEMPLATE = """# 素材
{material}

# 条件
- 聴衆: {audience}
- 発表時間: {minutes}分（1枚あたり約1分）
- 枚数: {slide_count}枚
- 言語: 日本語（簡体字・繁体字は使わない）

# 出力形式
以下のキーを持つJSONオブジェクトのみを出力。前後に説明文やコードフェンスを付けない。
{{
  "deck_title": "40文字以内",
  "slides": [
    {{
      "no": 1,
      "layout": "title | section | content | summary のいずれか",
      "title": "20文字以内・体言止め可・句点なし",
      "bullets": ["32文字以内", "..."],
      "speaker_note": "60〜120文字の口頭原稿"
    }}
  ]
}}

# 制約
- bullets は3項目以内。1枚に情報を詰めない
- 最初の1枚は layout="title"、最後は layout="summary"
- 中間に layout="section" の区切りスライドを1〜2枚入れる
- 箇条書きは「体言止め」または「動詞で終わる短文」。句点は付けない
"""
```

「素材にない数値を書くな」を明示するのは、要約タスクで LLM が統計値を捏造するのを防ぐためです。生成後に人間が1回レビューする前提も合わせて置いておきます。

### ステップ3：API 呼び出し

```python
import json
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)


def call_deepseek(material: str, audience="エンジニア", minutes=12, slide_count=10):
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER_TEMPLATE.format(
                material=material, audience=audience,
                minutes=minutes, slide_count=slide_count,
            )},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
        max_tokens=4000,
    )
    return json.loads(resp.choices[0].message.content)
```

- `response_format={"type": "json_object"}` を指定すると、コードフェンス付きの出力がほぼ消えます。ただし**プロンプト内に "JSON" という語を含める**必要がある実装が多いので、テンプレート側で明示しています。
- `temperature` は 0.3 前後。構成タスクで創造性を上げても、スライドが破綻するだけでメリットがありません。
- `max_tokens` を明示しないと JSON が途中で切れることがあります。12枚程度なら 4000 で足りますが、20枚超は分割生成に切り替えてください。

### ステップ4：文字数を「全角換算」でバリデーション

Python の `len()` は全角も半角も1文字と数えるため、日本語スライドの実寸と一致しません。全角=1.0、半角=0.5 で数えます。

```python
import unicodedata


def zenkaku_len(s: str) -> float:
    return sum(
        1.0 if unicodedata.east_asian_width(c) in ("W", "F", "A") else 0.5
        for c in s
    )


def validate(deck: dict) -> list[str]:
    issues = []
    for s in deck.get("slides", []):
        if zenkaku_len(s["title"]) > 20:
            issues.append(f"{s['no']}枚目: 見出しが長い（{zenkaku_len(s['title']):.1f}字）")
        if len(s.get("bullets", [])) > 3:
            issues.append(f"{s['no']}枚目: 箇条書きが{len(s['bullets'])}項目")
        for b in s.get("bullets", []):
            if zenkaku_len(b) > 32:
                issues.append(f"{s['no']}枚目: 「{b[:12]}…」が長い（{zenkaku_len(b):.1f}字）")
    return issues
```

違反があれば**再生成**します。部分修正させるより、指摘を素材に追記して丸ごと作り直すほうが安定します。

```python
def generate(material: str, retries: int = 2, **kw):
    prompt_material = material
    for _ in range(retries + 1):
        deck = call_deepseek(prompt_material, **kw)
        issues = validate(deck)
        if not issues:
            return deck
        prompt_material = material + "\n\n# 前回の指摘（必ず直す）\n" + "\n".join(issues)
    raise RuntimeError(f"スキーマ違反が解消しません: {issues}")
```

### ステップ5：python-pptx で日本語フォントを効かせる

`run.font.name` を設定しても、**日本語には反映されません**。OOXML では欧文（`a:latin`）と東アジア文字（`a:ea`）が別要素だからです。`a:ea` を明示的に設定します。

```python
from pptx.oxml.ns import qn

FONT = "Noto Sans JP"  # Mac: "Hiragino Sans" / Windows: "Yu Gothic" でも可


def set_run_font(run, name: str = FONT):
    run.font.name = name          # a:latin
    rPr = run._r.get_or_add_rPr()

    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        latin = rPr.find(qn("a:latin"))
        if latin is not None:
            latin.addnext(ea)     # スキーマ順（latin → ea → cs）を守る
        else:
            rPr.append(ea)
    ea.set("typeface", name)

    cs = rPr.find(qn("a:cs"))
    if cs is None:
        cs = rPr.makeelement(qn("a:cs"), {})
        ea.addnext(cs)
    cs.set("typeface", name)
```

`addnext` を使うのは、`a:rPr` の子要素に順序制約があるためです。末尾に `append` すると環境によっては PowerPoint が修復ダイアログを出します。

### ステップ6：デッキを組み立てる

```python
from pptx import Presentation
from pptx.util import Inches, Pt


def body_size(bullets: list[str]) -> int:
    longest = max((zenkaku_len(b) for b in bullets), default=0)
    if longest <= 20:
        return 24
    if longest <= 26:
        return 20
    return 18


def build_pptx(deck: dict, path: str = "deck.pptx") -> str:
    prs = Presentation()
    prs.slide_width = Inches(13.333)   # 16:9
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    for s in deck["slides"]:
        slide = prs.slides.add_slide(blank)

        tb = slide.shapes.add_textbox(Inches(0.8), Inches(0.6), Inches(11.7), Inches(1.2))
        tf = tb.text_frame
        tf.word_wrap = True
        r = tf.paragraphs[0].add_run()
        r.text = s["title"]
        set_run_font(r)
        r.font.size = Pt(36 if s["layout"] in ("title", "section") else 28)
        r.font.bold = True

        if s.get("bullets"):
            tb = slide.shapes.add_textbox(Inches(0.9), Inches(2.1), Inches(11.5), Inches(4.2))
            tf = tb.text_frame
            tf.word_wrap = True
            size = body_size(s["bullets"])
            for i, b in enumerate(s["bullets"]):
                p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                p.space_after = Pt(10)
                r = p.add_run()
                r.text = f"・{b}"
                set_run_font(r)
                r.font.size = Pt(size)

        slide.notes_slide.notes_text_frame.text = s.get("speaker_note", "")

    prs.save(path)
    return path
```

`text_frame.fit_text()` はフォントファイルを要求して環境依存になるため、文字数からフォントサイズを決めるほうが再現性が高いです。

### Markdown / Marp 出力も同じ JSON から

デザイン不要の社内共有なら、`.pptx` より Marp のほうが軽いです。中間表現が同じなので、レンダラを差し替えるだけです。

```python
def to_marp(deck: dict) -> str:
    out = ["---", "marp: true", "paginate: true", "---", ""]
    for s in deck["slides"]:
        out.append(f"## {s['title']}")
        out.append("")
        out += [f"- {b}" for b in s.get("bullets", [])]
        out.append("")
        if s.get("speaker_note"):
            out += [f"<!-- {s['speaker_note']} -->", ""]
    return "\n".join(out)
```

### 図解は「Mermaid コードを書かせる」で補う

DeepSeek に画像は作れませんが、**Mermaid 記法のテキスト**は生成できます。図の挿入位置だけ JSON に持たせ、Mermaid は別途レンダリングする運用が現実的です。

```json
{ "visual": { "type": "mermaid", "code": "flowchart LR\n  A[素材] --> B[DeepSeek] --> C[pptx]" } }
