---
title: "DeepSeek APIのtemperatureとmax_tokensを理解する"
description: "temperatureは出力のばらつき、max_tokensは出力の上限。用途別の推奨値と、コード例・落とし穴を整理して、迷わず設定できるようにする。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
series: "DeepSeek APIの実装"
order: 3
difficulty: beginner
minutes: 9
publishedAt: 2026-10-14
draft: false
---

## リード

- temperature と max_tokens が「何を制御するか」を1行で言えるようになる
- 用途別の推奨値が分かり、コピペで動く Python 例を手に入れられる
- 「出力が途中で切れる」「毎回違う答えが返る」の原因が切り分けられる

## 前提

- 想定読者: DeepSeek API を呼び出し始めたエンジニア
- 必要な環境: Python 3.10+、`openai` SDK（DeepSeek は OpenAI 互換エンドポイント）
- 前提知識: Chat Completions API の基本形（`messages` の渡し方）

> 注記: パラメータの対応状況・範囲・既定値はモデルごとに異なる。最新の仕様は公式ドキュメントで要確認。

## 本文

### temperature とは何か

出力トークンの選択における「ランダム性」を調整する値。

- 低い（0〜0.3）: 同じ入力に近い答えを返しやすい。決定的・再現的
- 高い（0.8〜1.5）: ばらつきが増える。発想・バリエーション向け
- 0 でも完全な決定性は保証されない（並列計算などの影響）

**重要**: temperature は「正しさ」を制御しない。事実誤認を減らす目的で 0 にしても効かない。RAG の精度改善は検索側の問題。

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

def ask(prompt: str, temperature: float = 0.0) -> str:
    res = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    return res.choices[0].message.content
```

### max_tokens とは何か

生成する出力トークンの**上限**。超えた分は打ち切られる。

- 入力トークンは含まない。出力専用の上限
- 上限に達すると `finish_reason == "length"` になる（`"stop"` が正常終了）
- 見積もり: 日本語はおおよそ 1 文字 ≒ 1 トークン前後を目安に（厳密値はトークナイザ次第）

```python
res = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "PythonでFizzBuzzを書いて"}],
    max_tokens=256,
    temperature=0.2,
)

choice = res.choices[0]
print(choice.finish_reason)  # "stop" なら正常、"length" なら切れている
print(choice.message.content)
```

### 用途別の推奨値

| 用途 | temperature | max_tokens の目安 |
|---|---|---|
| 分類・抽出・JSON整形 | 0.0〜0.3 | 用途の出力長 + 余裕 |
| コード生成 | 0.0〜0.3 | 512〜2048 |
| 要約 | 0.3〜0.5 | 元文の 1/3 程度 |
| アイデア出し・ブレスト | 0.9〜1.3 | 512〜1024 |
| 翻訳 | 0.0〜0.3 | 原文と同程度 |

迷ったら temperature=0.3、max_tokens は想定出力の 1.5〜2 倍から始める。

### 切り分けの実践

「出力が途中で終わる」ときは、まず `finish_reason` を見る。

```python
if choice.finish_reason == "length":
    # max_tokens を上げる、または「簡潔に」と指示する
    ...
```

「毎回違う答えが返る」ときは、まず temperature を下げる。それでも揺れるなら、プロンプトの曖昧さが原因。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 出力が途中で切れる | `finish_reason == "length"` を確認し max_tokens を上げる |
| JSON が壊れる | temperature を 0 に下げる。JSON モードの利用も検討 |
| temperature=0 でも結果が揺れる | 仕様。揺れが許容できない用途は事前に評価セットで検証 |
| 入力が長くてエラー | max_tokens ではなく**入力**側のコンテキスト長超過。messages を削る |
| コストが想定より高い | max_tokens は上限であり課金対象は実出力。実際の出力長を計測する |

## まとめ

- temperature は出力のばらつき、max_tokens は出力の上限。役割が全く違う
- 用途別の推奨値を起点に、評価で詰めるのが現実的
- `finish_reason` を必ず確認するクセをつけると、切り分けが速くなる

## チェックリスト

- [ ] temperature を用途に応じて設定した
- [ ] max_tokens を想定出力の 1.5〜2 倍で設定した
- [ ] `finish_reason` をログに出している
- [ ] temperature=0 の再現性を過信していない
- [ ] パラメータの既定値・範囲を公式ドキュメントで確認した
