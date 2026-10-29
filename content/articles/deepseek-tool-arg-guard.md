---
title: "DeepSeek APIの引数検証——400エラーを送信前に潰す実装パターン"
description: "DeepSeek APIのリクエストを送信する前に引数を検証する方法を、Pydantic／Zodでのスキーマ定義とパラメータ間の整合性チェックを中心に解説。400 invalid_request_errorの切り分け、リトライ可否の判断、検証ログの設計まで扱う。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - TypeScript
  - バリデーション
publishedAt: 2026-10-29
draft: false
series: "DeepSeek APIの実装"
order: 5
difficulty: intermediate
minutes: 12
---

## リード

DeepSeek API の呼び出しで一番もったいないのは、ネットワークを往復した末に `400 invalid_request_error` で返ってくるリクエストだ。この記事では、送信前に引数を検証して落とすためのスキーマ設計と、パラメータ同士の整合性チェックの書き方を示す。あわせて「どの 400 はリトライすべきで、どれはすべきでないか」の切り分けも整理する。

## 前提

- 想定読者: DeepSeek API をアプリケーションに組み込み始めたエンジニア
- 必要な環境: Python 3.10+ / Pydantic v2、または Node.js 18+ / TypeScript / Zod
- 前提知識: Chat Completions 形式のリクエスト（`model` と `messages`）を一度は手で投げたことがあること

> **注記**: パラメータ名・値域・上限値は変更される。本記事のコードは値域を定数／設定ファイルに外出しし、公式ドキュメントと突き合わせて更新する前提で書いている。実際の上限値は必ず公式ドキュメントで確認してほしい。

## 本文

### なぜクライアント側で検証するのか

サーバ側でも当然バリデーションはかかる。それでもクライアント側で検証する理由は3つある。

1. **レイテンシとコストの無駄を消す** — 明らかに不正なリクエストを network に出さない
2. **エラーメッセージが自分向けに書ける** — API のエラー本文は汎用的で、どのフィールドが原因か分かりにくいことがある
3. **リトライ設計が素直になる** — 「検証で落ちた＝リトライ不要」「HTTP 429/5xx＝リトライ対象」と層が分かれる

逆に、クライアント側だけで済ませてはいけない。SDK のバージョンアップやモデル追加でルールは変わるので、**クライアント検証は「早期に落とすための最適化」であって、唯一の防波堤ではない**と位置づける。

### 検証すべき4つの層

引数検証は一枚岩にせず、層を分けると保守しやすい。

| 層 | 見るもの | 例 |
|---|---|---|
| 1. 転送層 | 認証・エンドポイント・タイムアウト | API キーの有無、`Authorization` ヘッダ |
| 2. 構造 | 必須フィールド、型、未知フィールド | `messages` が空でない、`role` が列挙値 |
| 3. 値域 | 数値の上下限、文字数 | `temperature` の範囲、`max_tokens` の上限 |
| 4. 整合性 | パラメータ同士の矛盾 | `stream` と一部オプションの併用可否 |

層 3・4 が抜けやすい。特に層 4 は「単体では正しいが組み合わせで 400 になる」ケースで、型システムでは捕まらない。

### Pydantic v2 でスキーマを書く

まずは素直にモデル化する。値域は `Field` に、定数はモジュール定数に置く。

