---
title: "DeepSeek APIの開発環境と本番環境を分離する設計"
description: "開発中に本番キーで誤リクエストを飛ばす事故を防ぐため、APIキー・モデル・予算を環境ごとに分ける実装パターンをまとめる。"
category: operations
tags:
  - DeepSeek
  - API
  - Python
  - 運用
publishedAt: 2026-10-10
draft: false
series: "DeepSeek運用"
order: 1
difficulty: intermediate
minutes: 10
---

## リード

DeepSeek API を使ったアプリで、開発中に本番キーを使ってしまう事故を防ぐための環境分離を扱う。
設定を「環境」という1つの軸で切り替える設計にすることで、キー漏洩・課金爆発・本番データ汚染をまとめて防げる。
この記事を読むと、Python / JavaScript の両方で起動時に環境を判定し、誤設定を起動時点で検出するコードが書けるようになる。

## 前提

- 想定読者: DeepSeek API をアプリに組み込むエンジニア
- 必要な環境: Python 3.11+ または Node.js 20+、`.env` を扱える環境
- 前提知識: 環境変数の基本、DeepSeek API のチャット補完エンドポイントの概要

## 本文

### なぜ環境分離が必要か

分離していないと、次の3つの事故が起きる。

1. **キー漏洩** — 開発用リポジトリに本番キーが入り、コミット経由で外部に流出する
2. **課金爆発** — 開発中のテストループが本番キーで回り、想定外の従量課金が発生する
3. **本番データ汚染** — テストデータが本番のログ・保存先・分析に混入する

どれも「あとから気づく」タイプの事故で、検知が遅れるほど被害が大きい。起動時点で弾くのが最もコストが低い。

### 分離すべき4つの軸

環境を切り替えるとき、最低限これだけは分ける。

| 軸 | 開発 | 本番 |
|---|---|---|
| APIキー | 開発用キー | 本番用キー |
| モデル | 安価なモデルで検証 | 用途に応じたモデル |
| ベースURL | 検証用エンドポイント | 本番エンドポイント |
| 予算キャップ | 低めに設定 | 用途に応じて設定 |

> **注記**: DeepSeek API の具体的なモデル名・エンドポイント・レート制限値は変更される可能性があるため、公式ドキュメントで最新値を確認すること。以下のコードでは設定値を環境変数経由で受け取る前提で書く。

### 設定クラスで一元管理する（Python）

`ENV` を唯一のスイッチにし、そこから全ての設定を導出する。散在する `if ENV == "prod"` を減らすのが狙い。

```python
# config.py
import os
from dataclasses import dataclass
from typing import Literal

Env = Literal["dev", "prod"]

@dataclass(frozen=True)
class Settings:
    env: Env
    api_key: str
    base_url: str
    model: str
    max_requests_per_run: int

def load_settings() -> Settings:
    raw = os.environ.get("APP_ENV")
    if raw not in ("dev", "prod"):
        # 未設定を許容しない。曖昧なまま起動させないのが要点
        raise RuntimeError(
            "APP_ENV must be 'dev' or 'prod'. "
            f"got: {raw!r}"
        )
    env: Env = raw  # type: ignore[assignment]

    if env == "dev":
        return Settings(
            env=env,
            api_key=os.environ["DEEPSEEK_API_KEY_DEV"],
            base_url="https://api.deepseek.com",  # 例。公式で要確認
            model="deepseek-chat",
            max_requests_per_run=50,
        )
    return Settings(
        env=env,
        api_key=os.environ["DEEPSEEK_API_KEY_PROD"],
        base_url="https://api.deepseek.com",  # 例。公式で要確認
        model="deepseek-chat",
        max_requests_per_run=10_000,
    )
```

ポイントは次の3つ。

- `frozen=True` で起動後の書き換えを防ぐ
- `APP_ENV` 未設定時は **例外を投げて起動させない**（デフォルトで prod にすると危険）
- キーは環境ごとに別の環境変数名にする（`_DEV` / `_PROD`）。1つの変数を差し替える運用は事故りやすい

### 起動時に環境を必ずログ出力する

「今どっちの環境で動いているか」が見えていないと、事故の初動が遅れる。起動ログの先頭に出す。

```python
import logging

logger = logging.getLogger(__name__)

def announce_env(s: Settings) -> None:
    logger.warning(
        "starting app | env=%s | model=%s | max_requests=%d",
        s.env, s.model, s.max_requests_per_run,
    )
```

`warning` で出しているのは、本番起動時に目に入りやすくするため。`info` だと本番のログレベル次第で埋もれる。

### JavaScript / TypeScript の場合

同じ考え方を TS で書くと次のようになる。

```ts
// config.ts
type Env = "dev" | "prod";

export interface Settings {
  env: Env;
  apiKey: string;
  baseUrl: string;
  model: string;
  maxRequestsPerRun: number;
}

export function loadSettings(): Settings {
  const raw = process.env.APP_ENV;
  if (raw !== "dev" && raw !== "prod") {
    throw new Error(`APP_ENV must be 'dev' or 'prod'. got: ${raw}`);
  }
  const env: Env = raw;

  const apiKey =
    env === "dev"
      ? process.env.DEEPSEEK_API_KEY_DEV
      : process.env.DEEPSEEK_API_KEY_PROD;

  if (!apiKey) {
    throw new Error(`Missing API key for env=${env}`);
  }

  return {
    env,
    apiKey,
    baseUrl: "https://api.deepseek.com", // 例。公式で要確認
    model: "deepseek-chat",
    maxRequestsPerRun: env === "dev" ? 50 : 10_000,
  };
}
```

### 起動時ガードを入れる

設定読み込み時に「明らかに危険な組み合わせ」を弾く。テストを本番キーで実行するのを防ぐ。

```python
def assert_safe(s: Settings) -> None:
    # テスト実行時に本番環境だったら止める
    if os.environ.get("PYTEST_CURRENT_TEST") and s.env == "prod":
        raise RuntimeError("refusing to run tests against prod")

    # 本番キーが dev 環境変数に入っていないか（命名の取り違え検知）
    if s.env == "dev" and not s.api_key.startswith("sk-dev"):
        raise RuntimeError("dev key does not look like a dev key")
```

> **注記**: `sk-dev` のような接頭辞はあくまで例。実際のキー形式に合わせて置き換えること。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 本番キーが開発で使われた | `APP_ENV` 未設定時に例外を投げ、起動時にログ出力する |
| `.env` がコミットされた | `.gitignore` に追加し、pre-commit hook でステージングを検知する |
| 環境判定が曖昧 | デフォルト値を設けず、`APP_ENV` 未設定なら起動失敗させる |
| キーの取り違えに気づかない | 起動時ガードで接頭辞や実行コンテキストを検証する |
| ステージング環境がない | まず dev / prod の2環境で分離し、必要になったら増やす |

## まとめ

- 環境分離は「キー」「モデル」「URL」「予算」の4軸をまとめて切り替える設計にする
- 唯一のスイッチ（`APP_ENV`）を決め、未設定なら起動を止める
- 起動時に環境を必ずログ出力し、危険な組み合わせは起動時ガードで弾く

## チェックリスト

- [ ] `APP_ENV` は未設定時に例外を投げる（デフォルト値を持たない）
- [ ] APIキーは `_DEV` / `_PROD` で環境変数名を分けた
- [ ] 設定オブジェクトは起動後に書き換えられない（`frozen` / `readonly`）
- [ ] 起動ログの先頭に env / model / 予算が出る
- [ ] テスト実行時に本番環境なら止まる
- [ ] `.env` は `.gitignore` 済みで、pre-commit で検知する
