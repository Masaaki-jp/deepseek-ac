---
title: "DeepSeek APIの環境構築 — Python/JSで最初の1リクエストを送るまで"
description: "DeepSeek APIを使い始めるための環境構築ガイド。APIキー取得、Python/JavaScriptのSDK・curlでの最小構成、よくあるつまずきと対処までを一通りまとめます。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - JavaScript
  - 環境構築
publishedAt: 2026-10-05
draft: true
series: "DeepSeek APIの実装"
order: 1
difficulty: beginner
minutes: 10
---

# DeepSeek APIの環境構築 — Python/JSで最初の1リクエストを送るまで

## リード

DeepSeek APIをこれから触る人向けに、キー取得から最初のレスポンス受信までの最短ルートを示す。
OpenAI互換のインターフェースなので、既存のOpenAI SDKの知識がそのまま活かせる。
この記事のコードをコピーすれば、PythonとJavaScriptの両方で疎通確認まで完了する。

## 前提

- 想定読者: これからDeepSeek APIを試すエンジニア、他社APIの経験がある開発者
- 必要な環境: Python 3.10+ または Node.js 18+、ターミナル、HTTPクライアント（curl）
- 前提知識: REST APIの基本、環境変数の扱い

> 注記: モデル名・エンドポイント・価格は変更が早い。実装前に必ずDeepSeek公式ドキュメントで最新仕様を確認すること。本記事のコードは構成の説明が目的で、パラメータ名の最終確認は公式を優先する。

## 本文

### 1. APIキーを取得する

DeepSeekの開発者プラットフォームでアカウントを作成し、APIキーを発行する。

1. 公式プラットフォームにログイン
2. API Keysページで新しいキーを作成
3. `sk-` で始まる文字列をコピー（画面を閉じると再表示できないことが多い）

キーは**コードに直書きしない**。環境変数に格納するのが基本。

```bash
# .env を使う場合（.gitignoreに必ず追加）
echo 'DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx' >> .env
echo '.env' >> .gitignore
```

```bash
# シェルの一時設定（動作確認用）
export DEEPSEEK_API_KEY="sk-xxxxxxxxxxxxxxxx"
```

### 2. curlで疎通確認

まず生のHTTPで動くことを確認する。SDKのトラブルとネットワークのトラブルを切り分けられる。

```bash
curl https://api.deepseek.com/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "user", "content": "1+1は?"}
    ],
    "stream": false
  }'
```

`choices[0].message.content` に応答が入っていれば疎通成功。
401が返るならキー、404ならエンドポイントURL、429ならレート制限を疑う。

### 3. Pythonで実装する

OpenAI SDKが互換で使えるため、`base_url` を差し替えるだけで動く。

```bash
pip install openai
```

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "system", "content": "簡潔に答えてください。"},
        {"role": "user", "content": "DeepSeek APIの特徴を3行で。"},
    ],
    temperature=0.7,
)

print(response.choices[0].message.content)
```

ストリーミングにする場合は `stream=True` を渡し、チャンクを逐次処理する。

```python
stream = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "短い詩を書いて"}],
    stream=True,
)

for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

### 4. JavaScript（Node.js）で実装する

Node.jsでもOpenAI SDKを流用できる。

```bash
npm install openai
```

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.DEEPSEEK_API_KEY,
  baseURL: "https://api.deepseek.com",
});

const res = await client.chat.completions.create({
  model: "deepseek-chat",
  messages: [
    { role: "user", content: "JavaScriptからDeepSeekを呼ぶ最小例は?" },
  ],
});

console.log(res.choices[0].message.content);
```

`package.json` に `"type": "module"` を入れるか、`.mjs` 拡張子にしておくとimport文がそのまま動く。

### 5. 使うモデルを選ぶ

DeepSeekは用途別にモデルが用意されている。一般チャット・汎用なら `deepseek-chat`、推論を深めたい場合は推論系モデルを選ぶ、という使い分けが基本。ただし**モデル名は更新が頻繁なため、必ず公式の利用可能モデル一覧で確認する**こと。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 401 Unauthorized | APIキーの値・環境変数の読み込みを確認。余分な空白や改行に注意 |
| 404 Not Found | `base_url` / エンドポイントのパス誤り。公式のURLを再確認 |
| 429 Too Many Requests | レート制限。リトライ（指数バックオフ）を実装 |
| 環境変数が読めない | `.env` の読み込み（python-dotenv等）を忘れていないか確認 |
| importエラー | SDKのバージョン不一致。`pip show openai` / `npm ls openai` で確認 |
| 応答が途中で切れる | ストリーミング処理のバッファリング・flush漏れを確認 |

## まとめ

- DeepSeek APIはOpenAI互換で、SDKの`base_url`差し替えだけで動く
- まずcurlで疎通確認すると、SDK起因かネットワーク起因かの切り分けができる
- APIキーは環境変数で管理し、コードやリポジトリに残さない
- モデル名・エンドポイント・価格は変動するため公式ドキュメントを一次情報にする

## チェックリスト

- [ ] APIキーを発行し、環境変数に格納した
- [ ] `.env` を `.gitignore` に追加した
- [ ] curlで200と応答本文を確認した
- [ ] PythonまたはJSで1リクエスト成功した
- [ ] ストリーミングの動作を確認した
- [ ] 401/404/429の切り分け方法を把握した
- [ ] 公式ドキュメントで最新のモデル名・料金を確認した