---
title: "DeepSeek APIのリトライ設計：指数バックオフとジッターを正しく実装する"
description: "429や5xxで失敗するDeepSeek API呼び出しを、安全に再試行する設計をまとめました。リトライ可否の判定、フルジッター付き指数バックオフ、Retry-Afterの尊重、全体デッドライン、ストリーミング時の二重課金対策までをPython/TypeScriptのコードで示します。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - TypeScript
  - レート制限
publishedAt: 2026-10-24
draft: false
series: "DeepSeek APIの実装"
order: 5
difficulty: intermediate
minutes: 12
---

## リード

DeepSeek API の呼び出しを「とりあえず while ループで再試行」すると、障害時に全クライアントが同時に叩きに行き、30秒の不調が30分の停止に育ちます。この記事では、リトライしてよいエラーの見分け方、フルジッター付き指数バックオフの計算式、全体デッドラインの持ち方、そして LLM API 特有の「再試行＝二重課金」問題への対処を、動くコードで示します。

## 前提

- 想定読者: DeepSeek API を本番またはバッチで呼ぶエンジニア
- 必要な環境: Python 3.10+ / `httpx`、または Node.js 18+（`fetch`）
- 前提知識: HTTP ステータスコード、`async/await` の読み方

> 本記事のエラーコード分類は一般的な HTTP の挙動に基づきます。DeepSeek API が返すステータスコードの正式な一覧・`Retry-After` ヘッダの有無・冪等キーヘッダのサポート状況は、必ず公式ドキュメントで確認してください（本記事では断定しません）。

## なぜ単純なリトライループが危険か

リトライは「失敗したらもう一度呼ぶ」だけに見えて、実際には次の3つを同時に設計する必要があります。

1. **いつ再試行するか**（エラー種別の判定）
2. **どれだけ待つか**（バックオフとジッター）
3. **いつ諦めるか**（試行回数と全体デッドライン）

このうち 2 を等間隔（例: 1秒固定）にすると、レート制限や過負荷で失敗した瞬間、待機時間が揃ったクライアントが一斉に再突撃します。これが「リトライストーム」で、サーバー側の復旧を妨げます。対策が指数バックオフ＋ジッターです。

## リトライしてよいエラー／ダメなエラー

まず判定表を固定します。ここを間違えると、認証エラーを5回繰り返して無駄に遅くなるだけの実装になります。

| ステータス | 意味の例 | リトライ |
|---|---|---|
| 408 / 425 | タイムアウト、Too Early | する |
| 429 | レート制限 | する（`Retry-After` があれば優先） |
| 500 / 502 / 503 / 504 | サーバー側の一時障害・過負荷 | する |
| 400 / 422 | リクエスト形式・パラメータ不正 | しない |
| 401 / 403 | 認証・権限 | しない |
| 402 | 残高・支払い関連 | しない（チャージしない限り永遠に失敗） |
| 404 | エンドポイント・モデル名の誤り | しない |

ネットワーク例外（接続失敗、読み取りタイムアウト、コネクション切断）はリトライ対象です。ただし **リクエスト送信後に切断された場合、サーバー側では処理が完了している可能性がある** 点は後述します。

## 指数バックオフの計算式

基本形は次のとおりです。

```
delay = min(cap, base * 2^attempt) + jitter
```

- `base`: 初回の待機（0.25〜1秒程度）
- `cap`: 待機の上限（20〜60秒程度）
- `attempt`: 0 始まりの試行番号

そしてジッターには方式があります。AWS の記事で知られる **フルジッター**は、指数部分をそのまま上限とした一様乱数にします。

```
delay = random(0, min(cap, base * 2^attempt))
```

フルジッターは「待機時間のばらつき」を最大化でき、実装も1行で済むため、まずこれを選べば問題ありません。平均待機は指数バックオフより短くなるので、レイテンシ面でも有利です。

