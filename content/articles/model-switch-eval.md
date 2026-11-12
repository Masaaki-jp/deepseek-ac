---
title: "DeepSeek APIを黙って差し替えるな——モデル切り替えで事故らないための評価基盤"
description: "モデル切り替えの事故は「動いた」で判断した瞬間に始まる。評価セットを先に作り、同一基準で比較・フォールバック・ロールバックできる仕組みを、DeepSeek APIを例にPythonで実装する。"
category: operations
tags:
  - DeepSeek
  - 評価
  - モデル切り替え
  - Python
publishedAt: 2026-11-12
draft: false
series: "運用"
difficulty: intermediate
minutes: 14
---

## リード

モデル差し替えの事故は「レスポンスが返ってきた」で判断した瞬間に始まる。この記事では、差し替え前に作るべき評価セットと、比較・フォールバック・ロールバックを同じ基準で回す最小構成を、DeepSeek API を例に Python で実装する。

## 前提

- 想定読者: DeepSeek API を本番または PoC で使い、モデル更新やコスト最適化のために差し替えを検討しているエンジニア
- 必要な環境: Python 3.10+、`openai` パッケージ（OpenAI 互換クライアント）、DeepSeek の API キー
- 前提知識: Chat Completions の基本的な呼び出し、CI の基礎

> モデル名・料金・スナップショットの提供有無は変動が早い。本記事のコードは環境変数でモデル名を注入する形にしてある。実際の値は必ず公式ドキュメントで要確認。

## なぜ「黙って差し替え」が事故るのか

差し替えの成否は、実は3層に分かれている。

| 層 | 意味 | 見える場所 |
|---|---|---|
| Transport 成功 | HTTP 200 が返り、テキストが入っている | 監視ダッシュボード |
| Contract 成功 | 期待した形（JSON キー、日付形式、必須項目）になっている | パース処理のログ |
| Product 成功 | ユーザーの目的が達成された | ほぼ見えない |

標準的なフォールバック実装は 1 層目しか保証しない。プライマリが落ちてフォールバックが応答し、ダッシュボードが緑になる。しかし契約違反が混ざっていても、出力が微妙に劣化していても、ログには何も残らない。

さらに厄介なのは、**同じモデル名でも中身は変わる**という点。提供側の更新・安全調整・既定パラメータの変更で、同一入力への応答は時間とともに変化する（モデルドリフト）。つまり「先月の結果」は再現しない前提で運用する必要がある。

## 差し替え前に評価セットを作る

順序が重要で、**評価セットは差し替えの前に作る**。差し替え後に作ると、無意識に新モデルの出力へ期待値を寄せてしまう。

評価セットの作り方:

- 本番ログから拾う。成功例より**失敗例・境界例**を優先する
- 件数は 30〜50 件から。網羅性より「壊れたら困るもの」を入れる
- 期待する出力そのものは書かない。**判定条件**を書く（正規表現、必須キー、数値範囲）
- ビジネス影響度で `weight` を付ける。請求書の日付は weight 3、雑談は 1

```json
[
  {
    "id": "invoice-date-001",
    "input": "請求書の支払期日をYYYY-MM-DD形式のみで返してください: 2026年12月3日",
    "assert": { "type": "regex", "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
    "weight": 3
  },
  {
    "id": "json-summary-004",
    "input": "次の文を{\"title\": string, \"amount\": number}のJSONのみで返してください: 備品購入 12,000円",
    "assert": { "type": "json_keys", "keys": ["title", "amount"] },
    "weight": 2
  },
  {
    "id": "refusal-scope-002",
    "input": "社内規定にない休暇制度を断定してください",
    "assert": { "type": "contains", "values": ["確認"] },
    "weight": 2
  }
]
```

ポイントは、**ゴールデン出力（正解テキスト）を固定しない**こと。文章の言い回しはモデルごとに変わるので、固定すると全件差分になり、評価が「文章の一致度」に堕ちる。

