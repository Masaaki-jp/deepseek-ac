---
title: "DeepSeek APIの予算アラートと上限設定 — 公式に無いなら3層で作る"
description: "DeepSeek API には OpenAI のような予算アラートも hard limit もありません。残高による暗黙の上限だけに頼らず、残高監視・アプリ側のコスト計測・Harness の dsh-budget の3層で警告と上限を実装する方法をまとめます。"
category: operations
tags:
  - DeepSeek
  - コスト管理
  - API
  - Python
publishedAt: 2026-11-07
draft: false
series: "DeepSeek APIのコスト管理"
order: 1
difficulty: intermediate
minutes: 12
---

## リード

DeepSeek API には、OpenAI や Anthropic にあるような「管理画面で支出上限を設定する」機能がありません。
この記事では、残高監視・アプリ側のコスト計測・Harness プラグインの3層で、予算アラートと上限をどう作るかを示します。

## 前提

- 想定読者: DeepSeek API を本番または社内ツールで使い始めたエンジニア
- 必要な環境: Python 3.10+、`requests`、DeepSeek API キー
- 前提知識: Chat Completions API の基本、レスポンスの `usage` フィールド

> 本記事は「上限とアラートの作り方」に限定します。トークン削減やプロンプト圧縮といった単価を下げる施策は別記事の領域です。

## 結論：DeepSeek 本体に予算アラートは無い

2026年8月時点で公開されている比較情報では、DeepSeek の費用管理は4社の中で最も簡素です。

| 機能 | DeepSeek の状況 |
|---|---|
| ハードな支出上限 | 無し（残高ゼロが実質的な上限） |
| 予算アラート | 無し |
| プロジェクト単位の分離 | 無し（アカウント単位） |
| 上限到達時の挙動 | 残高不足で API 呼び出しが失敗 |
| コンソールの用量表示 | 基本的な統計のみ |

つまり提供されている統制は「プリペイド残高」と「段階的なレート制限」の2つだけです。

### 残高＝上限、では足りない理由

残高は確かにハードリミットとして機能しますが、実務では不十分です。

- **失敗の粒度が粗い**。残高が尽きた瞬間に、無関係なバッチも対話ツールも一斉に落ちます。
- **事前に気づけない**。コンソールを見に行かない限り、残高が減っていることに気づきません。
- **誰がいくら使ったか分からない**。アカウント単位でしか分離できないため、チーム利用では原因追跡ができません。

したがって、次の3層を重ねます。

1. 残高の監視と閾値通知（プラットフォーム側の唯一の統制点を監視する）
2. アプリ側でのコスト計測と上限（`alert` / `block` / `degrade` の判断を持つ）
3. Harness を使っている場合は `dsh-budget` プラグインに委譲する

## 第1層：残高を監視して閾値で通知する

