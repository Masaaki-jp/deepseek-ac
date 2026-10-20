---
title: "DeepSeekに構造化データを返させる：JSON Schemaの定義と検証をセットで設計する"
description: "LLMの構造化出力は「スキーマを定義する」だけでは壊れます。JSON Schemaの書き方、Pydantic/Zodでの検証、失敗時のリトライ設計までを1本にまとめ、実運用で壊れにくい形に落とします。"
category: pattern
tags:
  - DeepSeek
  - JSON Schema
  - 構造化出力
  - Python
  - Pydantic
publishedAt: 2026-10-20
draft: false
series: "構造化出力とスキーマ検証"
order: 1
difficulty: intermediate
minutes: 12
---

## リード

LLMにJSONを返させる作業は「スキーマを定義する」と「返ってきた値を検証する」の2段構えで設計します。
定義だけでは出力は保証されず、検証だけでは直らない壊れ方をするためです。
この記事では、スキーマ定義 → API呼び出し → 検証 → リトライまでを一続きの型として示します。

## 前提

- 想定読者: DeepSeek API を Python または TypeScript から呼んだことがあるエンジニア
- 必要な環境: Python 3.11+ / `openai` SDK（DeepSeek API は OpenAI 互換）、または Node.js 20+ / `zod`
- 前提知識: JSON Schema の基本、tool calling（function calling）の概要
- 注意: モデル名・パラメータの仕様は更新が速いため、実装前に公式ドキュメントで要確認

## 本文

### なぜ「定義」と「検証」を分けるのか

役割が違うからです。

| レイヤ | 役割 | 失敗すると |
|---|---|---|
| スキーマ定義 | モデルへの指示書。何をどの型で返すか | 項目の抜け、型の揺れ |
| 生成 | モデルがJSONを出力 | コードフェンス混入、余計なキー |
| 検証 | プログラム側の門番 | 不正データがDBや外部APIに流れる |

モデルはスキーマを「強く参考にする」だけで、**数学的な保証はありません**。したがって検証は必須で、検証エラーをどう扱うかまでが設計範囲です。

### レイヤ1: スキーマを定義する

まずは素朴な JSON Schema。ポイントは `required`・`enum`・`additionalProperties: false`・`description` の4つです。

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["category", "priority", "summary"],
  "properties": {
    "category": {
      "type": "string",
      "enum": ["bug", "feature", "question"],
      "description": "問い合わせ種別。判断できない場合は question"
    },
    "priority": {
      "type": "integer",
      "enum": [1, 2, 3],
      "description": "1=低 2=中 3=高"
    },
    "summary": {
      "type": "string",
      "description": "40文字以内の要約。体言止め"
    },
    "due_date": {
      "type": ["string", "null"],
      "format": "date",
      "description": "期限が明示されている場合のみ YYYY-MM-DD。なければ null"
    }
  }
}
```

避けたいのは「型は string で、中身はいい感じに」という設計です。自由文字列は揺れの温床なので、**列挙できるものは enum に落とす**のが基本方針になります。

### Pydantic を単一の正にする

スキーマを手書きJSONとPythonの両方で持つと必ずズレます。Pydantic を定義元にして JSON Schema を生成すると、検証とスキーマ定義が同じ型から出ます。

```python
from typing import Literal, Optional
from pydantic import BaseModel, Field
import json

class Ticket(BaseModel):
    category: Literal["bug", "feature", "question"] = Field(
        description="問い合わせ種別。判断できない場合は question"
    )
    priority: Literal[1, 2, 3] = Field(description="1=低 2=中 3=高")
    summary: str = Field(description="40文字以内の要約。体言止め")
    due_date: Optional[str] = Field(
        default=None,
        description="期限が明示されている場合のみ YYYY-MM-DD。なければ null",
    )

print(json.dumps(Ticket.model_json_schema(), ensure_ascii=False, indent=2))
```

`description` はドキュメントではなく**モデルへのプロンプト**として効きます。「判断できない場合は question」のような逃げ道を書いておくと、無理な分類をしなくなります。

### レイヤ2: API に渡す

OpenAI 互換の `tools` 形式でスキーマを渡すのが基本形です。

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

resp = client.chat.completions.create(
    model="deepseek-chat",  # モデル名は公式ドキュメントで要確認
    messages=[
        {"role": "system", "content": "問い合わせ文を分類し、必ずツールを1回だけ呼ぶこと。"},
        {"role": "user", "content": text},
    ],
    tools=[{
        "type": "function",
        "function": {
            "name": "classify_ticket",
            "description": "問い合わせを分類する",
            "parameters": Ticket.model_json_schema(),
        },
    }],
    tool_choice="required",
)
```

