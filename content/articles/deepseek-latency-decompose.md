---
title: "DeepSeek APIのレイテンシを分解する ― TTFT・TPOT・E2Eで測る改善"
description: "「なんとなく遅い」を捨て、TTFT・TPOT・E2Eの3指標に分解してDeepSeek APIのレイテンシを測る方法を解説。計測コード、ホスト側／プロバイダ側の切り分け、指標別の打ち手までを実装目線でまとめます。"
category: operations
tags:
  - DeepSeek
  - API
  - Python
  - レイテンシ
  - パフォーマンス
publishedAt: 2026-11-06
draft: false
series: "コスト・性能"
difficulty: intermediate
minutes: 13
---

## リード

DeepSeek API が「遅い」と感じたとき、UIのタイマーや体感だけで判断すると誤診します。本記事ではレイテンシを TTFT（首トークン）／TPOT（トークンあたり）／E2E（全体）に分解し、Python で測るコードと、指標ごとの打ち手を整理します。

## 前提

- 想定読者: DeepSeek API を本番／検証環境で呼び出しているエンジニア
- 必要な環境: Python 3.10+、`openai` SDK（DeepSeek の OpenAI 互換エンドポイント経由）、任意で `httpx`
- 前提知識: ストリーミングレスポンス（SSE）の基本的な読み方
- 注意: エンドポイント・モデル名・料金・パラメータ仕様は変更が速い領域です。本記事のコードは構造の説明が目的なので、実装前に公式ドキュメントで要確認してください

## 本文

### なぜ「体感が遅い」だけでは改善できないのか

レイテンシの悪化は最低でも3つの層で起きます。

1. ホスト側の送信前処理（プロンプト組み立て、DB、直列化）
2. プロバイダ側（キュー待ち、prefill、decode、混雑）
3. ホスト側の受信後処理（プロキシのバッファリング、SSEパース、UI描画）

この3層が1つの数字に潰れていると、「モデルが遅い」のか「自分のアプリが遅い」のか区別できません。参考として、ある上流の rc.7 報告では UI 上の TTFT 中央値が `49.7s` だったのに対し、同じリクエストをゲートウェイ側で測ると中央値 `2.1s` でした。**これはその構成固有の数値**で一般化はできませんが、「UIのタイマーだけで判断するな」という教訓はそのまま使えます。

### 3指標の定義を揃える（TTFT / TPOT / E2E）

まず用語を固定します。チーム内で定義がずれると、計測結果が比較できません。

| 指標 | 定義 | 主に何を映すか |
|---|---|---|
| TTFT | リクエスト送信 → **最初の非空トークン**到達 | キュー待ち・prefill・ネットワーク・プロキシ |
| TPOT | (E2E − TTFT) ÷ (出力トークン数 − 1) | decode 速度・バッチ混雑 |
| E2E | リクエスト送信 → 最終トークン到達 | ユーザーの実待ち時間 |
| 出力スループット | 出力トークン数 ÷ (E2E − TTFT) | TPOT の逆数。レポート用 |

TPOT は「2トークン目以降の平均間隔」です。`出力トークン数 - 1` で割るのは、1トークン目が TTFT に含まれているためです。

### 計測の落とし穴：タイマーの起点と空delta

TTFT の測定で最も多いバグは2つです。

- 起点を「接続後」や「最初のチャンク受信」に置いてしまう → ネットワークとキュー待ちが消える
- 最初のチャンク＝最初のトークンとみなす → SSE の先頭には `role` だけの空 delta が来ることがあり、TTFT を過小評価する

正しくは、**リクエスト送出直前を `t0` に置き、`content` が非空になった瞬間を TTFT とする**。チャンク数はトークン数とは一致しないので、トークン数は `usage` から取ります（ストリーミング時の `usage` 取得方法は API バージョンで挙動が変わるため公式ドキュメントで要確認。取得できない場合は非ストリーミングで別途トークン数を測るか、トークナイザで概算します）。

### Python + OpenAI SDK でTTFTを測る

最小構成の計測スクリプトです。`time.perf_counter()` を使い、壁時計の変更に影響されないようにします。

```python
import os
import statistics
import time

from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

def measure_once(prompt: str, model: str = "deepseek-chat", max_tokens: int = 256) -> dict:
    t0 = time.perf_counter()
    t_first = None
    text_parts: list[str] = []

    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        stream=True,
    )

    for chunk in stream:
        if not chunk.choices:
            continue
        content = getattr(chunk.choices[0].delta, "content", None)
        if content:  # 空 delta を除外するのがポイント
            if t_first is None:
                t_first = time.perf_counter()
            text_parts.append(content)

    t_end = time.perf_counter()
    return {
        "ttft": (t_first - t0) if t_first else None,
        "e2e": t_end - t0,
        "chars": len("".join(text_parts)),
    }

def summarize(rows: list[dict]) -> dict:
    ttfts = [r["ttft"] for r in rows if r["ttft"] is not None]
    e2es = [r["e2e"] for r in rows]
    return {
        "n": len(rows),
        "ttft_median": statistics.median(ttfts),
        "ttft_p95": statistics.quantiles(ttfts, n=20)[-1] if len(ttfts) >= 20 else None,
        "e2e_median": statistics.median(e2es),
    }
```

運用上のコツ:

- **ウォームアップを1回流して捨てる**（TLS ハンドシェイク、コネクション確立を除外）
- **n ≥ 20** を目安に中央値と p95 を出す。1回の計測で判断しない
- **同一プロンプト・同一 `max_tokens`** で比較する。出力長が違うと E2E は比較不能
- 時刻をずらして複数回測る（プロバイダ側の混雑は時間帯依存）