DeepSeek は残高照会のエンドポイントを提供しています（エンドポイント名とレスポンス形状は[公式ドキュメント](https://api-docs.deepseek.com/)の "Get User Balance" で必ず確認してください）。
これを定期実行し、閾値を下回ったら webhook に投げます。

```python
# balance_watch.py
import json
import os
import pathlib

import requests

API_BASE = "https://api.deepseek.com"
API_KEY = os.environ["DEEPSEEK_API_KEY"]
WEBHOOK_URL = os.environ["ALERT_WEBHOOK_URL"]

WARN_USD = 30.0    # この金額を下回ったら警告
CRITICAL_USD = 10.0
STATE = pathlib.Path("./.balance_state.json")


def fetch_balance() -> float:
    """残高（USD 相当）を返す。レスポンス形状は公式ドキュメントで要確認。"""
    res = requests.get(
        f"{API_BASE}/user/balance",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=10,
    )
    res.raise_for_status()
    infos = res.json()["balance_infos"]
    usd = [i for i in infos if i.get("currency") == "USD"]
    target = usd[0] if usd else infos[0]
    return float(target["total_balance"])


def notify(level: str, balance: float) -> None:
    text = f"[DeepSeek] 残高 {level}: ${balance:.2f}"
    requests.post(WEBHOOK_URL, json={"text": text}, timeout=10)


def load_state() -> str:
    if STATE.exists():
        return json.loads(STATE.read_text()).get("level", "ok")
    return "ok"


def main() -> None:
    balance = fetch_balance()
    if balance <= CRITICAL_USD:
        level = "critical"
    elif balance <= WARN_USD:
        level = "warn"
    else:
        level = "ok"

    prev = load_state()
    # 悪化した瞬間だけ通知する（毎回送らない）
    if level != prev and level != "ok":
        notify(level, balance)

    STATE.write_text(json.dumps({"level": level, "balance": balance}))
    print(f"balance={balance:.2f} level={level}")


if __name__ == "__main__":
    main()
```

**なぜ「悪化した瞬間だけ」通知するか**：毎時送るとアラートが無視されるようになります。レベル遷移（ok → warn → critical）でのみ発火させ、復旧時は静かに戻すのが運用上もっとも破綻しにくい形です。
cron なら `17 * * * *` のように毎時動かし、`STATE` はコンテナの外に置いてください。

## 第2層：アプリ側でコストを計測して上限をかける

残高監視は「気づく」だけで、止める力はありません。止める・落とす判断はアプリ側に持たせます。

### 単価テーブルは設定ファイルに外出しする

```python
# prices.py — 値は例示。必ず公式の料金表で確認し、改定時に更新すること
PRICES = {
    "deepseek-chat": {
        "input_cache_hit":  0.03,   # USD / 1M tokens
        "input_cache_miss": 0.28,
        "output":           0.42,
    },
}

# 非高峰時間帯の割引係数（提供状況・時間帯は公式で要確認）
OFF_PEAK_FACTOR = 0.5
OFF_PEAK_HOURS_UTC = range(16, 24)
```

単価をコードに直書きしない理由は、**価格改定と非高峰割引の両方に追従する必要がある**からです。特に非高峰割引は「同じトークン数でも時刻で単価が変わる」ため、平均単価で見積もると数割ずれます。

### `usage` からコストを計算する

```python
from datetime import datetime, timezone

from prices import PRICES, OFF_PEAK_FACTOR, OFF_PEAK_HOURS_UTC


def cost_usd(model: str, usage: dict, at: datetime | None = None) -> float:
    p = PRICES[model]

    hit = usage.get("prompt_cache_hit_tokens", 0)
    miss = usage.get("prompt_cache_miss_tokens", 0)
    if not hit and not miss:
        # キャッシュ内訳が返らない場合のフォールバック
        miss = usage.get("prompt_tokens", 0)

    out = usage.get("completion_tokens", 0)

    at = at or datetime.now(timezone.utc)
    factor = OFF_PEAK_FACTOR if at.hour in OFF_PEAK_HOURS_UTC else 1.0

    return factor * (
        hit * p["input_cache_hit"]
        + miss * p["input_cache_miss"]
        + out * p["output"]
    ) / 1_000_000
```

キャッシュヒットとミスを分けて計上するのが要点です。ヒット率が高いワークロードでは、`prompt_tokens` だけを見るとコストを過大評価します。

### 日次・月次の累計と上限判定

```python
import sqlite3
from datetime import date, datetime, timezone

DB = "budget.db"


def init() -> None:
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS spend (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            model TEXT NOT NULL,
            usd REAL NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()


def record(user_id: str, model: str, usd: float) -> None:
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO spend (user_id, model, usd, created_at) VALUES (?, ?, ?, ?)",
        (user_id, model, usd, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()


def total_since(prefix: str) -> float:
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT COALESCE(SUM(usd), 0) FROM spend WHERE created_at >= ?",
        (prefix,),
    ).fetchone()
    con.close()
    return float(row[0])


class BudgetGuard:
    """戻り値: "ok" | "warn" | "block" """

    def __init__(self, daily_cap=20.0, monthly_cap=400.0, warn_ratio=0.8):
        self.daily_cap = daily_cap
        self.monthly_cap = monthly_cap
        self.warn_ratio = warn_ratio

    def check(self) -> str:
        now = datetime.now(timezone.utc)
        day = total_since(now.strftime("%Y-%m-%dT00:00:00"))
        month = total_since(now.strftime("%Y-%m-01T00:00:00"))

        if day >= self.daily_cap or month >= self.monthly_cap:
            return "block"
        if day >= self.daily_cap * self.warn_ratio:
            return "warn"
        if month >= self.monthly_cap * self.warn_ratio:
            return "warn"
        return "ok"
```

呼び出し側はこうなります。

```python
guard = BudgetGuard()

decision = guard.check()
if decision == "block":
    raise RuntimeError("budget exceeded")   # 呼ばずに返す / キューに退避
if decision == "warn":
    logger.warning("budget near limit")

res = client.chat.completions.create(...)
usd = cost_usd(res.model, res.usage.model_dump())
record(user_id, res.model, usd)
```

**なぜ `block` を例外にするか**：`degrade`（安いモデルに落とす、`max_tokens` を絞る）で継続したいケースも多いはずです。ポリシーを3値で返す設計にしておけば、後から「警告のみ」「遮断」「劣化運転」を切り替えられます。後述の `dsh-budget` も同じ三分類なので、設計を揃えておくと移行が楽です。

## 第3層：DeepSeek Harness なら `dsh-budget` に任せる

Harness（`@deepseek-ai/dsh`）経由で DeepSeek を使っているなら、コスト管理はプラグインに寄せられます。

```bash
npx -p @deepseek-ai/dsh dsh plugin --profile web add github:PerryLink/dsh-budget
```

主な設定キー（すべて Schemastery で検証され、不正値はロード時に失敗します）:

| キー | 意味 |
|---|---|
| `prices` | モデル別の 1M トークン単価。組み込みテーブルにマージされる |
| `defaultPrice` | 単価未定義モデルの既定値 |
| `budgets.session` / `budgets.daily` / `budgets.monthly` | USD での上限。空欄は「無制限」 |
| warn 比率 | 閾値に達したら webhook + デスクトップ通知 |
| 超過時ポリシー | `alert` / `block` / `degrade` の3択 |

セッション・日次・月次で集計し、モデル別のレイテンシ統計やカーボンフットプリント推定も同じパネルに出ます。設定は `cordis.patch.yml` にキーごとのコメント付きで載っているので、まずそれを読むのが早いです。

**導入前の確認事項**：

- 互換バージョンは Harness 0.1.1-rc.2 系、Node は `^22.19.0 || >=24.0.0`。手元のバージョンと突き合わせてください。
- あくまで Harness 経由の呼び出しが対象です。Harness を通らないバッチや別クライアントは計測されません。第2層と併用する前提で考えてください。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| ある日突然すべての呼び出しが失敗 | 残高切れ。第1層の閾値通知を先に入れる。上限は残高ではなくアプリ側で持つ |
| 見積りより請求が大きい | キャッシュヒット/ミスを分けて計上する。`output` は `input` より単価が高い |
| リトライで二重計上 | リクエストIDで重複排除。失敗時は `usage` が返らない点に注意 |
| 夜間バッチのコストが合わない | 非高峰割引を平均単価で潰している。時間帯係数を入れる |
| 誰が使ったか分からない | DeepSeek はアカウント単位でしか分離できない。アプリ側で `user_id` を必ず記録する |
| アラートが多すぎて無視される | レベル遷移時のみ通知。復旧時は静かに戻す |

## まとめ

- DeepSeek API に予算アラートとハード上限は無い。残高が実質的な上限だが、失敗の粒度が粗く事前に気づけない
- 残高監視（気づく）＋ アプリ側の計測と判定（止める）の2層が最小構成。Harness 利用時は `dsh-budget` に寄せられる
- 単価は設定に外出しし、キャッシュヒット/ミスと非高峰割引を分けて扱う。ここを平均化すると見積りが崩れる

## チェックリスト

- [ ] 残高照会のエンドポイントとレスポンス形状を公式ドキュメントで確認した
- [ ] 残高の閾値通知を cron 等で定期実行し、レベル遷移時のみ発火するようにした
- [ ] 単価テーブルをコードから分離し、改定時に1ファイル更新で済むようにした
- [ ] キャッシュヒット/ミスを分けてコスト計上している
- [ ] `alert` / `block` / `degrade` のどのポリシーを採るか決め、実装に反映した
- [ ] `user_id` をログに残し、アカウント単位の集計から原因追跡できるようにした
- [ ] Harness を使っている場合、`dsh-budget` の互換バージョンを確認した
