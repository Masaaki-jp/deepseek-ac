---
title: "レスポンスを読む（JSON構造の理解）—— APIの「返事」を分解する"
description: "DeepSeek APIのレスポンスはJSON。content以外に取りこぼしがちな usage・finish_reason・reasoning_content の読み方を、公式スキーマに基づいて分解しながら解説する。"
category: implementation
tags:
  - DeepSeek
  - API
  - JSON
  - Python
publishedAt: 2026-10-05
draft: false
series: "DeepSeek APIの実装"
order: 3
difficulty: beginner
minutes: 8
---

# レスポンスを読む（JSON構造の理解）—— APIの「返事」を分解する

## リード

DeepSeek API のレスポンスは JSON です。`choices[0].message.content` だけ取り出して満足していませんか？ 実はレスポンスには、**課金に直結する `usage`**、**出力が途中で切れたことを示す `finish_reason`**、**thinking モード特有の `reasoning_content`** など、実務で効いてくる情報が詰まっています。この記事では、公式スキーマに基づいてレスポンスを「分解して読む」視点を身につけます。

## 前提

- 想定読者: DeepSeek API を叩けるようになったが、返ってくる JSON をなんとなくで扱っているエンジニア
- 必要な環境: Python 3.10+ または Node.js 18+、`openai` SDK（DeepSeek は OpenAI 互換）
- 前提知識: REST API / JSON の基本、Python または JS の辞書・オブジェクト操作

> **注記**: 本記事は執筆時点の公式スキーマに基づきます。フィールド名・モデル名は変更されうるため、実装時は公式ドキュメントで最新情報を確認してください。

## 本文

### まずレスポンス全体を見る

`print` や `console.log` で **レスポンス全体**を一度出してみるのが最短です。`content` だけ抜き出す癖がついていると、構造を見落とします。

```python
from openai import OpenAI

client = OpenAI(
    api_key="YOUR_API_KEY",  # 環境変数から読むこと
    base_url="https://api.deepseek.com",
)

resp = client.chat.completions.create(
    model="deepseek-flash",  # または "deepseek-v4-pro"
    messages=[{"role": "user", "content": "こんにちはと返して"}],
)

print(resp.model_dump_json(indent=2))
```

### レスポンスは4つのブロックに分かれる

トップレベルは `id` / `choices` / `created` / `model` / `system_fingerprint` / `object` / `usage` で構成されます。実務で見るのはこの4ブロックです。

**1. メタ情報**

| フィールド | 意味 | 使いどころ |
|---|---|---|
| `id` | リクエストID | ログ・サポート問い合わせ |
| `model` | 実際に応答したモデル | 料金確認・再現性 |
| `created` | 生成時刻（UNIX秒） | ログ |
| `system_fingerprint` | バックエンド構成の識別子 | バージョン追跡 |
| `object` | 常に `"chat.completion"` | 型判別 |

`system_fingerprint` は**公式スキーマ上 `required`** で、通常は常に含まれます。

**2. `choices[]` — 生成結果**

| フィールド | 意味 |
|---|---|
| `choices[].message.content` | 通常の本文（nullable） |
| `choices[].message.reasoning_content` | thinking モード時のみ。最終回答の前の推論内容 |
| `choices[].message.tool_calls` | モデルが呼び出したツール（関数） |
| `choices[].finish_reason` | 停止理由 |
| `choices[].index` | 選択肢のインデックス |
| `choices[].logprobs` | `logprobs: true` 指定時のみ |

**3. `finish_reason` — 停止理由（6値）**

| 値 | 意味 | 対処 |
|---|---|---|
| `stop` | 自然な停止 / stop シーケンス到達 | そのまま |
| `length` | `max_tokens` 到達 | 出力が切れている。`max_tokens` を増やす |
| `content_filter` | フィルタで省略 | プロンプトを見直す |
| `tool_calls` | モデルがツールを呼んだ | ツール結果を返して継続 |
| `insufficient_system_resource` | 推論基盤のリソース不足で中断 | リトライ |
| `aborted` | 生成が中断された | 原因を調査 |

`length` を見落とすと、**切れた出力をそのまま後段に渡す**事故が起きます。

**4. `usage` — トークン数とコスト**

| フィールド | 意味 |
|---|---|
| `usage.prompt_tokens` | 入力トークン数 |
| `usage.completion_tokens` | 出力トークン数 |
| `usage.total_tokens` | 合計 |
| `usage.prompt_cache_hit_tokens` | キャッシュにヒットした入力トークン |
| `usage.prompt_cache_miss_tokens` | キャッシュにヒットしなかった入力トークン |
| `usage.prompt_tokens_details.cached_tokens` | 上記 hit と同じ値 |
| `usage.completion_tokens_details.reasoning_tokens` | 推論で生成されたトークン |

公式に次の関係が明記されています。

```
prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens
```

