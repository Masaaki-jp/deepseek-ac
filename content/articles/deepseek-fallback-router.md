---
title: "DeepSeek APIが落ちても、請求だけは跳ねない──フォールバック実装の設計原則"
description: "フォールバックは可用性を上げるが、設計を誤るとリトライで課金が数倍に膨らむ。エラー分類・「壁」と「揺らぎ」の判別・予算付きリトライ・降格先の選び方を、Pythonの実装例とともに整理する。"
category: operations
tags:
  - DeepSeek
  - フォールバック
  - 可用性
  - コスト
  - Python
publishedAt: 2026-11-11
draft: false
series: "運用"
order: 1
difficulty: intermediate
minutes: 14
---

## リード

フォールバックを入れると可用性は上がるが、設計を誤ると**同じリクエストを何度も課金するだけの装置**になる。本記事では「落ちたときどう繋ぐか」ではなく「落ちたときに請求を増やさない繋ぎ方」を設計原則として整理する。

## 前提

- 想定読者: DeepSeek API を本番に組み込み始めたエンジニア
- 必要な環境: Python 3.10+ / `openai` SDK（DeepSeek は OpenAI 互換エンドポイント）
- 前提知識: try/except、HTTP ステータスコード、指数バックオフの基礎

> 課金単位・レート制限ヘッダ・冪等キーのサポート有無などプロバイダ仕様は変わる。実装前に必ず公式ドキュメントで確認してほしい。本記事のコードは「分類と予算」の構造を示すもので、単価やヘッダ名は固定値として扱っていない。

## なぜフォールバックで請求が跳ねるのか

### 課金は「呼んだ回数」ではなく「生成したトークン」で発生する

タイムアウトで例外が返っても、サーバ側で生成が走っていればトークンは消費されている可能性がある。つまり**失敗したリクエストも無料ではない**。ここを「失敗＝コストゼロ」と誤解すると、リトライ回数をそのまま課金倍率だと見積もってしまう。

### 典型的な3つの請求事故

| 事故 | 何が起きるか | 倍率の目安 |
|---|---|---|
| 例外をすべてリトライ | 400 系（自分のバグ）まで再送し、失敗し続ける | リトライ回数分 |
| チェーン全体を無条件に試す | 3モデル×3リトライ＝最大9回送信 | 9倍 |
| 高価格モデルへ無条件フォールバック | 安いモデルが落ちて高いモデルに流れ続ける | 単価差×呼び出し量 |

特に3つ目は静かに効く。可用性が戻ったことに気づかず、降格先のまま1週間稼働するケースは珍しくない。

## 設計原則1: 例外を4種類に分類する

まず「万能 try/except で別モデルへ」を捨てる。例外は**待つ / 切る / 渡す / 落とす**の4つに分かれる。

| 分類 | 例 | 取るべき行動 | リトライ |
|---|---|---|---|
| TRANSIENT（揺らぎ） | 接続失敗、タイムアウト、500/502/503/504、短い 429 | 少し待って同じ宛先へ | する |
| WALL（壁） | 401/403、残高切れ、クォータ枯渇、長い Retry-After | リトライせず別宛先 or 停止 | しない |
| INVALID（自分のせい） | 400、messages 形式、tool 定義ミス | 即失敗。**別モデルに投げても無駄** | しない |
| UNKNOWN | 未知の 4xx サブコード | 1回だけ別宛先へ | 原則1回 |

INVALID をフォールバックさせないのが最重要。パラメータ不正はどのモデルでも不正であり、課金だけが積み上がる。

## 設計原則2: 「壁」と「揺らぎ」を分ける

429 は特に紛らわしい。**短い 429（数秒で回復）と、クォータ枯渇による 429（リセット時刻まで回复しない）は別物**。

- `Retry-After` が数十秒以上、またはレート制限の残量が 0 → **壁**。リトライせず、その宛先を一定時間「停車」させる
- `Retry-After` が数秒 → **揺らぎ**。ジッター付きバックオフで再試行

