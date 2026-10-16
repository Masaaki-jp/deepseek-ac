---
title: "DeepSeek APIでJSON形式の出力を得る基本｜response_formatとプロンプト設計"
description: "DeepSeek APIで構造化データを受け取るには、response_format の指定とプロンプト側の制約を両立させるのが最短ルート。JSON mode の使い方、失敗パターン、パース前のバリデーションまでをコード付きで整理する。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - JSON
publishedAt: 2026-10-16
draft: false
series: "DeepSeek APIの実装"
order: 3
difficulty: beginner
minutes: 10
---

## リード

LLMの出力をプログラムで扱うなら、自由文ではなくJSONで受け取りたい。
この記事では DeepSeek API で JSON 出力を安定させる手順と、崩れたときの対処を示す。

## 前提

- 想定読者: DeepSeek API を Python から呼び出せるエンジニア
- 必要な環境: Python 3.10+、`openai` SDK（DeepSeek は OpenAI 互換エンドポイントを提供）、`DEEPSEEK_API_KEY` を環境変数に設定済み
- 前提知識: Chat Completions の基本（`messages` / `model` / `temperature`）

> API 仕様（`response_format` の対応状況、モデル名、料金）は変更されやすい。最新は公式ドキュメントで要確認。

## 本文

### なぜ response_format を使うのか

プロンプトで「JSONで返して」と指示するだけでも、多くの場合は JSON らしき文字列が返る。
ただし次のような揺れが起きる。

- 前後に説明文やマークダウンのコードフェンスが付く
- キー名が毎回変わる
- 数値が文字列になったり、null と欠落が混在する

`response_format` に JSON を指定すると、モデル側が「JSONとして妥当な文字列」を返す方向に矯正される。パース失敗の主因である「余計な前置き」と「壊れた構文」を減らせる。

### 最小構成

```python
import json
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

resp = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "system", "content": "あなたは抽出器です。必ずJSONのみを出力します。"},
        {"role": "user", "content": "次の文から氏名と年齢を抽出: 田中太郎は32歳です。"},
    ],
    response_format={"type": "json_object"},
    temperature=0,
)

data = json.loads(resp.choices[0].message.content)
print(data)  # {'name': '田中太郎', 'age': 32}
```

ポイントは3つ。

- `response_format={"type": "json_object"}` を渡す
- system か user のどちらかに「JSON」という語を含める（含めないとエラーになる実装がある）
- `temperature=0` にして出力のブレを減らす

### スキーマをプロンプトで固定する

JSON mode は「JSONである」ことしか保証しない。キー名や型はプロンプトで縛る。

```python
system_prompt = """\
あなたは情報抽出器です。以下のスキーマに厳密に従い、JSONのみを出力してください。
説明文・コードフェンス・コメントは禁止です。

{
  "name": "string",
  "age": "integer | null",
  "tags": ["string"]
}
"""
```

型まで書くと、`"age": "32"` のような文字列化が減る。ただし完全には防げないので、後述のバリデーションは必ず入れる。

### Pydantic でバリデーションする

JSON として妥当でも、スキーマに合うとは限らない。受け取った側で検証する。

```python
from pydantic import BaseModel, ValidationError

class Person(BaseModel):
    name: str
    age: int | None = None
    tags: list[str] = []

try:
    person = Person.model_validate_json(resp.choices[0].message.content)
except ValidationError as e:
    # リトライ or ログ出力
    print("スキーマ不一致:", e)
```

`model_validate_json` は文字列を直接受けるので、`json.loads` を挟まずに済む。

### 失敗時のリトライ設計

スキーマ不一致は「モデルの出力」と「エラー内容」を渡して再試行すると改善しやすい。

```python
def extract(text: str, max_retries: int = 2) -> Person:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]
    for _ in range(max_retries + 1):
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = resp.choices[0].message.content
        try:
            return Person.model_validate_json(content)
        except ValidationError as e:
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"スキーマ違反: {e}. 修正して再出力。"})
    raise RuntimeError("JSON抽出に失敗")
```

リトライは無限ループにしない。上限を切って、超えたら呼び出し元で例外を扱う。

### 配列を返したいとき

トップレベルを配列にしたい場合は、いったんオブジェクトで包む。

```python
# NG: トップレベル配列はJSON modeで扱いにくい
# [{"name": "..."}, ...]

# OK: キーで包む
# {"items": [{"name": "..."}, ...]}
```

`{"items": [...]}` の形にしておけば、キー名が固定され、空配列も表現できる。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| `response_format` を指定してもエラーになる | プロンプトに「JSON」の語が含まれているか確認。含まれないと弾く実装がある |
| 出力の前後に ```json が付く | system で「コードフェンス禁止」を明記。それでも残る場合は正規表現で除去 |
| 数値が文字列で返る | スキーマに型を明記。Pydantic の `int` で検証し、違反時はリトライ |
| キーが欠落する | スキーマに `null` 許容を書き、Pydantic 側で `Optional` にする |
| たまに空文字が返る | `temperature=0` に固定。`max_tokens` が不足していないか確認 |

## まとめ

- JSON mode は「JSONらしさ」を担保するが、スキーマは保証しない
- プロンプトで型まで指定し、受け取り側で Pydantic 検証する二段構えが安定する
- スキーマ違反時はエラー内容を渡してリトライ。上限を必ず設ける
- トップレベル配列は避け、キーで包む

## チェックリスト

- [ ] `response_format={"type": "json_object"}` を指定した
- [ ] プロンプトに「JSON」という語を含めた
- [ ] `temperature=0` にした
- [ ] スキーマ（キー名・型）をプロンプトに明記した
- [ ] Pydantic などのバリデーションを挟んだ
- [ ] リトライ回数に上限を設けた
