---
title: "ターミナル地獄からの解放——DeepSeek API の前に自前ゲートウェイを1枚置く"
description: "curlとexportが増え続ける開発環境を抜けるために、DeepSeek API の手前に小さな自前ゲートウェイを置く構成を解説します。OpenAI互換のままbase_urlを差し替えるだけで、キー管理・モデル制限・使用量ログ・リトライを1か所に集約できます。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - FastAPI
  - ゲートウェイ
publishedAt: 2026-11-09
# updatedAt: 2026-11-09
draft: false
series: "DeepSeek APIの実装"
order: 8
difficulty: intermediate
minutes: 14
---

## リード

`export DEEPSEEK_API_KEY=...` を打ち直し、curlをコピペし、どのモデルにいくら使ったか分からない——この「ターミナル地獄」は、APIの手前に小さなゲートウェイを1枚置くだけで終わります。本記事では FastAPI + httpx で自前ゲートウェイを建て、クライアント側は `base_url` を差し替えるだけで移行する手順を示します。

## 前提

- 想定読者: DeepSeek API をスクリプトや小規模ツールから叩いているエンジニア
- 必要な環境: Python 3.11+、FastAPI / uvicorn / httpx、Docker か systemd（常駐用）
- 前提知識: HTTP、SSE（Server-Sent Events）、OpenAI 互換 API の基本

## 本文

### 「ターミナル地獄」の正体は3つに分解できる

自前ゲートウェイの設計を始める前に、何がつらいのかを分解しておきます。対策が変わるためです。

| 症状 | 原因 | ゲートウェイで解決できるか |
|---|---|---|
| キーを毎回 export / .env をコピーしている | 認証情報がクライアント側にある | ○ キーはゲートウェイだけが持つ |
| どのモデルを誰がどれだけ使ったか不明 | 呼び出しが分散していてログがない | ○ 通過点で usage を記録 |
| 429 / 5xx でスクリプトが落ちる | リトライが各スクリプト任せ | ○ リトライを集約 |
| 高価なモデルをうっかり呼ぶ | モデル名が自由 | ○ 許可リストで制限 |
| プロンプトとレスポンスが手元に残らない | 標準出力に流して消える | △ 設計次第（後述の注意点） |

逆に、**ゲートウェイで解決すべきでないもの**もあります。プロンプトの品質管理、RAGの検索ロジック、アプリ固有のリトライ方針などはアプリ側の責務です。ゲートウェイは「通信と統制」だけを担当させると、責務がぶれず保守できます。

### なぜ既製ツールではなく自前なのか

先に正直に書きます。**要件が「複数プロバイダの統一」「予算アラート」「管理UI」レベルなら、既製のLLMゲートウェイやプロキシサービスで足ります。** 自前で建てる価値が出るのは次のような場合です。

- 社内ネットワーク内に閉じたい（プロンプトを外部SaaSに通したくない）
- 社内の認証基盤・監査ログの形式に合わせる必要がある
- 数十行のロジックで済むので、依存を増やしたくない

本記事の構成は「100〜150行程度で読める自前実装」を狙います。これ以上複雑になったら、既製ツールへの移行を検討したほうがいいというのが実感です。

### 全体構成

```
[クライアント] --(gw用トークン)--> [自前ゲートウェイ] --(実APIキー)--> [DeepSeek API]
   OpenAI SDK                        FastAPI + httpx                  OpenAI互換
```

ポイントは、**DeepSeek API が OpenAI 互換である**という点です。したがってゲートウェイも「OpenAI 互換の `/v1/chat/completions` を受けて、上流の同名エンドポイントへ中継する」形にすれば、クライアントは SDK の `base_url` を変えるだけで移行できます。DeepSeek 側の正確なベースURLやパス（`/v1` を付けるか等）は公式ドキュメントで要確認してください。

### 実装: 最小のゲートウェイ

```python
# gateway/main.py
import json
import os
import secrets
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
UPSTREAM_KEY = os.environ["DEEPSEEK_API_KEY"]      # 実キーはここだけ
GATEWAY_TOKEN = os.environ["GATEWAY_TOKEN"]        # クライアントが使うトークン
ALLOWED_MODELS = set(os.environ.get("ALLOWED_MODELS", "deepseek-chat").split(","))

app = FastAPI()
client = httpx.AsyncClient(
    base_url=UPSTREAM_BASE,
    timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
)


def check_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not secrets.compare_digest(auth[7:], GATEWAY_TOKEN):
        raise HTTPException(status_code=401, detail="invalid gateway token")
```

認証は `secrets.compare_digest` を使います。`==` での比較はタイミング攻撃の余地があるためです（ローカル用途でも癖として直しておく価値があります）。

続いて本体。ストリーミングと非ストリーミングの両方を扱います。

