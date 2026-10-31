---
title: "ストリーミングの仕組み — なぜLLM APIは「届いた分から」返すのか"
description: "ストリーミングの基本原理（バッファ・チャンク転送）を押さえた上で、DeepSeek APIのSSEストリーミングをPython/JSで正しく受信する実装と、途中で表示が止まる原因を解説します。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - JavaScript
  - SSE
publishedAt: 2026-10-31
draft: false
series: "DeepSeek APIの実装"
order: 4
difficulty: intermediate
minutes: 10
---

## リード

ストリーミングは「全部そろってから処理する」を「届いた分から順に処理する」に変える仕組みです。この記事では、動画配信で使われる基本原理（バッファとチャンク）を土台に、DeepSeek API のストリーミング応答を Python / JavaScript で正しく受け取る方法と、表示が止まる典型原因を整理します。

## 前提

- 想定読者: DeepSeek API でチャットUIやCLIツールを作るエンジニア
- 必要な環境: Python 3.11+ / Node.js 20+（fetch が標準で使える版本）
- 前提知識: HTTP リクエスト、非同期処理（`async/await` またはイテレータ）の基礎

> 本記事の API 仕様（モデル名、エンドポイント、`usage` の返り方など）は変更され得ます。実装前に DeepSeek 公式ドキュメントで最新仕様を確認してください。

## 本文

### ストリーミングの基本原理

ストリーミングの本質は、**受信しながら同時に再生（処理）する**ことです。従来のダウンロード方式では、ファイル全体を取得し終えてからでないと再生できませんでしたが、ストリーミングでは分割された小さなデータを受け取り、バッファ（一時的な蓄積領域）に置きながら順番に処理します。

- ダウンロード方式: 配信タイミングと再生タイミングが独立（事前に全部取得しておく）
- ストリーミング方式: 配信タイミングと処理タイミングが強く結びつく（届く速さが体験を左右する）

後者の弱点もはっきりしています。**受信側の回線や処理能力が不足すると、再生が途中で止まる**。この「止まる／詰まる」問題は、LLM API のストリーミングでも形を変えてそのまま現れます。

| 観点 | ダウンロード方式 | ストリーミング方式 |
|---|---|---|
| 最初の出力まで | 全体取得後 | 最初のチャンク到着時 |
| メモリ／ディスク | 全体分が必要 | チャンク＋バッファ分で済む |
| 失敗時の影響 | 再取得でやり直しやすい | 途中までの出力が残る |
| ボトルネック | 総転送量 | 回線品質・バッファリング |

### なぜ LLM API でストリーミングを使うのか

LLM の応答は「長い文章を生成し終えてから返す」ため、非ストリーミングだと**全文生成が終わるまで何も表示されません**。ストリーミングにすると、最初のトークンが届いた時点で描画を始められます。

体感を決める指標は2つに分けて考えます。

- **TTFT（Time To First Token）**: リクエスト送信から最初の1トークン到着まで
- **総生成時間**: 最後のトークンまでの時間

ストリーミングは総生成時間を短くしませんが、TTFT を体感の待ち時間に変える効果があります。逆に言えば、**TTFT が悪化する要因（大きなプロンプト、混雑、プロキシのバッファリング）を潰さないと、ストリーミングの意味はほぼ消えます**。

### 中身は Server-Sent Events（SSE）

LLM API のストリーミングは、HTTP 1本の接続の上を `data: ...` 行が連続して流れる **SSE（Server-Sent Events）** 形式が一般的です。DeepSeek API も OpenAI 互換の `stream: true` を受け付ける形が案内されています（最新の対応状況は公式ドキュメントで要確認）。

まず SDK を使う場合。差分だけが届くので、**受け取った断片を連結する**のが基本です。

```python
from openai import OpenAI

client = OpenAI(
    api_key="YOUR_API_KEY",          # 環境変数から読む。直書きしない
    base_url="https://api.deepseek.com",  # 要確認
)

stream = client.chat.completions.create(
    model="deepseek-chat",           # モデル名は公式ドキュメントで要確認
    messages=[{"role": "user", "content": "SSEの仕組みを3行で"}],
    stream=True,
)

parts = []
for chunk in stream:
    delta = chunk.choices[0].delta.content if chunk.choices else None
    if delta:
        parts.append(delta)
        print(delta, end="", flush=True)

answer = "".join(parts)
```

SDK が何を隠しているかを知るには、生の HTTP を一度見るのが早道です。

