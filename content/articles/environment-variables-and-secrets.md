---
title: "DeepSeek APIキーを安全に扱う — 環境変数とシークレット管理の実践"
description: "DeepSeek APIキーをコードに埋め込まず、.env・OS環境変数・シークレットマネージャで管理する方法を、ローカル開発から本番までの流れで整理する。漏洩時のローテーション手順も扱う。"
category: security
tags:
  - DeepSeek
  - セキュリティ
  - 環境変数
  - APIキー
publishedAt: 2026-10-05
draft: false
series: "DeepSeekのセキュリティ"
order: 1
difficulty: beginner
minutes: 9
---

## リード

APIキーの扱い方を間違えると、気づかないうちに課金・情報漏洩につながる。ここでは DeepSeek APIキーを例に、ローカル開発から本番環境まで安全に管理する実践手順をまとめる。

## 前提

- 想定読者: DeepSeek API を使い始めた開発者 / 既存プロジェクトのキー管理を見直したいエンジニア
- 必要な環境: Python 3.10+ または Node.js 18+、Git
- 前提知識: 環境変数の基本、`.env` ファイルの存在を知っている程度

## 本文

### なぜ「コードに直書き」が危険か

よくある失敗パターンを示す。

```python
# NG: キーがソースに残る
client = OpenAI(api_key="sk-xxxxxxxxxxxx", base_url="https://api.deepseek.com")
```

この形の問題点は次の3つ。

- Git 履歴に残る（削除しても履歴に残る）
- スクリーンショットやエラーログ経由で外部に露出する
- キーを差し替えるたびに再デプロイが必要になる

特に「一度 push したキーは漏洩済みとみなす」が原則。履歴からの削除だけでは不十分で、後述のローテーションが必要になる。

### .env でローカル管理する

ローカル開発では `.env` に分離し、`.gitignore` で除外する。

```bash
# .env
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

```gitignore
# .gitignore
.env
.env.*
!.env.example
```

チームで共有するのは値ではなく「キー名の一覧」だけにする。

```bash
# .env.example（これはコミットする）
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

### Python での読み込み

`python-dotenv` を使って起動時に読み込む。

```python
from dotenv import load_dotenv
import os

load_dotenv()  # .env を読み込む

api_key = os.environ["DEEPSEEK_API_KEY"]  # 無ければ KeyError で気づける
```

ポイントは `os.environ.get()` ではなく `os.environ[]` を使うこと。キー未設定時に `None` で素通りさせず、起動時点で落とす。

### Node.js での読み込み

```js
// Node.js 20.6+ は --env-file が使える
// node --env-file=.env index.js

const apiKey = process.env.DEEPSEEK_API_KEY;
if (!apiKey) {
  throw new Error('DEEPSEEK_API_KEY is not set');
}
```

Node 20.6 以降は `--env-file` が標準で使えるため、`dotenv` パッケージを追加せずに済む場合がある。

### 本番では「シークレットマネージャ」に置く

`.env` はローカル専用と割り切る。本番ではプラットフォームのシークレット機能を使う。

- GitHub Actions: Repository secrets / Environment secrets
- Vercel / Netlify / Cloudflare: ダッシュボードの Environment Variables
- AWS: Secrets Manager / SSM Parameter Store
- GCP: Secret Manager

コード側は「環境変数を読む」まま変えず、供給元だけを差し替えるのが定石。これでローカルと本番で同じコードが動く。

### ログにキーを出さない

HTTPクライアントのデバッグログは、リクエストヘッダをそのまま出す設定になっているとキーが漏れる。開発時のみ有効にし、本番では無効にする。

```python
# デバッグログは開発時のみ。本番では必ずオフ。
# logging.basicConfig(level=logging.DEBUG)  # 危険
```

### キーが漏れたときのローテーション

漏洩を疑ったら、次の順で動く。

1. DeepSeek のダッシュボードで該当キーを無効化（失効）
2. 新しいキーを発行
3. 各環境（ローカル / CI / 本番）のシークレットを更新
4. 課金・利用状況を確認し、不審な呼び出しがないか点検
5. Git 履歴に残っている場合は、履歴削除（`git filter-repo` 等）を検討

「無効化 → 再有効化」ではなく「無効化 → 新規発行」が原則。古いキーを復活させない。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| `KeyError: 'DEEPSEEK_API_KEY'` | `.env` の配置場所と `load_dotenv()` の呼び出し順を確認 |
| CI でだけキーが空 | GitHub Actions の Secrets に未登録。Environment secrets のスコープも確認 |
| 本番で動かない | シークレット登録後に再デプロイしていないケースが多い |
| ログにキーが出た | HTTPクライアントのデバッグログをオフにし、キーをローテーション |
| `.env` を commit した | すぐローテーション。履歴削除はその後 |

## まとめ

- キーはコード・Git・ログのどこにも残さない
- ローカルは `.env`、本番はプラットフォームのシークレット機能に分離する
- コードは「環境変数を読む」形を保ち、供給元だけ差し替える
- 漏洩時は「無効化 → 新規発行 → 反映 → 点検」の順で動く

## チェックリスト

- [ ] キーがソースに直書きされていない
- [ ] `.env` が `.gitignore` に入っている
- [ ] `.env.example` にキー名だけ記載している
- [ ] キー未設定時に起動時点でエラーになる
- [ ] 本番はシークレットマネージャから供給している
- [ ] デバッグログにキーが出ない設定になっている
- [ ] 漏洩時のローテーション手順をチームで共有している