## 実装: 最小の評価ハーネス

`evals/run.py`。1 モデル分を回して集計するだけの構成。

```python
import json, os, re, time
from pathlib import Path
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)

def strip_fence(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())

def check(rule: dict, text: str) -> bool:
    kind = rule["type"]
    if kind == "regex":
        return re.search(rule["pattern"], text) is not None
    if kind == "contains":
        return all(s in text for s in rule["values"])
    if kind == "json_keys":
        try:
            obj = json.loads(strip_fence(text))
        except json.JSONDecodeError:
            return False
        return all(k in obj for k in rule["keys"])
    raise ValueError(f"unknown rule type: {kind}")

def run_case(model: str, case: dict) -> dict:
    started = time.perf_counter()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": case["input"]}],
        temperature=0,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    text = resp.choices[0].message.content or ""
    return {
        "id": case["id"],
        "passed": check(case["assert"], text),
        "weight": case.get("weight", 1),
        "latency_ms": latency_ms,
        "prompt_tokens": resp.usage.prompt_tokens,
        "completion_tokens": resp.usage.completion_tokens,
    }

def summarize(results: list[dict], price_in: float, price_out: float) -> dict:
    total_w = sum(r["weight"] for r in results)
    passed_w = sum(r["weight"] for r in results if r["passed"])
    latencies = sorted(r["latency_ms"] for r in results)
    p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)]
    cost = sum(
        r["prompt_tokens"] / 1e6 * price_in + r["completion_tokens"] / 1e6 * price_out
        for r in results
    )
    return {
        "score": round(100 * passed_w / total_w, 1),
        "failures": [r["id"] for r in results if not r["passed"]],
        "p95_latency_ms": round(p95),
        "cost_usd": round(cost, 4),
    }
```

`temperature=0` でも完全な再現性は保証されない。だから評価は「1 回の点数」ではなく、必要なら 3 回回して不安定なケースを炙り出す。

実行部はこう。

```python
def main() -> int:
    cases = json.loads(Path("evals/cases.json").read_text())
    prices = json.loads(Path("evals/prices.json").read_text())
    model = os.environ["DEEPSEEK_MODEL"]
    results = [run_case(model, c) for c in cases]
    p = prices[model]
    summary = summarize(results, p["input"], p["output"])
    print(json.dumps({"model": model, **summary}, ensure_ascii=False, indent=2))
    Path(f"evals/reports/{model}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    return 0 if not summary["failures"] else 1
```

`prices.json` はモデル名をキーにした単価表にしておく。単価をコードに埋めると、価格改定のたびにコードを触ることになる。

```json
{
  "deepseek-chat": { "input": 0.0, "output": 0.0 }
}
```

> 単価は必ず公式の料金ページで確認し、そこから転記する。単位は「100万トークンあたりの USD」に揃えると比較が楽。

## 合格ラインをコードで決める（ゲート）

評価は点数を出すだけでは意味がない。**通すか止めるかを機械が決める**必要がある。

```python
def gate(baseline: dict, candidate: dict, th: dict) -> list[str]:
    failures = []
    drop = baseline["score"] - candidate["score"]
    if drop > th["max_score_drop"]:
        failures.append(f"品質スコアが {drop:.1f}pt 低下（許容 {th['max_score_drop']}pt）")
    if candidate["failures"]:
        failures.append(f"契約違反 {len(candidate['failures'])} 件: {candidate['failures']}")
    if candidate["p95_latency_ms"] > baseline["p95_latency_ms"] * th["max_latency_ratio"]:
        failures.append("p95レイテンシが許容倍率を超過")
    if candidate["cost_usd"] > baseline["cost_usd"] * th["max_cost_ratio"]:
        failures.append("コストが許容倍率を超過")
    return failures
```

閾値の初期値は「スコア低下 2pt まで、レイテンシ 1.3 倍まで、コスト 1.0 倍まで」。安いモデルへの差し替えが目的なら、コストは下がる方向にしか動かないはずで、下がらなければ設定ミスを疑える。

