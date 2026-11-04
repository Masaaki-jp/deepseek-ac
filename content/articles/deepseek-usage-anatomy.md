---
title: "そのAPI、いくら燃えていますか？ ── DeepSeekのusageを解剖する"
description: "DeepSeek APIのusageは「キャッシュヒット／ミス／出力」の3区分で単価が桁違いに違う。区分別トークンの取り出し方、単価の外部化、feature単位のログ集計までを実装で示す。"
category: operations
tags:
  - DeepSeek
  - API
  - Python
  - コスト
  - キャッシュ
  - usage
publishedAt: 2026-11-04
draft: false
series: "コスト・性能"
order: 1
difficulty: intermediate
minutes: 12
---

## リード

DeepSeek APIの `usage` は、`total_tokens` だけ見ても役に立たない。課金は「キャッシュヒット」「キャッシュミス」「出力」の3区分で単価が桁違いに違うため、区分別に取り出して初めて「どこで燃えているか」が見える。この記事では、レスポンスからusageを抜き、コストに変換し、ログに貯めて集計するまでの最小構成を示す。

> 価格とレスポンスのフィールド名は改定される。本記事の数値・フィールド名は例として扱い、実装前に必ず公式ドキュメント（料金ページ／チャット補完のレスポンス例）で確認してほしい。

## 前提

- 想定読者: DeepSeek API を呼ぶコードを書き、請求額の内訳を把握したいエンジニア
- 必要な環境: Python 3.11 以上、`openai` SDK（OpenAI互換エンドポイント経由）
- 前提知識: トークンの概念、HTTPレスポンスの読み方

## 本文

### usage には3つの「財布」が入っている

DeepSeekのusageは、少なくとも次の3つに分解できる。

| 課金区分 | レスポンス上のフィールド（例） | 意味 |
|---|---|---|
| キャッシュヒット | `prompt_cache_hit_tokens` / `prompt_tokens_details.cached_tokens` | 過去リクエストと前方一致した入力。最も安い |
| キャッシュミス | `prompt_cache_miss_tokens` | 実際に計算が必要だった入力 |
| 出力 | `completion_tokens` | 生成されたトークン。最も高い |

関係は `prompt_tokens = キャッシュヒット + キャッシュミス`、`total_tokens = prompt_tokens + completion_tokens`。この等式が崩れていたら、フィールドの取りこぼしかSDKのバージョン差を疑う。

なぜ区分を見るか。公式のキャッシュ発表では「キャッシュヒット分の料金を桁違いに下げる」と説明されている。つまり**同じ `total_tokens` でも中身の比率で請求額が1桁変わる**。`total_tokens` に潰した時点で、改善の余地がログから消える。

### 単価はコードに埋め込まない

価格は改定される（値下げ、オフピーク割引など）。コードに直書きすると、過去のリクエストを誤った単価で再計算してしまい、ダッシュボードが静かに嘘をつく。単価はデータとして外出しし、未設定なら起動時に落とす。

```python
# pricing.py
# 単位: USD / 1M tokens
# 値は必ず公式の料金ページを確認して埋める。このファイルはコミットしない運用でもよい
PRICES = {
    "cache_hit":  None,
    "cache_miss": None,
    "output":     None,
}

def require_prices(prices: dict | None = None) -> dict:
    p = prices or PRICES
    missing = [k for k, v in p.items() if v is None]
    if missing:
        raise RuntimeError(
            f"単価が未設定: {missing} — 公式の料金ページを確認して埋めてください"
        )
    return p
```

`None` を `0` として扱わないのが肝。単価未設定が「コスト$0」として通ると、誰も気づかないまま予算を溶かす。**静かに間違えるより、起動時に落ちる方が安い。**

### レスポンスから区分別トークンを取り出す

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

resp = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "..."}],
)

usage = resp.usage
hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
miss = getattr(usage, "prompt_cache_miss_tokens", None)
if miss is None:
    miss = usage.prompt_tokens - hit  # フィールドが無いSDK向けのフォールバック
```

SDKの型定義に無いフィールドは、バージョンによっては属性として見えないことがある。検証できるよう、生の辞書も一緒に残しておく。

```python
raw = resp.model_dump().get("usage", {})
```

「型付きオブジェクトで取れなかったらrawを見る」という順序にしておくと、SDKを上げたときに差分で気づける。

### コストに変換する

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Usage:
    cache_hit: int
    cache_miss: int
    output: int

    @classmethod
    def from_response(cls, usage) -> "Usage":
        hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        if miss is None:
            miss = usage.prompt_tokens - hit
        return cls(cache_hit=hit, cache_miss=miss, output=usage.completion_tokens)

def to_usd(u: Usage, prices: dict | None = None) -> float:
    p = require_prices(prices)
    return (
        u.cache_hit  * p["cache_hit"]  / 1_000_000
        + u.cache_miss * p["cache_miss"] / 1_000_000
        + u.output     * p["output"]     / 1_000_000
    )
```

### 指標は「額」より「率」で見る

額はトラフィックに比例して増えるので、改善したのか単に使われたのかが区別できない。率にしておくと比較できる。

