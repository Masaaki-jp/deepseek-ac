---
title: "DeepSeek APIキーの管理方法 — 漏洩を防ぐ実装パターン"
description: "APIキーをコードに直書きしないための現実的な管理方法を整理。環境変数・シークレットマネージャ・ローテーション・漏洩時の対処まで、DeepSeek APIを前提にまとめます。"
category: security
tags:
  - DeepSeek
  - API
  - セキュリティ
  - 環境変数
publishedAt: 2026-10-05
draft: false
series: "DeepSeekセキュリティ"
order: 1
difficulty: beginner
minutes: 10
---

## リード

DeepSeek APIキーの漏洩は、そのまま従量課金の不正利用に直結します。
この記事では「どこに置くか」「どう渡すか」「漏れたらどうするか」の3点を、ローカル開発から本番運用まで一通り整理します。

## 前提

- 想定読者: DeepSeek APIを使い始めた〜本番導入を検討中のエンジニア
- 必要な環境: Python 3.10+ または Node.js 18+
- 前提知識: 環境変数、Git の基本操作

> 注記: 具体的なキー発行手順・ダッシュボードのUI・料金は変更される可能性があります。最新の仕様は DeepSeek 公式ドキュメントで確認してください。

## 本文

### やってはいけない置き方

まず避けるべきパターンを押さえます。よくある事故は次の4つです。

- ソースコードへの直書き（`api_key = "sk-..."`）
- `.env` をそのまま Git にコミット
- フロントエンドのJSやモバイルアプリへの埋め込み
- チャットや issue への貼り付け

特にフロントエンド埋め込みは、ビルド成果物から誰でも抽出できます。ブラウザから直接 DeepSeek API を叩く構成は避け、サーバ側を経由させてください。

### 環境変数で渡す（ローカル開発）

最小構成は環境変数です。コード側は「環境変数が無ければ起動時に失敗する」ようにしておくと、設定漏れに早く気づけます。

```python
import os
from openai import OpenAI

api_key = os.environ.get("DEEPSEEK_API_KEY")
if not api_key:
    raise RuntimeError("DEEPSEEK_API_KEY is not set")

client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
```

Node.js の場合:

```js
import OpenAI from "openai";

const apiKey = process.env.DEEPSEEK_API_KEY;
if (!apiKey) {
  throw new Error("DEEPSEEK_API_KEY is not set");
}

const client = new OpenAI({ apiKey, baseURL: "https://api.deepseek.com" });
```

`.env` はローカル専用にし、必ず `.gitignore` に入れます。

```bash
# .gitignore
.env
.env.*
!.env.example
```

リポジトリには値ではなくキー名だけを持つ `.env.example` を置きます。

```bash
# .env.example
DEEPSEEK_API_KEY=
```

### コードに鍵の存在を検知させる

直書きや誤コミットを防ぐため、pre-commit にシークレットスキャナを挟みます。

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.0
    hooks:
      - id: gitleaks
```

CI 側でも同じチェックを回すと、ローカルフックをすり抜けたコミットを拾えます。

```yaml
# .github/workflows/secret-scan.yml
name: secret-scan
on: [push, pull_request]
jobs:
  gitleaks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: gitleaks/gitleaks-action@v2
```

### 本番はシークレットマネージャに寄せる

本番で `.env` ファイルを配る運用は、配布経路が増えるほど漏洩リスクが上がります。クラウドのシークレットマネージャ経由で「実行時に取得」する形にします。

- AWS: Secrets Manager / SSM Parameter Store
- GCP: Secret Manager
- Azure: Key Vault
- セルフホスト: Vault

環境変数として注入する例（AWS Secrets Manager + Python）:

```python
import json
import boto3
import os
from openai import OpenAI

def load_api_key() -> str:
    secret_arn = os.environ["DEEPSEEK_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret = client.get_secret_value(SecretId=secret_arn)
    return json.loads(secret["SecretString"])["DEEPSEEK_API_KEY"]

client = OpenAI(api_key=load_api_key(), base_url="https://api.deepseek.com")
```

取得値はログに出さないでください。例外ハンドラで `str(e)` をそのまま出力すると、リクエストヘッダが混ざることがあります。

### 権限とキーの分離

1つのキーを全環境で使い回さないでください。少なくとも次を分けます。

- 開発用 / ステージング用 / 本番用
- 用途別（バッチ、APIサーバ、社内ツール）

DeepSeek 側でキーごとのスコープや利用上限を設定できる場合は、必ず絞ります。使われていないキーは削除します。

> 注記: キー単位のスコープ・レート制限・予算アラートの有無はプランや時期により変わります。公式ダッシュボードで確認してください。

### ローテーション

漏洩していなくても、定期的に交換します。手順を決めておくと事故時にも使えます。

1. 新しいキーを発行する
2. デプロイ先のシークレットを更新する（新旧併用期間を設ける）
3. アプリを再起動し、新キーで動作確認する
4. 旧キーを無効化する
5. 監査ログに実施日と理由を残す

「先に新を入れ、後で旧を消す」順序を守るとダウンタイムが出ません。

### 漏洩に気づいたときの初動

疑わしいと思った時点で、次の順に動きます。

1. 該当キーを即座に無効化（ローテーションより先）
2. 利用量・請求の急増がないか確認
3. アクセスログから不正な呼び出し元を特定
4. 新しいキーを発行して再デプロイ
5. 原因（コミット、ログ出力、共有経路）を潰す

Git にコミットしてしまった場合は、**履歴から消すだけでは不十分**です。必ずキー自体を無効化してください。push 済みならリモートにも残っています。

### ログとエラーハンドリング

APIキーが漏れる経路の多くは「ログ」です。次のような出力を避けます。

```python
# NG: 例外やリクエスト全体を丸ごと出力
logger.exception("request failed: %s", response.request)

# OK: ステータスコードとメッセージだけ
logger.error("request failed: status=%s", response.status_code)
```

デバッグ時も Authorization ヘッダをマスクする仕組みを入れておくと安全です。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| ローカルでは動くがCIで認証エラー | CIのシークレットに `DEEPSEEK_API_KEY` を登録し、参照名を揃える |
| `.env` を push してしまった | 履歴削除より先にキーを無効化し、再発行する |
| 環境変数が読み込まれない | 実行プロセスに注入されているか確認（systemd, Docker, サーバレス設定など） |
| キーをどこに置いたか分からない | シークレットマネージャに一本化し、コードからはARN/名前だけ参照する |

## まとめ

- キーはコードに書かず、環境変数かシークレットマネージャから実行時に渡す
- 環境・用途ごとにキーを分け、不要なキーは削除する
- 漏洩時は「無効化 → 監査 → 再発行」の順で動く

## チェックリスト

- [ ] ソースコードにAPIキーが直書きされていない
- [ ] `.env` が `.gitignore` に入っている
- [ ] `.env.example` にキー名だけを記載している
- [ ] pre-commit / CI でシークレットスキャンを回している
- [ ] 本番キーはシークレットマネージャから取得している
- [ ] 環境・用途ごとにキーを分離している
- [ ] ローテーション手順をドキュメント化している
- [ ] 例外・ログにキーやヘッダを出力しないようにしている