キャッシュヒット分は課金が割引されることがあります。**この2つを分けて記録するとコスト最適化の指標になります**。

### content が null になるケース

**thinking モード**では、`content` が `null` で `reasoning_content` 側に中身が入ることがあります。`content` が空だからといって失敗と決めつけず、両方と `finish_reason` を確認します。

```python
choice = resp.choices[0]
content = choice.message.content
reasoning = getattr(choice.message, "reasoning_content", None)

if content is None and reasoning:
    print("推論過程:", reasoning)
    print("推論トークン:", resp.usage.completion_tokens_details.reasoning_tokens)
```

`getattr` でガードしておくと、フィールドが無いバージョンでも落ちません。

> **thinking モードの制御**: `thinking.type` で `enabled` / `disabled`、`reasoning_effort` で `none` / `low` / `high` / `max` を指定できます。`none` で thinking 無効、`low` / `high` / `max` で有効になります（デフォルトは `high`）。
> **注意**: thinking モードでは `temperature` が効果を持ちません。`top_p` は thinking モードでは有効範囲が `0.95–1.0` にクランプされ、non-thinking モードでは `1.0` 固定で渡した値は無視されます。

> **ツール呼び出し時の `reasoning_content` について**
> ツール呼び出し（tools）を挟んで会話を継続する際、前ターンの `reasoning_content` を次のリクエストの入力メッセージに含めるべきかは、公式の Tool Calls Guide を参照してください。取り扱いは実装時に必ず確認してください。

### usage をログに残す

コスト管理の第一歩は、`usage` を毎回ログに残すことです。キャッシュ関連も含めて残すと、後で削減余地を分析できます。

```python
u = resp.usage
print(
    f"prompt={u.prompt_tokens} "
    f"completion={u.completion_tokens} "
    f"total={u.total_tokens} "
    f"cache_hit={u.prompt_cache_hit_tokens} "
    f"cache_miss={u.prompt_cache_miss_tokens} "
    f"reasoning={getattr(u.completion_tokens_details, 'reasoning_tokens', None)}"
)
```

### JavaScript の場合

JS では `resp.choices[0].message.content` のようにそのまま参照できます。存在しないフィールドは `undefined` になるため、optional chaining で守ります。

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.DEEPSEEK_API_KEY,
  baseURL: "https://api.deepseek.com",
});

const resp = await client.chat.completions.create({
  model: "deepseek-flash", // または "deepseek-v4-pro"
  messages: [{ role: "user", content: "こんにちはと返して" }],
});

const choice = resp.choices[0];
const content = choice.message?.content;
const reasoning = choice.message?.reasoning_content;
const finish = choice.finish_reason;

if (finish === "length") {
  console.warn("出力が打ち切られました");
}
if (finish === "insufficient_system_resource") {
  console.warn("リソース不足で中断。リトライを検討");
}
console.log({ content, reasoning });
```

## つまずきポイント

| 症状 | 対処 |
|---|---|
| `content` が `null` | `reasoning_content` と `finish_reason` を確認（thinking モード） |
| 出力が途中で切れる | `finish_reason == "length"` を疑い `max_tokens` を増やす |
| コストが合わない | `usage` をログに残す。`cache_hit` / `cache_miss` も分けて記録 |
| ときどき中断される | `insufficient_system_resource` を疑いリトライ |
| `temperature` が効かない | thinking モードでは無効。`reasoning_effort: none` を検討 |
| `top_p` が効かない | non-thinking では 1.0 固定、thinking では 0.95–1.0 にクランプ |
| 使ったモデルが分からない | レスポンスの `model` フィールドを見る |
| フィールドが見つからない | バージョン差の可能性。公式ドキュメントで要確認 |

## まとめ

- レスポンスは `choices`（結果）・`usage`（コスト）・メタ情報のブロックに分解できる
- `finish_reason` は6値。`length` と `insufficient_system_resource` は要対処
- thinking モードでは `content` が `null` で `reasoning_content` に入ることがある
- `prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens` の関係を押さえる
- `usage` をログに残すことがコスト管理の第一歩
- thinking モードでは `temperature` が効かず、`top_p` はクランプされる
- フィールド名・モデル名はバージョンで変わりうるため公式ドキュメントで要確認

## チェックリスト

- [ ] レスポンス全体を `model_dump_json` / `console.log` で一度出力した
- [ ] `finish_reason` をコードで確認している（6値すべて把握した）
- [ ] `usage` をログに残している（`cache_hit` / `cache_miss` も分けて記録）
- [ ] `usage.completion_tokens_details.reasoning_tokens` を確認している（thinking モード時）
- [ ] `content` が `null` のとき `reasoning_content` を見ている
- [ ] thinking モードで `temperature` が効かないことを理解している
- [ ] `system_fingerprint` をログに残している（バージョン追跡用）
- [ ] フィールド名・モデル名を公式ドキュメントで確認した