停車（クールダウン）を入れる理由は、壁に対してリトライを繰り返すと**成功しないリクエストに課金だけが発生し続ける**から。サーキットブレーカーの本質は可用性ではなくコスト防御でもある。

## 設計原則3: リトライに予算を持たせる

「回数」ではなく「予算」で縛る。最低限、次の3つを明示的に持つ。

1. **総試行回数の上限**（例: 3）
2. **1宛先あたりのリトライ上限**（例: 2）
3. **デッドライン**（例: 45秒。超えたら諦める）

デッドラインがないと、遅いモデルを待つ間にユーザーが離脱し、さらにタイムアウト後の再送で二重課金になる。

```python
# router.py
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)


class Kind(str, Enum):
    TRANSIENT = "transient"
    WALL = "wall"
    INVALID = "invalid"
    UNKNOWN = "unknown"


@dataclass
class Budget:
    max_attempts: int = 3
    max_retries_per_target: int = 2
    deadline_sec: float = 45.0
    attempts: int = 0
    started: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return time.monotonic() - self.started > self.deadline_sec

    def take(self) -> bool:
        if self.expired() or self.attempts >= self.max_attempts:
            return False
        self.attempts += 1
        return True


def classify(e: Exception) -> Kind:
    if isinstance(e, (APIConnectionError, APITimeoutError)):
        return Kind.TRANSIENT

    if isinstance(e, RateLimitError):
        return Kind.WALL if _is_quota_wall(e) else Kind.TRANSIENT

    if isinstance(e, APIStatusError):
        s = e.status_code
        if s in (401, 403):
            return Kind.WALL
        if s == 400:
            return Kind.INVALID
        if s in (408, 409, 500, 502, 503, 504):
            return Kind.TRANSIENT
        if s == 429:
            return Kind.TRANSIENT

    return Kind.UNKNOWN


def _is_quota_wall(e: RateLimitError) -> bool:
    """Retry-After が長い / 残量 0 なら壁とみなす。ヘッダ名は公式ドキュメントで要確認。"""
    headers = getattr(getattr(e, "response", None), "headers", {}) or {}
    retry_after = headers.get("retry-after")
    try:
        return retry_after is not None and float(retry_after) >= 60
    except (TypeError, ValueError):
        return False
```

## 設計原則4: 降格先は「安い方」に並べる

チェーンの順序は「品質順」ではなく**「品質÷コスト」順**にする。

```python
# 上から順に試す。下に行くほど安い＝品質は落ちる
CHAIN = [
    {"name": "reasoner", "model": "deepseek-reasoner", "max_tokens": 4096},
    {"name": "chat",     "model": "deepseek-chat",     "max_tokens": 2048},
    {"name": "local",    "model": "qwen2.5-7b-instruct", "max_tokens": 1024},
]
```

ポイントは2つ。

- **降格時は `max_tokens` を絞る**。止血が目的なので、長文生成まで許すとコストが読めなくなる
- **クラウドだけでチェーンを閉じない**。同一アカウントの残高切れ・リージョン障害では全クラウドが同時に死ぬ。ローカルモデルか静的応答を最後に置く

## 設計原則5: 全滅時の出口を先に決める

チェーンが尽きたときの挙動を、実装前に決めておく。ここが未定義だと `500` がそのまま返り、SSE ストリーミングでは接続が硬断されてリトライループの引き金になる。

選択肢は3つ。

- **静的応答**（「混雑しています。○分後に再試行してください」）
- **人間へのエスカレーション**（問い合わせフォーム、チャット引き継ぎ）
- **キャッシュ済み回答の提示**（同一プロンプトの過去応答）

いずれも**課金ゼロで返せる**経路であることが条件。

## 設計原則6: 実際に使われたモデルを返す・記録する

「設定上のモデル」ではなく「実際に応答したモデル」を返す。これを間違えると、降格が起きていることに誰も気づかず、安いモデルの回答が高いモデルの成果として扱われる。