```python
def extract_usage(data: bytes) -> dict | None:
    """SSE/JSON の末尾から usage オブジェクトを best-effort で拾う。"""
    idx = data.rfind(b'"usage"')
    if idx == -1:
        return None
    start = data.find(b"{", idx)
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(data[start:].decode("utf-8", "ignore"))
        return obj
    except json.JSONDecodeError:
        return None


def log_usage(meta: dict) -> None:
    # 本番は JSON Lines でファイルか stdout に。プロンプト本文は入れない。
    print(json.dumps(meta, ensure_ascii=False), flush=True)


async def relay(upstream: httpx.Response, meta: dict):
    tail = b""
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
            tail = (tail + chunk)[-4096:]   # usage は末尾に来る想定
    finally:
        await upstream.aclose()
        meta["usage"] = extract_usage(tail)
        meta["elapsed_ms"] = int((time.monotonic() - meta.pop("_t0")) * 1000)
        log_usage(meta)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_auth(request)
    body = await request.json()

    model = body.get("model", "")
    if model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail=f"model not allowed: {model}")

    meta = {
        "req_id": request.headers.get("x-request-id") or os.urandom(8).hex(),
        "model": model,
        "stream": bool(body.get("stream")),
        "_t0": time.monotonic(),
    }

    upstream_req = client.build_request(
        "POST",
        "/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {UPSTREAM_KEY}"},
    )
    upstream = await client.send(upstream_req, stream=True)

    if upstream.status_code >= 400:
        raw = await upstream.aread()
        await upstream.aclose()
        meta["status"] = upstream.status_code
        log_usage(meta)
        return JSONResponse(
            status_code=upstream.status_code,
            content=json.loads(raw or b"{}"),
        )

    if meta["stream"]:
        return StreamingResponse(
            relay(upstream, meta),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    data = await upstream.json()
    await upstream.aclose()
    meta["usage"] = data.get("usage")
    meta["status"] = 200
    meta["elapsed_ms"] = int((time.monotonic() - meta.pop("_t0")) * 1000)
    log_usage(meta)
    return JSONResponse(content=data)
```

`X-Accel-Buffering: no` は、後段に nginx を置いたときに SSE がバッファされて「一気に表示される」問題を防ぐためです。nginx 側にも `proxy_buffering off;` が必要になります。

### 起動とクライアント側の移行

```bash
# .env はゲートウェイ側だけに置く
cat > gateway/.env <<'EOF'
DEEPSEEK_API_KEY=sk-xxxxxxxx
GATEWAY_TOKEN=local-dev-token
ALLOWED_MODELS=deepseek-chat
EOF

uvicorn gateway.main:app --host 127.0.0.1 --port 8787
```

クライアントはこう変わります。**変更点は2行だけ**です。

```python
# 変更前
# client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")

# 変更後
from openai import OpenAI

client = OpenAI(api_key="local-dev-token", base_url="http://127.0.0.1:8787/v1")
resp = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "hello"}],
)
```

curl はこうなります。もうシェル履歴に実キーが残りません。

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hello"}]}'
```

### ログ設計で必ず決めること

ゲートウェイを作ると「全部のプロンプトが1か所を通る」状態になります。これは便利であると同時に、**事故ると全ログが漏洩源になる**ということでもあります。

- 既定では **プロンプト本文・レスポンス本文をログに書かない**（メタ情報のみ）
- 書く必要があるなら、環境変数で明示的にON、かつ保持期間を決める
- `Authorization` ヘッダは絶対にログ出力しない
- `usage` はトークン数・モデル名・所要時間だけを残す

DeepSeek の usage にはキャッシュ関連の内訳が含まれる場合がありますが、フィールド名や有無は変更されうるので、**キー名を決め打ちせず `usage` を丸ごと保存する**のが安全です（詳細は公式ドキュメントで要確認）。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 推論系モデルで 504 / ReadTimeout | `httpx.Timeout` の `read` を長め（300秒〜）に。connect は短く保つ |
| ストリーミングが途中で固まる | nginx の `proxy_buffering off;`、レスポンスに `X-Accel-Buffering: no` |
| `usage` が取れない | `stream_options` 等の指定が必要な場合がある。仕様は公式ドキュメントで要確認 |
| 上流 429 がそのままクライアントに返る | まずは素通しでよい。リトライを入れるなら 429/5xx のみ、指数バックオフ＋最大回数 |
| クライアントから繋がらない | `--host 127.0.0.1` は同一マシンのみ。Docker なら `0.0.0.0` とポート公開 |
| キーが漏れた気がする | ゲートウェイのトークンをローテーション。実キーは上流側で再発行 |

## まとめ

- ターミナル地獄の本質は「認証情報・ログ・リトライがクライアント側に散っている」こと
- DeepSeek API が OpenAI 互換なので、ゲートウェイも OpenAI 互換で作れば `base_url` 差し替えだけで移行できる
- 実キーはゲートウェイだけが持ち、クライアントはゲートウェイ用トークンを使う
- モデル許可リストと usage ログだけで、統制の8割は効く
- プロンプト本文は既定でログに残さない（残すなら明示設定＋保持期間）
- 要件が複雑になったら既製ゲートウェイへの移行を検討する

## チェックリスト

- [ ] 実キーがクライアント側の `.env` / シェル履歴から消えている
- [ ] `GATEWAY_TOKEN` は `secrets.compare_digest` で比較している
- [ ] モデル許可リストを設定した
- [ ] タイムアウトを connect と read で分けて設定した
- [ ] ストリーミング時に `X-Accel-Buffering: no` を返している
- [ ] ログに `Authorization` ヘッダとプロンプト本文を出していない
- [ ] `usage` を丸ごと保存する形にした（キー名を決め打ちしていない）
- [ ] 常駐化（systemd / Docker）と再起動時のログローテーションを決めた
