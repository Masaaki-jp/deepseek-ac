---
title: "DeepSeek APIのタイムアウトと部分失敗｜二重実行を避けるリトライ設計"
description: "タイムアウトは失敗ではなく「結果不明」。読み取りタイムアウト後の再送で二重生成・二重課金を起こさないために、操作レコード・エラー分類・部分成功の扱いをどう設計するかをコード付きで整理する。"
category: troubleshooting
tags:
  - DeepSeek
  - API
  - Python
  - リトライ
  - 冪等性
publishedAt: 2026-10-25
draft: false
series: "DeepSeek APIのトラブルシューティング"
order: 1
difficulty: intermediate
minutes: 12
---

## リード

DeepSeek API のタイムアウトは「失敗した」ではなく「結果が分からない」状態です。読み取りタイムアウト後の再送で二重生成・二重課金を起こさないための設計と、ストリーム切断のような部分失敗を成功扱いにしない実装を扱います。

## 前提

- 想定読者: DeepSeek API をバッチ処理や本番フローに組み込んでいるエンジニア
- 必要な環境: Python 3.10 以上、OpenAI 互換 SDK（`openai`）、`DEEPSEEK_API_KEY`
- 前提知識: HTTP ステータスコード、指数バックオフ、冪等性の考え方

> 本記事の DeepSeek 固有仕様（エラーコード体系、冪等キーの有無、`stream_options` の対応状況、料金）は変更され得ます。実装前に公式ドキュメントで要確認してください。

## 本文

### タイムアウトを3つに分ける

「タイムアウトした」と一括りにすると対処を誤ります。少なくとも次の3つは別物です。

| 種類 | サーバー側の状態 | 再送の意味 |
|---|---|---|
| 接続タイムアウト | リクエストが届いていない可能性が高い | 比較的安全 |
| 読み取りタイムアウト | 生成が完了している可能性がある | 二重実行になり得る |
| ストリーム途中の切断 | 一部だけ生成済み | 部分成功として扱う |

危険なのは2つ目です。クライアントは応答を受け取っていないだけで、サーバー側では生成と課金が済んでいるかもしれません。つまりタイムアウトは「失敗」ではなく「結果不明」であり、再送は「リトライ」ではなく「新しい実行」になり得ます。

### リトライ可否はステータスコードで決める

- 再送する: 429 / 500 / 502 / 503 / 504、接続エラー
- 再送しない: 400 / 401 / 402（残高不足） / 422

429 では `Retry-After` ヘッダがあればそれに従います。無い場合は指数バックオフにジッタを足してください。ジッタ無しで全ワーカーが同じ間隔で再送すると、復旧直後に再度レート制限へ突っ込みます。

DeepSeek の具体的なエラーコード体系は公式ドキュメントで要確認です。ステータスコードだけを見て分類する実装にしておけば、体系が変わっても壊れにくくなります。

### 操作レコードで「意図」と「HTTP試行」を分ける

ユーザーの「この生成を1回行う」という意図に `op_id` を振り、HTTP の試行回数とは別に記録します。同じ `op_id` に対する再送は、結果キャッシュがあればそれを返すだけで済みます。

```sql
CREATE TABLE IF NOT EXISTS operations (
  op_id        TEXT PRIMARY KEY,
  status       TEXT NOT NULL,   -- running / done / partial / failed
  result_json  TEXT,
  partial_text TEXT,
  updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  op_id      TEXT NOT NULL,
  attempt    INTEGER NOT NULL,
  outcome    TEXT NOT NULL,     -- ok / timeout_unknown / connection / 429 / 4xx / 5xx
  created_at TEXT NOT NULL
);
```

`attempts` を残す理由は、後から「どこで何回不明な実行が起きたか」を追えるようにするためです。タイムアウトした試行も課金され得るので、コスト調査は成功回数ではなく試行単位で見ます。

### 実装：SDK内蔵リトライを切って自前で制御する

SDK の自動リトライと自前リトライを併用すると、試行回数が掛け算になり、二重実行の窓が広がります。ここでは `max_retries=0` にして制御を一本化します。

```python
import os, time, random, json, sqlite3
from openai import (
    OpenAI, APITimeoutError, APIConnectionError,
    RateLimitError, APIStatusError,
)

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
    timeout=60.0,
    max_retries=0,  # リトライは下の関数に一本化する
)

RETRYABLE = {429, 500, 502, 503, 504}

def backoff(attempt: int, base: float = 1.5) -> None:
    time.sleep(min(base ** attempt, 30) * (0.5 + random.random()))

def call_once(conn: sqlite3.Connection, op_id: str, payload: dict, max_attempts: int = 4):
    row = conn.execute(
        "SELECT status, result_json FROM operations WHERE op_id = ?", (op_id,)
    ).fetchone()
    if row and row[0] == "done":
        return json.loads(row[1])  # 再送せずキャッシュを返す

    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.chat.completions.create(**payload)
            conn.execute(
                "INSERT OR REPLACE INTO operations VALUES (?, 'done', ?, NULL, datetime('now'))",
                (op_id, resp.model_dump_json()),
            )
            conn.commit()
            return resp

        except RateLimitError as e:  # APIStatusError より先に捕捉する
            retry_after = e.response.headers.get("retry-after")
            _log(conn, op_id, attempt, "429")
            time.sleep(float(retry_after) if retry_after else backoff(attempt))

        except APITimeoutError:
            # 結果不明。完了済みかもしれないが確認手段が無いので記録して再送する
            _log(conn, op_id, attempt, "timeout_unknown")
            backoff(attempt)

        except APIConnectionError:
            _log(conn, op_id, attempt, "connection")
            backoff(attempt)

        except APIStatusError as e:
            _log(conn, op_id, attempt, str(e.status_code))
            if e.status_code not in RETRYABLE:
                raise  # 400/401/402/422 は再送しても結果は変わらない
            backoff(attempt)

    raise RuntimeError(f"op_id={op_id} は {max_attempts} 回で完了しませんでした")
```