`response_format={"type": "json_object"}` を使う場合、**プロンプト側にスキーマ（またはその要約）を明示**しないとキー名がぶれます。JSON Schema をそのまま指定できるモードの対応可否はモデルによって変わるため、採用前に公式ドキュメントで要確認してください。tool calling 経由にしておくと、引数が構造化されて返るぶん扱いが安定します。

### レイヤ3: 返ってきた値を検証する

受け取った文字列をそのまま `json.loads` して使うのは危険です。Pydantic で検証し、失敗したら**エラー内容をモデルに返して1〜2回だけ再試行**します。

```python
from pydantic import ValidationError

def classify(text: str, max_retry: int = 2) -> Ticket:
    messages = [
        {"role": "system", "content": "問い合わせ文を分類し、必ずツールを1回だけ呼ぶこと。"},
        {"role": "user", "content": text},
    ]

    for _ in range(max_retry + 1):
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            tools=[TOOL],
            tool_choice="required",
            temperature=0,
        )
        args = resp.choices[0].message.tool_calls[0].function.arguments

        try:
            return Ticket.model_validate_json(args)
        except ValidationError as e:
            messages.append({
                "role": "user",
                "content": (
                    "前回の出力はスキーマに違反していました。修正して再度ツールを呼んでください。\n"
                    f"違反内容: {e.errors(include_url=False)}"
                ),
            })

    raise RuntimeError("スキーマ検証に失敗しました")
```

リトライ設計の要点は3つです。

- **上限を切る**（无限リトライはコストとレイテンシを食い潰す）
- **違反内容を具体的に返す**（「間違えました」だけでは直らない）
- **temperature を下げる**（揺れを減らす方向に倒す）

### TypeScript 側は Zod で同じ形にする

```ts
import { z } from "zod";

const Ticket = z.object({
  category: z.enum(["bug", "feature", "question"]),
  priority: z.union([z.literal(1), z.literal(2), z.literal(3)]),
  summary: z.string().max(40),
  due_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable(),
});

const parsed = Ticket.safeParse(JSON.parse(args));
if (!parsed.success) {
  // parsed.error.issues をプロンプトに戻す
}
```

### スキーマを壊れにくくするコツ

- **単位をキー名に入れる**: `amount` ではなく `amount_jpy`。モデルもヒトも誤解しない
- **null を明示する**: `Optional` を黙って許すと「空文字」と「null」と「欠落」が混在する
- **配列には `maxItems`**: 無限に要素を生やす出力を抑える
- **入れ子は2階層まで**: 深い構造は精度が落ちる。フラットに寄せる
- **日付は `format: date`**: 自由記述の日付はほぼ確実に揺れる

### 検証をテストに落とす

スキーマはコードなので、回帰テストを置きます。

```python
import pytest

@pytest.mark.parametrize("text,expected", [
    ("ログインできません", "bug"),
    ("CSV出力が欲しい", "feature"),
    ("料金体系は？", "question"),
])
def test_classify(text, expected):
    assert classify(text).category == expected
```

実LLMを叩くテストは不安定なので、**スキーマ単体のバリデーションテスト**（壊れたJSONを弾けるか）と**少数のゴールデンケース**に分けると運用しやすいです。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 出力の前後に ``` が付く | `json_object` 指定、またはコードフェンスを除去してから検証 |
| 必須キーが欠落する | `required` を明示し、検証エラーをプロンプトに戻して再試行 |
| enum 外の値が来る | `Literal` / `enum` で定義。自由文字列を減らす |
| 数値が文字列で返る | 自動 coerce に頼らず検証で落とす。黙って変換すると不整合の原因になる |
| 余計なキーが混ざる | `additionalProperties: false`。ただし無視される場合もあるため検証側でも弾く |
| ストリーミングで検証できない | 完了後にまとめて検証するか、部分パースは最後の砦として扱う |
| リトライでコストが膨らむ | 上限2回＋失敗率をメトリクス化して監視 |

## まとめ

- 構造化出力は「スキーマ定義」と「検証」を必ずセットで設計する
- スキーマは Pydantic / Zod を単一の正にして、JSON Schema を生成する
- 検証エラーは捨てずにプロンプトへ戻す。ただしリトライは上限を切る
- `description` はモデルへの指示。逃げ道（「判断できなければ question」）を書くと安定する
- スキーマはコードなので、バリデーションの回帰テストを置く

## チェックリスト

- [ ] スキーマの定義元が1つに決まっている（手書きJSONの二重管理をしていない）
- [ ] `required` / `enum` / `additionalProperties` を明示した
- [ ] `description` に「何を入れるか」「判断できないときの値」を書いた
- [ ] 受け取った値を必ずスキーマ検証してから業務ロジックに渡している
- [ ] 検証失敗時のリトライ回数・temperature・エラー反馈を決めた
- [ ] 壊れたJSONを弾くテストがある
- [ ] 使用したモデル名とパラメータを公式ドキュメントで確認した