これを CI に入れる。

```yaml
- name: Model gate
  run: |
    python -m evals.run > /dev/null
    python -m evals.gate --baseline evals/reports/baseline.json \
                         --candidate evals/reports/candidate.json
```

ベースラインは「現行モデルの直近レポート」を固定ファイルとしてリポジトリに置く。更新するのは差し替えを承認したときだけ。

## フォールバックは「同じ基準」で

フォールバックは可用性の話に見えるが、本質は品質の話。応答が返ることと、使える応答であることは違う。

現実的な設計はこうなる。

1. タスク種別ごとに、各モデルの評価スコアを事前に持っておく
2. フォールバック候補は「そのタスクで基準スコアを満たすモデル」に限定する
3. 満たす候補がなければ、中途半端なモデルで答えるのではなく**縮退モード**（定型文＋人間エスカレーション）に落とす

```python
FALLBACK_POLICY = {
    "invoice_extraction": ["model-a", "model-b"],  # 基準を満たす順
    "chitchat": ["model-a", "model-b", "model-c"],
}

def call_with_same_bar(task: str, messages: list[dict]):
    for model in FALLBACK_POLICY[task]:
        try:
            out = client.chat.completions.create(
                model=model, messages=messages, temperature=0
            ).choices[0].message.content
        except Exception:
            continue
        if out:
            return out, model
    return "担当者に確認します。少々お待ちください。", "degraded"
```

出力単体を見て「良さそう」と判定するのは不安定なので、**モデル×タスクの事前スコアで候補を絞る**方が再現性が高い。事前スコアは上の評価ハーネスをタスク別ケースで回せばそのまま得られる。

## 運用に組み込む

- **モデル名と実行日時を必ずログに残す**。モデル名がエイリアスで最新版を指す場合、名前だけでは実体を特定できない
- 評価レポートをリポジトリにコミットし、差分で劣化を追う
- 本番反映はカナリアから。1〜5% のトラフィックで契約違反率を監視し、閾値超過で自動ロールバック
- LLM-as-judge を使うなら、**被評価モデルと別系統のモデル**で、ルーブリックを固定して回す。同一モデルで自己採点させると甘く出る

## つまずきポイント

| 症状 | 対処 |
|---|---|
| スコアが想定より高い | 期待値を新モデルに寄せていないか確認。判定条件が緩すぎる可能性 |
| 全ケースが失敗する | JSON がコードフェンスで包まれている。`strip_fence` を通す |
| judge の点数がやたら高い | 同一モデルで自己評価している。別系統モデル＋固定ルーブリックに変える |
| フォールバック後も緑なのに苦情が来る | Transport / Contract しか見ていない。Product 層のサンプル監査を足す |
| モデル名を変えたのに差分ゼロ | エイリアスが同じ実体を指している、または応答キャッシュが効いている |
| レイテンシだけ悪化 | 評価セットの大半が短文。長文ケースを weight 付きで追加する |

## まとめ

- 差し替えの判断材料は「動いた」ではなく、事前に作った評価セットの点数と契約違反数
- モデル名は実体を固定しない。実行日時とモデル名をログに残し、ドリフトを前提に運用する
- フォールバックは可用性ではなく品質の設計。基準を満たさないなら縮退モードに落とす

## チェックリスト

- [ ] 評価セットを差し替え**前**に作った（30件以上、失敗例・境界例を含む）
- [ ] 期待値はゴールデン出力ではなく判定条件で書いた
- [ ] ビジネス影響度で weight を付けた
- [ ] ベースラインのレポートをリポジトリに固定した
- [ ] ゲートの閾値（スコア・レイテンシ・コスト）をコードで定義した
- [ ] フォールバック候補をタスク別の事前スコアで絞った
- [ ] 縮退モードの文言とエスカレーション先を決めた
- [ ] モデル名と実行日時をログに残している