ポイントは捕捉順です。`RateLimitError` は `APIStatusError` のサブクラスなので、逆順に書くと 429 が汎用ハンドラに吸われて `Retry-After` を読めません。

なお、DeepSeek API がリクエスト単位の冪等キーを受け付けるかは公式ドキュメントで要確認です。非対応なら、上記の `op_id` キャッシュが実質的な重複防止になります。

### ストリーミングの部分失敗は finish_reason で判定する

ストリームが途中で切れると、本文は尻切れのまま返ってきます。これを成功扱いにすると後工程（要約・DB保存・公開）まで壊れます。完結判定は `finish_reason` で行います。

```python
def stream_text(conn, op_id: str, payload: dict) -> tuple[str, str | None]:
    chunks: list[str] = []
    finish = None
    stream = client.chat.completions.create(**payload, stream=True)

    for ch in stream:
        if not ch.choices:
            continue
        delta = ch.choices[0].delta.content
        if delta:
            chunks.append(delta)
        if ch.choices[0].finish_reason:
            finish = ch.choices[0].finish_reason

    text = "".join(chunks)
    if finish != "stop":
        # 部分成功。捨てずに退避し、status を partial にする
        conn.execute(
            "INSERT OR REPLACE INTO operations VALUES (?, 'partial', NULL, ?, datetime('now'))",
            (op_id, text),
        )
        conn.commit()
    return text, finish
```

`finish_reason` が `length` なら `max_tokens` 不足、`None` なら通信断です。どちらも「生成本文はあるが完結していない」状態なので、再送するか部分結果を使うかを呼び出し側が選べるようにします。

ストリームでトークン使用量を取得したい場合は `stream_options={"include_usage": true}` を渡す形が OpenAI 互換 API の一般的な做法ですが、DeepSeek 側の対応状況は公式ドキュメントで要確認です。取得できない場合、使用量は成功した最終応答からしか分かりません。

### 部分成功を成功として返さない

「一部の処理がタイムアウトしたが握り潰して成功を返す」実装は、AI パイプラインでは特に危険です。S3 のマルチパートアップロードで一部パートがタイムアウトしたのに成功を返し、末尾だけ保存される、といった不具合が実際に報告されています。生成 AI でも同じ構造の事故が起きます。

- 要約が半分だけのまま DB に入る
- ツール呼び出しの途中結果だけが会話履歴に残る
- usage が欠落したままコスト集計から漏れる

対策は単純で、成果物に `status` を持たせ、`done` 以外は下流に流さないことです。`partial` を許容するのは、人間が確認する経路（下書き保存、再実行 UI）だけに限定します。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 同じ記事・同じ回答が2つ生成される | `op_id` を発行し、再送前に結果キャッシュを確認する |
| タイムアウト後に再送したら利用量が想定の2倍 | 読み取りタイムアウトは「結果不明」。試行単位でログを残し、コストは成功回数ではなく試行回数で見る |
| ストリームの本文が尻切れのまま保存される | `finish_reason == "stop"` を完結条件にし、それ以外は `partial` として退避 |
| 429 が連続し、リトライでさらに悪化する | `Retry-After` を尊重し、指数バックオフにジッタを足す。並列度も下げる |
| SDK のリトライと自前リトライで回数が想定より多い | `max_retries=0` にして制御を一本化する |
| 402 や 401 で延々とリトライし続ける | 非再試行コード（400/401/402/422）は即座に例外を投げる |

## まとめ

- タイムアウトは失敗ではなく「結果不明」。特に読み取りタイムアウトの再送は新しい実行になり得る
- ユーザーの意図（`op_id`）と HTTP 試行を分けて記録すると、重複防止とコスト調査の両方が成立する
- 部分成功は握り潰さず `partial` として明示し、下流には `done` だけを流す

## チェックリスト

- [ ] タイムアウトを接続 / 読み取り / ストリーム断の3種類に分類している
- [ ] 非再試行ステータス（400/401/402/422）で即座に失敗させている
- [ ] `op_id` 単位の結果キャッシュを持ち、再送前に参照している
- [ ] SDK 内蔵リトライを無効化し、リトライ制御を一本化している
- [ ] ストリームは `finish_reason` で完結判定している
- [ ] 試行ログを残し、失敗試行を含めたコストを追えるようにしている