```python
# schema.py
from typing import Literal
from pydantic import BaseModel, Field, model_validator, field_validator

# 値域は設定として外出しする（公式ドキュメントと突き合わせて更新する）
TEMPERATURE_MIN, TEMPERATURE_MAX = 0.0, 2.0
TOP_P_MIN, TOP_P_MAX = 0.0, 1.0
MAX_TOKENS_LIMIT = 8192          # モデルごとに異なる場合はモデル別dictにする
MAX_MESSAGE_CHARS = 100_000      # アプリ側の安全弁としての上限

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    model_config = {"extra": "forbid"}   # 未知フィールドは落とす

    role: Role
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None

    @field_validator("content")
    @classmethod
    def check_length(cls, v: str | None) -> str | None:
        if v is not None and len(v) > MAX_MESSAGE_CHARS:
            raise ValueError(f"content が長すぎる: {len(v)} > {MAX_MESSAGE_CHARS}")
        return v


class ChatRequest(BaseModel):
    model_config = {"extra": "forbid"}

    model: str
    messages: list[Message] = Field(min_length=1)
    temperature: float = Field(1.0, ge=TEMPERATURE_MIN, le=TEMPERATURE_MAX)
    top_p: float = Field(1.0, ge=TOP_P_MIN, le=TOP_P_MAX)
    max_tokens: int | None = Field(None, ge=1, le=MAX_TOKENS_LIMIT)
    stream: bool = False
    stop: list[str] | str | None = None

    @model_validator(mode="after")
    def check_semantics(self) -> "ChatRequest":
        roles = [m.role for m in self.messages]

        # system は先頭に1つだけ、という運用ルールを機械的に守る
        if roles.count("system") > 1:
            raise ValueError("system メッセージは1つにしてください")
        if "system" in roles and roles[0] != "system":
            raise ValueError("system メッセージは先頭に置いてください")

        # 最後が assistant のリクエストは「続きの生成」扱いになる。
        # 通常の呼び出しと混ざると事故るので、明示フラグが無い限り弾く
        if roles[-1] == "assistant" and not getattr(self, "_allow_prefix", False):
            raise ValueError("最後が assistant のリクエストは prefix completion 専用です")

        # tool メッセージには tool_call_id が要る
        for m in self.messages:
            if m.role == "tool" and not m.tool_call_id:
                raise ValueError("role=tool には tool_call_id が必要です")
            if m.role != "tool" and m.content is None:
                raise ValueError(f"role={m.role} には content が必要です")

        # 空文字の stop は意図しない即時停止を招く
        if isinstance(self.stop, list) and any(s == "" for s in self.stop):
            raise ValueError("stop に空文字は指定できません")

        return self
```

ポイントは3つ。

- `extra="forbid"` で **タイポを即死させる**（`max_token` のような揺れが一番厄介）
- 値域は `Field` で宣言的に、整合性は `model_validator` で手続き的に書く
- 「API の制約」と「アプリの運用ルール」をコメントで区別する。混ざると、API 仕様変更時にどれを消せばいいか分からなくなる

### Zod で同じことを書く（TypeScript）

```ts
// schema.ts
import { z } from "zod";

export const TEMPERATURE_MIN = 0;
export const TEMPERATURE_MAX = 2;
export const MAX_TOKENS_LIMIT = 8192;

const Message = z.object({
  role: z.enum(["system", "user", "assistant", "tool"]),
  content: z.string().nullable().optional(),
  name: z.string().optional(),
  tool_call_id: z.string().optional(),
}).strict();  // 未知フィールドを拒否

export const ChatRequest = z.object({
  model: z.string().min(1),
  messages: z.array(Message).min(1),
  temperature: z.number().min(TEMPERATURE_MIN).max(TEMPERATURE_MAX).default(1),
  top_p: z.number().min(0).max(1).default(1),
  max_tokens: z.number().int().min(1).max(MAX_TOKENS_LIMIT).optional(),
  stream: z.boolean().default(false),
  stop: z.union([z.string(), z.array(z.string())]).optional(),
}).strict().superRefine((req, ctx) => {
  const roles = req.messages.map((m) => m.role);

  if (roles.filter((r) => r === "system").length > 1) {
    ctx.addIssue({ code: "custom", path: ["messages"], message: "system は1つまで" });
  }
  if (roles.includes("system") && roles[0] !== "system") {
    ctx.addIssue({ code: "custom", path: ["messages"], message: "system は先頭に" });
  }
  req.messages.forEach((m, i) => {
    if (m.role === "tool" && !m.tool_call_id) {
      ctx.addIssue({ code: "custom", path: ["messages", i], message: "tool_call_id が必要" });
    }
  });
});

export type ChatRequest = z.infer<typeof ChatRequest>;
```

`superRefine` の `path` を埋めておくと、エラーが「どのメッセージの何行目か」まで指せる。ログを読むときに効いてくる。

### SDK 側に検証を持たせるという選択肢