```python
import json
import httpx

with httpx.stream(
    "POST",
    "https://api.deepseek.com/chat/completions",  # 要確認
    headers={"Authorization": "Bearer YOUR_API_KEY"},
    json={
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "こんにちは"}],
        "stream": True,
    },
    timeout=60.0,
) as res:
    for line in res.iter_lines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue  # 空行・keep-alive・分割された断片は捨てる
        delta = obj["choices"][0]["delta"].get("content")
        if delta:
            print(delta, end="", flush=True)
```

JavaScript では、`ReadableStream` のチャンクが**必ずしもイベント境界で切れない**点が最大の罠です。

```javascript
const res = await fetch("https://api.deepseek.com/chat/completions", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: `Bearer ${process.env.DEEPSEEK_API_KEY}`,
  },
  body: JSON.stringify({
    model: "deepseek-chat",
    messages: [{ role: "user", content: "こんにちは" }],
    stream: true,
  }),
});

const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = "";

while (true) {
  const { value, done } = await reader.read();
  if (done) break;

  // stream: true を忘れるとマルチバイト文字が壊れる
  buffer += decoder.decode(value, { stream: true });

  const events = buffer.split("\n\n");
  buffer = events.pop(); // 末尾は未完成の可能性があるので保留

  for (const event of events) {
    for (const line of event.split("\n")) {
      if (!line.startsWith("data: ")) continue;
      const payload = line.slice(6);
      if (payload === "[DONE]") break;
      try {
        const obj = JSON.parse(payload);
        const delta = obj.choices?.[0]?.delta?.content;
        if (delta) process.stdout.write(delta);
      } catch {
        // 不完全な JSON は無視（次チャンクで補完される）
      }
    }
  }
}
```

### バッファリングという共通の敵

動画配信でバッファが「途切れ防止」に働くのに対し、API のストリーミングでは**バッファが遅延の原因**になります。間にプロキシや CDN が入ると、小さなチャンクがまとめられて一気に届くことがあります。

- リバースプロキシ（nginx 等）: レスポンスバッファリングを無効化する設定を検討
- アプリ側: チャンク単位で `flush` する（Python の `flush=True`、Node の逐次 `write`）
- 圧縮: gzip が挟まるとバッファされやすいため、SSE エンドポイントでは無効化を検討

（具体的な設定値は利用中のミドルウェアのドキュメントを参照してください。）

### 実装パターン

- **途中キャンセル**: ブラウザなら `AbortController`、Python ならループの `break` かクライアントのクローズ。生成を止めても課金済みトークンは戻らない点に注意（課金仕様は公式ドキュメントで要確認）
- **二重表示の防止**: 推論過程と最終回答が別フィールドで届くモデルでは、両方を同じ場所に追記すると表示が崩れる。フィールドごとに出力先を分ける
- **保存と表示の分離**: 表示用の逐次出力と、DB保存用の全文連結を別変数で持つ（後段で整形・検証しやすくなる）

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 途中で `JSON.parse` が失敗する | チャンク境界で切れている。バッファに残して次チャンクと連結する |
| 日本語が文字化けする | `TextDecoder` に `{ stream: true }` を渡す／バイト列で分割しない |
| 最後のメッセージが欠ける | `[DONE]` 到達時に残バッファを処理しているか確認 |
| ローカルでは流れるが本番で一括表示 | プロキシ／CDN のバッファリング。圧縮も疑う |
| ストリーミングにしたのに速く感じない | TTFT を計測。プロンプト長・モデル・リージョンを切り分ける |
| リトライで応答が二重になる | 生成途中の再開は基本できない。リクエスト単位で冪等に設計する |

## まとめ

- ストリーミングの正体は「チャンク＋バッファで、届いた分から処理する」方式。処理能力や回線が不足すると詰まるという弱点も共通
- LLM API では SSE（`data:` 行の連続）として実装され、差分を連結するのが基本。境界で切れる前提のバッファ処理が必須
- 効果が出るかどうかは TTFT で決まる。プロキシのバッファリングを潰さないと、実装しても体感は変わらない

## チェックリスト

- [ ] API キーを環境変数から読んでいる（コード・ログに直書きしていない）
- [ ] チャンク境界をまたぐ JSON をバッファで処理している
- [ ] `TextDecoder` に `stream: true` を渡している
- [ ] `[DONE]` と空行・keep-alive 行を正しく無視している
- [ ] 本番環境でプロキシ／圧縮のバッファリングを確認した
- [ ] TTFT と総生成時間を分けて計測している
- [ ] モデル名・エンドポイント・課金仕様を公式ドキュメントで最終確認した