## Python実装（標準ライブラリ＋httpx）

外部依存を増やさない素朴な実装です。ポイントは「全体デッドライン」「`Retry-After` の尊重」「非リトライエラーは即座に例外」の3点です。

```python
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After は秒数 or HTTP-date。解釈できなければ None。"""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def full_jitter(attempt: int, base: float = 0.5, cap: float = 20.0) -> float:
    return random.uniform(0.0, min(cap, base * (2 ** attempt)))


def post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    headers: dict,
    payload: dict,
    max_attempts: int = 5,
    base: float = 0.5,
    cap: float = 20.0,
    deadline: float = 90.0,
) -> httpx.Response:
    started = time.monotonic()
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        if time.monotonic() - started > deadline:
            break
        try:
            resp = client.post(url, headers=headers, json=payload)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last_error = e
        else:
            if resp.status_code < 400:
                return resp
            if resp.status_code not in RETRYABLE_STATUS:
                resp.raise_for_status()  # 400/401/402 などは即失敗
            last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            wait = parse_retry_after(resp.headers.get("retry-after"))
            if wait is not None:
                time.sleep(min(wait, cap))
                continue

        time.sleep(full_jitter(attempt, base, cap))

    raise RuntimeError(f"retry exhausted after {max_attempts} attempts") from last_error
```

呼び出し側では、タイムアウトを用途別に明示します。LLM の応答は長いので、`read` だけ長めにします。

```python
timeout = httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)

with httpx.Client(timeout=timeout) as client:
    resp = post_with_retry(
        client,
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        payload={"model": "deepseek-chat", "messages": [{"role": "user", "content": "ping"}]},
    )
```

## tenacity を使う場合

既に `tenacity` を入れているプロジェクトなら、待機戦略だけ借りるのが安全です。

```python
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

@retry(
    retry=retry_if_exception_type((httpx.TransportError, RetryableError)),
    wait=wait_random_exponential(multiplier=0.5, max=20),
    stop=stop_after_attempt(5),
    reraise=True,
)
def call_deepseek(payload: dict) -> dict:
    ...
```

`wait_random_exponential` は「0〜指数バックオフ値の乱数」を返す **フルジッター** です。`wait_exponential_jitter` は指数値に乱数を *加算* する方式で挙動が異なるため、混同しないでください。

## TypeScript実装（fetch）

```ts
const RETRYABLE = new Set([408, 425, 429, 500, 502, 503, 504]);

type Options = {
  maxAttempts?: number;
  baseMs?: number;
  capMs?: number;
  deadlineMs?: number;
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function fullJitter(attempt: number, baseMs: number, capMs: number) {
  return Math.random() * Math.min(capMs, baseMs * 2 ** attempt);
}

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const asNumber = Number(value);
  if (Number.isFinite(asNumber)) return Math.max(0, asNumber * 1000);
  const date = Date.parse(value);
  return Number.isNaN(date) ? null : Math.max(0, date - Date.now());
}

export async function postWithRetry(
  url: string,
  body: unknown,
  headers: Record<string, string>,
  { maxAttempts = 5, baseMs = 500, capMs = 20_000, deadlineMs = 90_000 }: Options = {},
): Promise<Response> {
  const started = Date.now();
  let lastError: unknown;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (Date.now() - started > deadlineMs) break;

    let res: Response;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...headers },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(120_000),
      });
    } catch (e) {
      lastError = e; // ネットワーク系はリトライ
      await sleep(fullJitter(attempt, baseMs, capMs));
      continue;
    }

    if (res.ok) return res;
    if (!RETRYABLE.has(res.status)) {
      throw new Error(`non-retryable: HTTP ${res.status}`);
    }

    lastError = new Error(`HTTP ${res.status}`);
    const retryAfter = parseRetryAfter(res.headers.get("retry-after"));
    await sleep(retryAfter ?? fullJitter(attempt, baseMs, capMs));
  }

  throw new Error(`retry exhausted or deadline exceeded`, { cause: lastError });
}
```