自前で書かずに、SDK のリクエスト構造体＋バリデーションに寄せる手もある。たとえば Go の `go-deepseek/deepseek` は Request パッケージで構造体定義と検証を提供しており、JSON ペイロードに落とす前に弾ける。言語ごとに事情が違うので、

- **公式 SDK がある言語** → SDK の型＋薄い追加検証（アプリ固有ルールのみ）
- **HTTP を直接叩く言語** → Pydantic / Zod などでスキーマを自前管理

という切り分けが現実的だ。どちらでも「アプリ固有の運用ルール」だけは自前で持つことになる。

### どの 400 をリトライし、どれをリトライしないか

検証を通り抜けたリクエストが 400 を返した場合、原因は大きく2種類ある。

| 種類 | 例 | リトライ |
|---|---|---|
| 恒久的な不備 | 未知のモデル名、値域外、スキーマ違反 | **しない**（修正して再デプロイ） |
| 一時的な不備 | レート制限、上流の一時障害、タイムアウト | する（指数バックオフ＋ジッタ） |

実装としては、HTTP ステータスだけで分岐せず、**エラー本文の種別を見て「引数起因かどうか」を判定する**のが望ましい。DeepSeek API は OpenAI 互換のエラー形式（`error.type` / `error.code` を持つ JSON）を返すため、`invalid_request_error` 系は非リトライ、それ以外はリトライ対象、という分類が書きやすい。正確なフィールド名と種別の一覧は公式ドキュメントで確認すること。

```python
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

def is_retryable(status: int, body: dict) -> bool:
    if status in RETRYABLE_STATUS:
        return True
    err = body.get("error") or {}
    if err.get("type") == "invalid_request_error":
        return False   # 引数起因は何度投げても同じ
    return status >= 500
```

### 検証ログの設計

検証で落ちたリクエストは、**修正のインプットになる**。ただしプロンプト本文をそのまま保存すると機密情報・個人情報の塊になる。最低限これだけ残せば十分だ。

- 落ちた層（構造 / 値域 / 整合性）
- フィールドパス（`messages[3].tool_call_id` など）
- パラメータの**型と長さ**（`content_len=4821` のように）
- モデル名とリクエスト ID（あれば）

本文は保存しない、あるいはハッシュのみ。これはセキュリティ記事の「ログに機密を残さない」原則と揃える。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| `max_token` などのタイポが 400 になるまで気づかない | `extra="forbid"` / `.strict()` で未知フィールドを拒否する |
| `temperature` に 2.5 を渡して 400 | 値域を定数化し、`Field(ge=..., le=...)` で宣言的に弾く |
| `messages` が空配列で 400 | `min_length=1` / `.min(1)` を付ける |
| `role=tool` で `tool_call_id` を忘れる | `model_validator` で role ごとの必須項目を検査 |
| 最後が `assistant` のリクエストが意図せず続き生成になる | 明示フラグが無い限り弾く（prefix completion は別経路に） |
| 400 をリトライし続けて課金とレイテンシが膨らむ | エラー種別でリトライ可否を分岐させる |
| モデル追加・上限変更で検証が古くなる | 上限値を定数／設定ファイルに集約し、更新手順をREADMEに書く |

## まとめ

- 引数検証は「転送層 / 構造 / 値域 / 整合性」の4層に分けると保守しやすい
- 型で表せるものはスキーマ（Pydantic・Zod）に、パラメータ間の矛盾は明示的なバリデータに書く
- 未知フィールドの拒否（`extra="forbid"` / `.strict()`）はタイポ検出に最も費用対効果が高い
- 400 は「引数起因＝非リトライ」「一時障害＝リトライ」で分岐させる
- 検証ログはフィールドパスと型・長さだけ残し、本文は残さない

## チェックリスト

- [ ] 値域・上限を定数／設定ファイルに外出しした
- [ ] 未知フィールドを拒否する設定を入れた
- [ ] `messages` の空配列・role ごとの必須項目を検証している
- [ ] パラメータ間の整合性（`stream`、tool、prefix completion）を検証している
- [ ] 400 のリトライ可否をエラー種別で分岐させた
- [ ] 検証ログにプロンプト本文を残していない
- [ ] 上限値の更新手順をドキュメントに書いた
