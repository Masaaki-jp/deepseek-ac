---
title: "DeepSeek APIの再試行設計 — リトライしてよい失敗とダメな失敗の分け方"
description: "DeepSeek APIの再試行は、通信の失敗だけを対象にしないと課金と遅延が膨らみます。リトライ可否の判定、指数バックオフ＋ジッター、総予算、ストリーミングと内容側失敗の扱いをPython/JSのコードで整理します。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - JavaScript
  - エラーハンドリング
publishedAt: 2026-10-21
draft: false
series: "DeepSeek APIの実装"
difficulty: intermediate
minutes: 12
---

## リード

DeepSeek API の呼び出しは、失敗する前提で組む必要があります。この記事では失敗を「通信側」と「内容側」に分け、リトライしてよい条件、待ち時間の設計、総予算の切り方、ストリーミングとツール呼び出しの注意点をコード付きで整理します。

## 前提

- 想定読者: DeepSeek API を本番コードに組み込み始めたエンジニア
- 必要な環境: Python 3.10+ または Node.js 18+、`openai` SDK（DeepSeek は OpenAI 互換エンドポイント）
- 前提知識: HTTP ステータスコード、指数バックオフの基本

> 本記事のステータスコードやヘッダの扱いは代表例です。DeepSeek の最新仕様は[公式ドキュメント](https://api-docs.deepseek.com/)で要確認してください。

## 失敗を2種類に分ける

再試行設計の出発点は「失敗を混ぜて扱わないこと」です。

| 種類 | 例 | リトライ |
|---|---|---|
| 通信側の失敗 | 接続できない、タイムアウト、429、5xx | してよい |
| 内容側の失敗 | 空応答、JSON でない、質問と無関係、事実誤り | してはいけない |

通信側は時間をおけば直る可能性があります。一方、内容側は同じ入力を送る限り結果は大きく変わりません。ここを混ぜて「エラーなら3回リトライ」と作ると、直らない失敗にトークン課金を払い続けることになります。

## リトライ可否の判定

判定は例外の型とステータスコードで行います。代表的な分類は次のとおりです。

| ステータス | 意味 | リトライ |
|---|---|---|
| 400 | リクエスト不正 | しない |
| 401 / 403 | 認証・権限 | しない |
| 402 | 残高不足（DeepSeek 固有の可能性あり・要確認） | しない |
| 408 / 409 | タイムアウト・競合 | する |
| 429 | レート制限 | する（間隔を空ける） |
| 500 / 502 / 503 / 504 | サーバ側障害 | する |

```python
import httpx
from openai import APIStatusError, APITimeoutError, APIConnectionError

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (APITimeoutError, APIConnectionError,
                        httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in RETRYABLE_STATUS
    return False
```

## 指数バックオフ＋ジッター

待ち時間は回ごとに伸ばし、必ずジッター（揺らぎ）を入れます。全クライアントが同じ間隔で再送すると、復旧しかけたサーバに再度負荷をかけるためです。

```python
import random
import time

def backoff_delay(attempt: int, base: float = 0.8, cap: float = 20.0) -> float:
    delay = min(cap, base * (2 ** attempt))       # 0.8s → 1.6s → 3.2s ...
    return delay * (0.5 + random.random() / 2)     # 50〜100%のジッター
```

試行回数は 3〜4 回で十分です。それで成功しないなら一時的な問題ではない可能性が高く、記録して人に渡すほうが早く片付きます。

## 総予算（total budget）で打ち切る

「最大4回」だけでは、1回あたりが遅い場合に全体が数十秒伸びます。経過時間の上限を別に持ちます。

```python
def call_with_retry(client, *, max_attempts=4, total_budget=60.0, **kwargs):
    started = time.monotonic()
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc):
                raise
            if attempt == max_attempts - 1:
                break
            delay = backoff_delay(attempt)
            if time.monotonic() - started + delay > total_budget:
                break
            time.sleep(delay)
    raise last_exc
```

予算超過時は例外を上げるか、フォールバック（短いモデル・キャッシュ・定型文）に切り替えます。ユーザーを無限に待たせないことが優先です。

> `openai` Python SDK はデフォルトで自動リトライ（`max_retries=2`）を持ちます。自前実装と二重にならないよう、`OpenAI(max_retries=0)` にして自前制御に寄せるか、SDK に完全委任するかを決めてください。DeepSeek エンドポイントでの挙動は SDK バージョン依存のため要確認です。

## JavaScript での実装

```js
const RETRYABLE = new Set([408, 409, 429, 500, 502, 503, 504]);

async function withRetry(fn, {
  maxAttempts = 4, base = 800, cap = 20000, budgetMs = 60000,
} = {}) {
  const started = Date.now();
  let lastErr;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      const status = err?.status ?? err?.response?.status;
      const retryable = status
        ? RETRYABLE.has(status)
        : ["ECONNRESET", "ETIMEDOUT", "ENOTFOUND"].includes(err?.code);
      if (!retryable) throw err;
      if (attempt === maxAttempts - 1) break;
      const delay = Math.min(cap, base * 2 ** attempt) * (0.5 + Math.random() / 2);
      if (Date.now() - started + delay > budgetMs) break;
      await new Promise((r) => setTimeout(r, delay));
    }
  }
  throw lastErr;
}
```

## ストリーミングは再試行しない

ストリーミングで途中まで受信した後に再試行すると、ユーザーには同じ文章が二重に見えます。部分受信済みのストリームは再試行対象外にし、再生成は UI 側の明示操作にしてください。

- 接続確立前の失敗 → 再試行してよい
- 最初のチャンク受信後の失敗 → 再試行せず、途中までの出力を確定させて「続きを生成」ボタンを出す

## 内容側の失敗は「リペア」で扱う

JSON パース失敗などはリトライではなく、1回だけ修復リクエストを投げます。まずは `response_format` で構造化を強制するのが先です。

```python
import json

def parse_or_repair(client, raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        fixed = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "入力をJSONのみに修正してください。説明は不要です。"},
                {"role": "user", "content": raw},
            ],
            response_format={"type": "json_object"},
        )
        return json.loads(fixed.choices[0].message.content)
```

事実誤りや無関係な回答はリペアでは直りません。検証（引用元との照合、ツールでの裏取り）か、「分からない」と答えるプロンプト設計で対処します。

## 二重実行を防ぐ（ツール呼び出し）

LLM 呼び出し自体は読み取り専用なので再試行しても安全です。危険なのは、モデルが返したツール呼び出し（決済・メール送信・DB 更新）をアプリ側で実行する場合です。ここは LLM のリトライとは別に、ツール側で冪等キーを持たせます。

```python
import hashlib

idempotency_key = hashlib.sha256(f"{run_id}:{step_id}".encode()).hexdigest()
# 外部APIへ渡し、同じキーの再実行を弾いてもらう
```

## 連続失敗を止める（サーキットブレーカー）

429 や 5xx が続く場合、リトライを続けるほど状況が悪化します。直近 N 回の失敗率が閾値を超えたら一定時間呼び出しを止め、即座にフォールバックを返す仕組みを入れます。

- 失敗率 50% 超が 30 秒続いたら 60 秒間は新規リクエストを送らない
- その間はキャッシュ・定型文・別モデルで応答
- 復帰は半開状態（少量のリクエストで試す）から

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 429 が連発する | 指数バックオフ＋ジッター、並列度を下げる。`Retry-After` がある場合は従う（有無は要確認） |
| リトライで課金が増えた | 内容側の失敗をリトライしていないか確認。生成完了分は課金対象になり得る |
| ストリーミングで文が重複する | 部分受信済みを再試行している。UI 側の再生成に切り替える |
| 401/402 が延々リトライされる | 恒久エラー。判定関数から除外し、即座に失敗させる |
| ツールが二重実行された | ツール側に冪等キー。LLM リトライと業務処理リトライを分離 |
| 全体レイテンシが読めない | 総予算を設定し、超過時はフォールバックに倒す |
| SDK の自動リトライと二重になる | `max_retries=0` にして自前制御に統一する |

## まとめ

- リトライ対象は通信側の失敗だけ。内容側はリペアか検証で扱う
- 待ち時間は指数バックオフ＋ジッター、回数は 3〜4 回、総予算で打ち切る
- ストリーミングとツール呼び出しは別設計。部分受信の再試行と二重実行を避ける
- 連続失敗時はサーキットブレーカーで止め、フォールバックを返す

## チェックリスト

- [ ] リトライ可否をステータスコードと例外型で判定している
- [ ] 指数バックオフにジッターを入れている
- [ ] 最大試行回数と総予算の両方を設定している
- [ ] ストリーミングの部分受信を再試行対象から外している
- [ ] 内容側の失敗をリトライではなくリペア／検証で扱っている
- [ ] ツール呼び出しに冪等キーを付けている
- [ ] SDK の自動リトライと自前実装が二重になっていない
- [ ] 429/5xx 継続時のサーキットブレーカーとフォールバックがある