```python
@dataclass
class Reply:
    text: str
    provider: str        # 実際に応答した宛先
    degraded: bool       # プライマリ以外なら True
    usage: object | None


def ask(messages: list[dict], budget: Budget | None = None) -> Reply:
    budget = budget or Budget()
    last_error: Exception | None = None

    for idx, target in enumerate(CHAIN):
        for attempt in range(budget.max_retries_per_target + 1):
            if not budget.take():
                raise TimeoutError("budget exhausted") from last_error

            try:
                client = _client(target)  # base_url / api_key は環境変数から
                resp = client.chat.completions.create(
                    model=target["model"],
                    messages=messages,
                    max_tokens=target["max_tokens"],
                    timeout=30.0,
                )
                return Reply(
                    text=resp.choices[0].message.content or "",
                    provider=target["name"],
                    degraded=idx > 0,
                    usage=resp.usage,
                )
            except Exception as e:  # noqa: BLE001
                last_error = e
                kind = classify(e)

                # 自分のリクエスト不正：他のモデルでも同じ結果。即座に降りる
                if kind is Kind.INVALID:
                    raise

                # 壁：この宛先は諦めて次の宛先へ
                if kind is Kind.WALL:
                    break

                # 揺らぎ：ジッター付きバックオフ
                if kind is Kind.TRANSIENT and attempt < budget.max_retries_per_target:
                    time.sleep(min(2 ** attempt + random.random(), 8.0))
                    continue

                break

    raise RuntimeError("all targets failed") from last_error
```

`degraded=True` をログと UI の両方に出す。「降格応答率」をメトリクスにしておけば、可用性が戻ったタイミングでチェーン先頭に戻す判断もできる。

## 設計原則7: ヘルスチェックを課金パスに置かない

「DeepSeek が生きているか」を毎分チャット補完で確認すると、それ自体が課金対象になる。監視は次の順で安い手段を選ぶ。

1. 実際のトラフィックのエラー率（追加コストゼロ）
2. TCP/TLS 接続や `/models` などの非生成エンドポイント（課金有無は要確認）
3. どうしても生成が必要なら、低頻度＋`max_tokens=1`

## つまずきポイント

| 症状 | 対処 |
|---|---|
| フォールバックしたのに請求が増え続ける | `INVALID` をリトライしていないか確認。400 は即 raise |
| 429 で無限リトライしている | `Retry-After` を読み、長ければ「壁」として停車させる |
| 気づいたら高いモデルで動いていた | `degraded` フラグをメトリクス化し、復帰時に自動で先頭へ戻す |
| ストリーミングで接続が切れて二重課金 | 途中までの生成も消費される。中断時は再送ではなく静的応答を返す |
| 全モデルが同時に落ちた | クラウドのみのチェーンは単一障害点。ローカル or 静的応答を最終段に置く |
| タイムアウト待ちでユーザーが離脱 | デッドラインを全体で1本持ち、宛先ごとの timeout と分離する |

## まとめ

- フォールバックの設計単位は「可用性」ではなく「**可用性 ÷ 課金増加**」
- 例外は TRANSIENT / WALL / INVALID / UNKNOWN に分類し、INVALID は絶対にリトライしない
- 429 は「壁」と「揺らぎ」に分ける。壁は停車、揺らぎはジッター付きバックオフ
- リトライは回数ではなく「総試行数・宛先別上限・デッドライン」の3点で予算化する
- 降格先は安い順に並べ、`max_tokens` を絞り、最後は課金ゼロの出口で閉じる

## チェックリスト

- [ ] 例外を4分類し、INVALID でリトライしないことをテストで固定した
- [ ] 429 の `Retry-After` を読み、長い場合にクールダウンする実装がある
- [ ] 総試行回数・宛先別リトライ上限・全体デッドラインの3つを設定した
- [ ] チェーン末尾がクラウド以外（ローカル or 静的応答）になっている
- [ ] 降格時に `max_tokens` を下げている
- [ ] 実際に応答したモデル名を返し、`degraded` をメトリクスに記録している
- [ ] ヘルスチェックを生成エンドポイントに置いていない
- [ ] 全滅時の応答文を、課金ゼロで返せる形で用意した