### セグメント分解：host outbound / provider / host inbound

SDK 経由だと「送信前」「プロバイダ内」「受信後」が分離できません。切り分けたいときは `httpx` で生の SSE を読み、3点のタイムスタンプを取ります。

```python
import json
import os
import time

import httpx

def measure_segments(payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}",
        "Content-Type": "application/json",
    }
    t0 = time.perf_counter()          # host outbound 開始
    t_headers = t_first_byte = t_first_token = None

    with httpx.Client(timeout=120) as c:
        with c.stream(
            "POST",
            "https://api.deepseek.com/chat/completions",
            json={**payload, "stream": True},
            headers=headers,
        ) as r:
            t_headers = time.perf_counter()   # レスポンスヘッダ到着
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                now = time.perf_counter()
                if t_first_byte is None:
                    t_first_byte = now
                obj = json.loads(data)
                choices = obj.get("choices") or []
                if choices and choices[0].get("delta", {}).get("content"):
                    t_first_token = now
                    break

    t_end = time.perf_counter()
    return {
        "host_outbound": t_headers - t0,
        "queue_and_prefill": (t_first_token - t_headers) if t_first_token else None,
        "to_first_byte": (t_first_byte - t0) if t_first_byte else None,
        "total": t_end - t0,
    }
```

読み方の例:

- `host_outbound` が大きい → DNS・プロキシ・egress・リクエストボディの肥大（長いプロンプトの送信自体が重い）
- `queue_and_prefill` が大きい → プロバイダ側のキュー待ち or prefill コスト（入力長に比例するか確認）
- `to_first_byte` は小さいのに `queue_and_prefill` が大きい → ストリーミング自体は正常、生成待ちが支配的
- SDK 計測と大きく乖離する → プロキシ／WAF／リバースプロキシのバッファリングを疑う

### 同時実行数とセッションサイズを独立に振る

「遅い」の原因を切り分けるには、2つの変数を**同時に動かさない**のが原則です。

1. 入力長だけを変える（出力長・同時実行数は固定）→ TTFT が比例して伸びるなら prefill 支配
2. 出力長だけを変える（入力長は固定）→ E2E だけ伸びて TTFT が横ばいなら decode 支配
3. 同時実行数だけを変える → TTFT・TPOT が両方悪化するならキュー／レート制限の影響

特にエージェント構成では、会話履歴が伸びるほど入力トークンが増え、TTFT が悪化します。「新しいセッションは速いが、長期セッションだけ遅い」なら、モデルではなく**プロンプト長と履歴の積み上がり**が主因である可能性が高いです。

### 改善の打ち手を指標ごとに切り分ける

| 症状 | 主因候補 | 打ち手 |
|---|---|---|
| TTFT が入力長に比例して伸びる | prefill コスト | プロンプト圧縮、履歴の要約、固定プレフィックスの再利用（キャッシュ） |
| TTFT が入力長と無関係に伸びる | キュー・ネットワーク・プロキシ | コネクション再利用、リトライ設計見直し、プロキシのバッファリング無効化、時間帯を変えて再測 |
| TPOT が遅い | decode 速度・混雑 | 同時実行数の制御、`max_tokens` 制限、重要リクエストの優先度分離 |
| E2E だけ遅い | 出力が長すぎる | 出力形式の指定（箇条書き・JSON）、`max_tokens` 上限、ストリーミングで体感改善 |
| UI だけ遅い | クライアント側 | 描画のスロットリング、SSE パースの同期処理、フロントのバッファリング確認 |

ストリーミングは E2E を短くしません。TTFT を体感の待ち時間に変換するだけです。**「E2E を短くする」と「体感を良くする」は別の施策**だと分けて考えると、優先順位が付けやすくなります。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| TTFT が毎回ばらついて比較できない | n≥20 で中央値・p95 を取る。ウォームアップを捨てる。時間帯を分けて再測 |
| 空 delta を1トークン目と数えてしまう | `delta.content` が非空の最初のチャンクのみを TTFT とする |
| チャンク数をトークン数として TPOT を計算してしまう | `usage.completion_tokens` を使う。取得できない場合は非ストリーミングで別途計測 |
| SDK 計測と curl 計測で結果が大きく違う | プロキシ／WAF／HTTPクライアントのバッファリングを疑う。`httpx` の生SSEで再測 |
| 長いセッションだけ遅い | 入力トークン数の推移をログに出す。履歴要約・キャッシュを検討 |
| 特定モデルだけ遅い | モデル名・バージョン・リージョンを固定して比較。仕様は公式ドキュメントで要確認 |

## まとめ

- レイテンシは TTFT / TPOT / E2E に分解すると、原因の層（ホスト送信・プロバイダ・ホスト受信）を切り分けられる
- TTFT は「リクエスト送出 → 最初の非空 delta」で測る。空 delta とチャンク数の誤用が最大の罠
- 入力長・出力長・同時実行数を**独立に**振ることが、診断の最短ルート
- UI のタイマーは当てにしない。ゲートウェイ側の数字と突き合わせる
- ストリーミングは E2E ではなく体感を改善する施策。混同しない

## チェックリスト

- [ ] TTFT / TPOT / E2E の定義をチーム内で文書化した
- [ ] 計測は n≥20、ウォームアップ除外、中央値と p95 で記録している
- [ ] 空 delta を除外する実装になっている
- [ ] トークン数は `usage` から取得している（チャンク数ではない）
- [ ] 入力長・出力長・同時実行数を独立に振る実験を一度は実施した
- [ ] ホスト側の送信前後処理を計測範囲に含めている
- [ ] モデル名・エンドポイント・パラメータ仕様を公式ドキュメントで最終確認した