`AbortSignal.timeout()` は Node 18 以降で利用できます。全体デッドライン（`deadlineMs`）と1回あたりのタイムアウトは別物なので、両方持たせます。

## LLM API 特有の注意：再試行は「二重課金」になりうる

通常の Web API と違い、LLM API のリトライには次のコストが伴います。

- **再試行した回数だけトークン課金が発生しうる**。特にストリーミングで途中まで受信してから切れた場合、そこまでのトークンは消費済みです。
- **出力は非決定的**。1回目と2回目で内容が変わるため、「同じ結果が返る」前提の冪等性は成立しません。

したがって、方針を分けます。

| 状況 | 方針 |
|---|---|
| 接続失敗・429・5xx（応答本文を受け取る前） | リトライしてよい |
| ストリーミング開始前の失敗 | リトライしてよい |
| ストリーミング途中の切断 | 自動リトライしない。部分出力を捨てるか、ユーザー操作で再生成 |
| エージェントのツール実行（決済・DB書き込み・メール送信） | ツール側に冪等キーを付けてからリトライ |

エージェント用途では「LLM 呼び出しのリトライ」と「ツール実行のリトライ」を混ぜないことが重要です。後者は副作用を持つため、リトライ前に必ず一意のリクエストIDを発行し、実行側で重複排除します。DeepSeek API 側に冪等キーヘッダが用意されているかは公式ドキュメントで確認し、無ければアプリケーション側で管理してください。

## リトライ予算とサーキットブレーカー

個々の呼び出しのリトライを固めても、上位で無限に再試行すると同じことになります。

- **リトライ予算**: 全リクエストの 10% など上限を決め、超えたらリトライせず失敗させる（"retry budget"）
- **サーキットブレーカー**: 直近 N 件の失敗率が閾値超えなら一定時間呼び出し自体を止める
- **同時実行数の制限**: レート制限の主因は同時実行数。リトライで増える分も含めてセマフォで絞る

バッチ処理では「リトライで復旧できなかったリクエストを DLQ（デッドレターキュー）に積み、後で再実行」とするのが安全です。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 429 が減らず、全体が遅くなる | ジッターなしの可能性。フルジッターに変更し、同時実行数も制限する |
| 1リクエストが数分かかる | 試行回数 × `cap` が上限になっている。全体デッドラインを設定する |
| 認証エラーで無駄に待つ | 400/401/402/404 を非リトライ対象に分類する |
| 同じ応答が二重に課金される | ストリーミング途中の切断をリトライしている。途中失敗はリトライしない |
| `Retry-After` を無視してさらに 429 | ヘッダがあればバックオフより優先する（上限 `cap` でクリップ） |
| タイムアウトが効かない | `httpx.Timeout` の `connect`/`read`/`write`/`pool` を個別に設定する |

## まとめ

- リトライは「回数」ではなく「エラー分類・待機戦略・打ち切り条件」の3点セットで設計する
- 待機は `min(cap, base * 2^attempt)` を上限としたフルジッターが第一選択。`Retry-After` があれば優先する
- LLM API では再試行が二重課金につながる。ストリーミング途中の切断と副作用あるツール実行は自動リトライしない

## チェックリスト

- [ ] リトライ対象のステータスコードを明示的に列挙した
- [ ] フルジッター付き指数バックオフを実装した（固定間隔にしていない）
- [ ] `Retry-After` ヘッダを尊重し、上限値でクリップしている
- [ ] 試行回数だけでなく全体デッドラインを設定した
- [ ] 接続・読み取りなどタイムアウトを個別に設定した
- [ ] ストリーミング途中の切断を自動リトライしていない
- [ ] 副作用のあるツール実行には冪等キーを付けている
- [ ] リトライ予算またはサーキットブレーカーを上位に置いた