- **ヒット率** = `cache_hit / (cache_hit + cache_miss)` … プロンプト設計の質
- **出力比** = `output / total` … 生成側のコスト支配度

キャッシュは「リクエストの先頭から一致」した場合だけ効く。ツール定義の順序を毎回入れ替える、プロンプト冒頭にタイムスタンプやリクエストIDを置く——これだけでヒット率は0に落ちる。サードパーティの実測では、同じモデルでもハーネスの組み方だけでヒット率が99%超になり、入力コストが数十倍変わったという報告もある（外部ブログのため一次情報ではないが、方向性としては整合する）。

### ログに落とす（JSON Lines）

```python
import json
import time
from pathlib import Path

LOG = Path("logs/usage.jsonl")

def log_usage(feature: str, model: str, u: Usage, usd: float, raw: dict | None = None) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": round(time.time(), 3),
        "feature": feature,
        "model": model,
        "cache_hit": u.cache_hit,
        "cache_miss": u.cache_miss,
        "output": u.output,
        "usd": round(usd, 8),
    }
    if raw:
        rec["raw_usage"] = raw
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
```

JSON Lines を選ぶ理由は3つ。追記だけで壊れない、1行1リクエストなので `grep` で追える、後から pandas / polars / BigQuery にそのまま流せる。

**`feature` ラベルを付けるのが一番効く。** モデル単位の集計では「どの機能が燃えているか」は永久に分からない。

### 集計する（標準ライブラリだけで）

```python
import json
from collections import defaultdict
from pathlib import Path

def report(path: str = "logs/usage.jsonl") -> None:
    agg = defaultdict(lambda: {"usd": 0.0, "hit": 0, "miss": 0, "out": 0, "n": 0})
    for line in Path(path).open(encoding="utf-8"):
        r = json.loads(line)
        a = agg[r["feature"]]
        a["usd"] += r["usd"]
        a["hit"] += r["cache_hit"]
        a["miss"] += r["cache_miss"]
        a["out"] += r["output"]
        a["n"] += 1

    rows = []
    for f, a in agg.items():
        inp = a["hit"] + a["miss"]
        rows.append((f, a["usd"], a["hit"] / inp if inp else 0.0, a["n"]))

    for f, usd, hit_rate, n in sorted(rows, key=lambda x: -x[1]):
        print(f"{f:24s} ${usd:9.4f}  hit={hit_rate:6.1%}  n={n}")

if __name__ == "__main__":
    report()
```

出力例:

```text
chat                     $  12.4021  hit= 93.2%  n=4821
summarize                $   3.1100  hit=  4.8%  n=311
```

この例なら、まず `summarize` のプロンプトを見る。額が2番手でもヒット率が4.8%なので、伸びしろはこちらが大きい。

### 数字が合わないときに疑う順番

1. **単価表が古い** — 値下げ・割引の適用を反映していない
2. **前方一致になっていない** — 毎回変わる値（時刻、UUID、ツール順）が先頭にある
3. **キャッシュの保持は保証されない** — 時間が空いたリクエストはミスになる
4. **推論モデルの思考トークン** — 出力側に計上される扱いかどうかを公式で確認する
5. **ストリーミング時にusageが返っていない** — 最終チャンクにしか載らない実装がある

## つまずきポイント

| 症状 | 対処 |
|---|---|
| `usage` が None / 全フィールド0 | ストリーミング時のusage取得オプションを確認。非ストリーミングで一度叩いて比較する |
| キャッシュヒットが常に0 | プロンプト先頭に可変値を置いていないか確認。ツール定義の順序を固定する |
| `prompt_tokens != hit + miss` | SDKが独自フィールドを落としている。`model_dump()` のrawを確認 |
| 試算と請求が合わない | 単価表の更新漏れ、割引の適用漏れ、保持期間切れによるミスを順に潰す |
| コストが突然跳ねた | `feature` 別の出力比を見る。出力トークンは入力より高く、長文生成の追加が支配的になりやすい |

## まとめ

- usageは「キャッシュヒット／ミス／出力」の3区分。`total_tokens` に潰すと改善点が消える
- 単価はコードに埋め込まず外部化する。未設定なら例外で落とし、静かな誤集計を防ぐ
- `feature` ラベルとヒット率を日次で見れば、「どこが燃えているか」は特定できる

## チェックリスト

- [ ] usageを3区分（hit / miss / output）でログに残している
- [ ] 単価をコードに直書きせず、未設定時は例外にしている
- [ ] リクエストに `feature` ラベルを付けている
- [ ] ヒット率を日次で集計している
- [ ] `total_tokens` だけで見積もっていない
- [ ] 価格改定時に単価表を更新する運用が決まっている

## 参考

- 料金: https://api-docs.deepseek.com/quick_start/pricing
- キャッシュ発表（2024）: https://api-docs.deepseek.com/zh-cn/news/news0802
  - キャッシュの利用ガイドはこのページから辿れる

※ リンクと価格は執筆時点のもの。 latest は公式サイトのトップから辿って確認してほしい。
