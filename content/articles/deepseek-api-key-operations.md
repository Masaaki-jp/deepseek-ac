---
title: "残高は本家、予算は外側——DeepSeek APIを本番で使い続けるためのハイブリッド運用"
description: "DeepSeek APIの残高は公式側、機能別予算と遮断は自前の外側レイヤで持つハイブリッド運用を解説。usageのキャッシュヒット/ミスを分けたコスト計算、事前reserve、段階的熔断、価格改定への追従手順をコード付きで示します。"
category: operations
tags:
  - DeepSeek
  - API
  - コスト管理
  - 運用
  - 監視
publishedAt: 2026-11-22
draft: false
series: "運用"
difficulty: intermediate
minutes: 15
---

[![動画を見る](https://img.youtube.com/vi/latKo7B5keM/0.jpg)](https://www.youtube.com/watch?v=latKo7B5keM)

## リード

DeepSeek の前払い残高は公式側で持ち、**「誰が・どの機能で・いつ」使ったか**はアプリ側の外側レイヤで管理する。この分担にすると、価格改定があってもコードを直さずに済み、残高切れで全機能が同時に止まる事故も防げる。

## 前提

- 想定読者: 個人開発〜小規模チームで DeepSeek API を本番利用している、またはこれからするエンジニア
- 必要な環境: Python 3.11+、DeepSeek API キー、Redis または SQLite
- 前提知識: OpenAI 互換 API の基本、レスポンス `usage` の読み方

> 本記事の単価・ピーク時間帯・モデル名は**前提例**です。数値は必ず公式の価格ページ／API ドキュメントで確認してください。本記事の主張は「数値をコードに埋めない」ことで、特定の価格表ではありません。

## 本文

### なぜ予算を「外側」に置くのか

公式プラットフォームの残高は「あと何ドル残っているか」しか教えてくれません。それだけでは次の3つが分かりません。

1. **どの機能が**食っているか（チャットか、バッチ要約か、社内評価か）
2. **どのユーザー／テナント**が食っているか
3. **キャッシュが効いているか**（＝プロンプト設計が良いか）

さらに残高切れは全機能の同時停止です。機能ごとに守り分けたいなら、遮断判定はリクエストを出す直前、つまり自前の外側に置く必要があります。

### 責務の分け方

| 層 | 持つもの | 持たないもの |
|---|---|---|
| 本家（公式プラットフォーム） | 前払い残高、APIキー発行、モデルと価格の正 | 機能別の内訳、リクエスト単位の遮断 |
| 外側（自前ゲートウェイ） | 機能別/ユーザー別の予算、日次上限、遮断、記録 | 残高そのもの、価格の決定 |

両方使うから「ハイブリッド」です。本家側の残高アラートは最後の砦として必ず併用します。

### 1. usage をコストに変換する（キャッシュを分ける）

DeepSeek の `usage` にはキャッシュ関連の方言フィールドが含まれます。キャッシュヒット時とミス時では入力単価が大きく違うため、**分けずに計算すると見積が桁でずれます**。

単価表はコードに書かず、JSON で外出しします。未設定なら起動時に落とすのが安全です（0 円で計算して予算が効かなくなる事故を防ぐ）。

```json
{
  "version": "2026-11-22",
  "fetched_at": "2026-11-22",
  "currency": "USD",
  "models": {
    "MODEL_ID_HERE": {
      "peak":    { "input_miss": 0.00, "input_hit": 0.00, "output": 0.00 },
      "offpeak": { "input_miss": 0.00, "input_hit": 0.00, "output": 0.00 }
    }
  }
}
```

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Price:
    input_miss: float  # USD / 1M tokens
    input_hit: float
    output: float

def estimate_cost(usage: dict, price: Price) -> dict:
    hit = usage.get("prompt_cache_hit_tokens") or 0
    miss = usage.get("prompt_cache_miss_tokens")
    prompt = usage.get("prompt_tokens") or 0
    if miss is None:
        # 方言フィールドが無いモデル/レスポンスへのフォールバック
        miss = max(prompt - hit, 0)
    completion = usage.get("completion_tokens") or 0

    cost = (
        miss / 1_000_000 * price.input_miss
        + hit  / 1_000_000 * price.input_hit
        + completion / 1_000_000 * price.output
    )
    return {
        "cost_usd": cost,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
    }
```

**なぜ実測だけを信じるか**: 「固定プロンプトだから毎回ヒットしている」という推測は外れます。キャッシュはベストエフォートで、失効もします。`usage` に出た値だけを積むのが正解です。

### 2. 機能別の予算を外側で持つ（reserve → commit）

実行後でないと正確なトークン数は分かりません。だから「事前に見積で枠を確保 → 実行 → 実測で補正」の2段構えにします。これをやらないと、同時実行で上限を超えます。

```python
# budget.py
DAILY_LIMIT_USD = {
    "chat":          0.50,
    "batch_summary": 2.00,
    "internal_eval": 0.30,
}

class BudgetExceeded(Exception):
    def __init__(self, feature, spent, est, limit):
        super().__init__(f"{feature}: spent={spent:.4f} est={est:.4f} limit={limit}")
        self.feature, self.spent, self.est, self.limit = feature, spent, est, limit

def reserve(feature: str, estimated_usd: float, spent_today: float) -> None:
    limit = DAILY_LIMIT_USD.get(feature)
    if limit is None:
        raise PermissionError(f"unknown feature: {feature}")  # 未登録機能は通さない
    if spent_today + estimated_usd > limit:
        raise BudgetExceeded(feature, spent_today, estimated_usd, limit)
```

未登録の機能を `PermissionError` で落とすのは意図的です。新機能を追加したときに「予算未設定のまま野放し」になるのを防げます。

### 3. 全体の日次熔断は「段階遮断」にする

全体上限に達したとき、全リクエストを拒否すると障害になります。無制限なら破産します。現実的なのは重要度順の段階遮断です。

```python
RANK = {"chat": 0, "batch_summary": 1, "internal_eval": 2}

def allow(feature: str, global_spent: float, global_limit: float) -> bool:
    if global_spent < global_limit * 0.8:
        return True
    if global_spent < global_limit:
        return RANK.get(feature, 99) == 0   # 重要機能のみ通す
    return False
```

0.8 のしきい値は運用しながら調整する前提の初期値です。バッチ系は翌日に回せるので、まずここから削るのが定石です。

### 4. ピーク判定は設定ファイル＋タイムゾーン明示で

ピーク／オフピークで単価が変わる場合、コスト計算に時刻を入れます。ただし祝日とタイムゾーンの扱いが地雷です。判定ロジックに日付を直書きせず、祝日カレンダーを外部ファイルに持たせてください。

```python
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

def is_peak(now: datetime, holidays: set[str]) -> bool:
    local = now.astimezone(JST)
    if local.weekday() >= 5:                    # 土日は終日オフピーク
        return False
    if local.date().isoformat() in holidays:    # 祝日は外部ファイルから
        return False
    t = local.hour * 60 + local.minute
    return (10 * 60 <= t < 13 * 60) or (15 * 60 <= t < 19 * 60)
```

**注意**: 上記の時間帯は参考情報をもとにした例です。対象となる祝日の範囲（どの国の祝日が適用されるか）も含め、**公式の価格ページで要確認**です。推測でハードコードすると、静かにコストがずれます。

### 5. 1リクエスト1行で記録する

これが最も効きます。記録があれば「どの機能が予算を食っているか」「キャッシュヒット率が低い機能はどれか」が事実として出ます。キャッシュヒット率が低いのは、多くの場合プロンプト先頭が毎回変わっている設計の匂いです。

```sql
CREATE TABLE llm_usage (
  ts                TEXT    NOT NULL,
  feature           TEXT    NOT NULL,
  model             TEXT    NOT NULL,
  is_peak           INTEGER NOT NULL,
  cache_hit_tokens  INTEGER NOT NULL,
  cache_miss_tokens INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  cost_usd          REAL    NOT NULL,
  pricing_version   TEXT    NOT NULL,   -- 単価表のバージョン
  status            TEXT    NOT NULL
);
```

`pricing_version` を必ず残してください。これが無いと、価格改定後に「過去の数字と今の数字がなぜ違うのか」を説明できなくなります。

### 価格改定に追従する手順

1. 公式の価格ページを確認する（記憶で書かない）
2. `pricing.json` の `version` と `fetched_at` を更新し、数値だけ差し替える
3. 過去1日分の `llm_usage` を新単価で再計算し、旧単価との差分を出す
4. 差分がしきい値（例: 20%）を超えたら `DAILY_LIMIT_USD` も見直す

単価表をコードから分離しておくと、この一連の作業が「設定ファイル1個の PR」で完結します。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 見積と請求が合わない | `usage` の方言フィールドを読んでいるか確認。キャッシュヒット/ミスを分けずに計算していないか |
| 日次上限を超える | 事前 `reserve` なしの同時実行が原因。reserve → commit 方式にする |
| 残高が突然尽きる | 機能別上限だけで全体上限が無い。全体熔断を必ず入れる |
| ピーク判定がずれる | タイムゾーンと祝日を外部ファイル化。日付をコードに直書きしない |
| 価格改定後に数字が合わない | `pricing.json` の version 更新漏れ。ログに `pricing_version` を残す |
| 新機能が予算をすり抜ける | 未登録 feature を `PermissionError` で落とす |

## まとめ

- 残高は本家、配分と遮断は外側。責務を分けると片方の変更でもう片方が壊れない
- コスト計算は `usage` の実測のみを信じ、キャッシュヒット/ミスを必ず分ける
- 単価・ピーク時間・祝日は設定ファイルに外出しし、その version をログに残す
- 全体熔断は「全停止」ではなく重要度順の段階遮断にする
- モデル名・単価・時間帯は必ず公式ドキュメントで確認する（本記事の数値は前提例）

## チェックリスト

- [ ] `pricing.json` に単価バージョンと取得日を書いた
- [ ] `usage` からキャッシュヒット/ミスを分けてコスト計算している
- [ ] 機能別の日次上限と、全体の日次上限の両方がある
- [ ] 事前 reserve → 実測 commit の2段構えになっている
- [ ] 全体上限に近づいたときの段階遮断ルールがある
- [ ] ピーク判定のタイムゾーンと祝日カレンダーを外出しした
- [ ] `llm_usage` に `pricing_version` を記録している
- [ ] 本家側の残高アラートも併用している