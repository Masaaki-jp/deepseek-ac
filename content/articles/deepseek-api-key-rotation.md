---
title: "DeepSeek APIキーの漏洩を前提にした設計 — 失効とローテーションの実装"
description: "APIキーは「漏れない」ではなく「漏れても被害を最小化できる」前提で設計する。DeepSeek APIにおけるキー分離、失効、ローテーション、監視の実装パターンを整理します。"
category: security
tags:
  - DeepSeek
  - API
  - セキュリティ
  - APIキー
publishedAt: 2026-10-09
draft: false
series: "セキュリティ"
order: 1
difficulty: intermediate
minutes: 10
---

## リード

APIキーの漏洩は「起きるかどうか」ではなく「いつ起きるか」の問題です。
この記事では、DeepSeek APIキーが漏れた場合に被害を最小化する設計 — キーの分離、失効手順、ローテーションの自動化 — を実装目線で整理します。

## 前提

- 想定読者: DeepSeek API を本番またはチームで利用しているエンジニア
- 必要な環境: Python 3.10+ / Node.js 18+、シークレットマネージャ（例: AWS Secrets Manager, GCP Secret Manager, 1Password など）
- 前提知識: 環境変数による API キー管理、CI/CD の基本

> 注: DeepSeek の API キー管理画面の具体的なUIやローテーション機能の有無は変更されやすいため、実装前に公式ドキュメントで最新仕様を確認してください。本記事は「キー文字列を安全に差し替えられる」ことを前提にした一般設計です。

## 本文

### なぜ「漏洩前提」で設計するのか

「コミットしない」「ログに出さない」といった予防策は必要ですが、それだけでは不十分です。実際の漏洩経路は多様です。

- 誤って Git にコミット（履歴に残る）
- CI のログ、エラーレポート、APM に出力される
- 開発者端末の `.env` が侵害される
- 依存パッケージ経由でプロセス環境変数が読まれる

予防と同時に、**漏れても短時間で無効化できる**状態を作っておくことが重要です。

### 原則1: キーを用途別に分離する

1つのキーを全環境・全用途で使い回すと、1箇所の漏洩が全体に波及します。

| 単位 | 例 | 目的 |
|---|---|---|
| 環境 | dev / staging / prod | 開発環境の漏洩が本番に影響しない |
| 用途 | 対話的利用 / バッチ / 外部公開API | 影響範囲の限定 |
| 主体 | 開発者ごと / サービスごと | 漏洩元の特定と個別失効 |

DeepSeek 側で複数キーを発行できる場合、この粒度で分けておくとローテーションも局所化できます。

### 原則2: キーはコード・環境変数だけで持たない

`.env` はローカル開発には便利ですが、本番では**シークレットマネージャから動的に取得**します。

```python
# 例: AWS Secrets Manager から取得（概念）
import json
import boto3
from functools import lru_cache

@lru_cache(maxsize=1)
def get_deepseek_key() -> str:
    client = boto3.client("secretsmanager")
    secret = client.get_secret_value(SecretId="deepseek/api-key")
    return json.loads(secret["SecretString"])["api_key"]
```

キャッシュを短命にしておくと、ローテーション後も再起動なしで新キーに追従できます。可能なら TTL 付きキャッシュにします。

### 原則3: ローテーションを手順化する（二重キー期間を設ける）

キーを差し替える瞬間に「旧キー無効化 → 新キー配布」を同時に行うと、ダウンタイムが発生します。**新キーを先に配布し、両方が有効な期間を設けてから旧キーを失効**させます。

```
1. 新キー発行（旧キーはまだ有効）
2. シークレットマネージャの値を新キーに更新
3. 各サービスが新キーを取得したことを確認（TTL経過 or 再デプロイ）
4. 旧キーの使用状況を監視（呼び出しがゼロになったか）
5. 旧キーを失効
```

各ステップの間に「切り戻せる」余地を残すのがポイントです。

### 原則4: 失効を「すぐ実行できる」状態にする

漏洩に気づいたとき、失効作業が属人化していると初動が遅れます。

- 失効手順を Runbook として文書化（担当・コマンド・確認項目）
- 発行者一覧と「どのサービスがどのキーを使っているか」を台帳化
- 失効後に影響範囲を確認できるよう、キー単位で使用ログを分ける

### 原則5: 漏洩検知と併せて設計する

失効はあくまで「気づいた後」の対応です。気づく仕組みも合わせて用意します。

- リポジトリへのシークレット混入検知（例: gitleaks, GitHub secret scanning）
- 異常な利用パターンの監視（通常と異なる時間帯・量・送信元）
- キー単位の利用量ダッシュボード

### 実装例: ローテーション対応のクライアント

```python
import os
import time
import httpx

class DeepSeekClient:
    def __init__(self, key_provider, ttl_seconds=300):
        self._key_provider = key_provider
        self._ttl = ttl_seconds
        self._cached_key = None
        self._expires_at = 0.0

    def _key(self) -> str:
        now = time.monotonic()
        if self._cached_key is None or now >= self._expires_at:
            self._cached_key = self._key_provider()
            self._expires_at = now + self._ttl
        return self._cached_key

    def chat(self, messages, model="deepseek-chat"):
        # 401/403 を検知したら即座にキャッシュを破棄して再取得
        for attempt in range(2):
            resp = httpx.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {self._key()}"},
                json={"model": model, "messages": messages},
                timeout=30,
            )
            if resp.status_code in (401, 403) and attempt == 0:
                self._cached_key = None  # 失効・ローテーションを検知
                continue
            resp.raise_for_status()
            return resp.json()
```

> 注: エンドポイントや認証ヘッダの仕様は変更されうるため、実装時は公式ドキュメントで確認してください。上記は「キー失効時に再取得する」パターンの例示です。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| ローテーション後に全リクエストが 401 になる | 旧キー無効化が早すぎる。二重キー期間を設ける |
| 新キーに切り替わらないサービスがある | キャッシュ TTL が長すぎる／再デプロイ漏れ。TTL 短縮と台帳で確認 |
| 失効したはずのキーが使われ続ける | キーが複数箇所にコピーされている。台帳と利用ログで発見 |
| 漏洩に気づくのが遅れた | シークレット検知・利用量監視を未整備。原則5を参照 |

## まとめ

- 予防策と同時に「漏れた後の失効・ローテーション」を設計する
- キーは環境・用途・主体で分離し、影響範囲を限定する
- ローテーションは二重キー期間を設けて無停止で行う
- 失効手順を Runbook 化し、誰でも即実行できる状態にする
- 検知の仕組み（シークレットスキャン・利用監視）を併設する

## チェックリスト

- [ ] APIキーを環境・用途・主体で分離している
- [ ] 本番キーをシークレットマネージャで管理している
- [ ] キー取得に短い TTL キャッシュを使っている
- [ ] ローテーション手順（二重キー期間）を文書化している
- [ ] 失効 Runbook と発行者台帳がある
- [ ] リポジトリのシークレット混入検知を有効化している
- [ ] キー単位で利用量を監視している

