---
title: "単一モデル依存の危険と対策 — DeepSeek一本足打法をやめる設計"
description: "単一AIモデルへの依存は、API停止・価格改定・規制変更・性能陳腐化の4つのリスクを同時に負う。呼び出し層・コンテキスト層・メモリ層を分離し、フォールバックと評価セットで乗り換え可能性を確保する具体策を、Pythonコード付きで解説する。"
category: operations
tags:
  - DeepSeek
  - マルチモデル
  - フォールバック
  - ベンダーロックイン
  - 可用性
publishedAt: 2026-11-08
draft: false
series: "運用"
order: 3
difficulty: intermediate
minutes: 12
---

## リード

DeepSeek を本番導入したあと「もし API が止まったら」「価格が倍になったら」を考えずに済ませていませんか。
単一モデル依存は、可用性・コスト・規制・性能の4方向から同時に事業リスクになります。
この記事では、依存を「ゼロにする」のではなく「いつでも乗り換えられる状態にしておく」ための設計とコードを示します。

## 前提

- 想定読者: DeepSeek API を本番または社内ツールで使い始めたエンジニア
- 必要な環境: Python 3.11+、`openai` SDK（DeepSeek API は OpenAI 互換エンドポイントを提供）
- 前提知識: Chat Completions API の基本、環境変数によるキー管理

> モデル名・エンドポイント・料金は変更が速い領域です。本記事のコードは設定値として外出しし、実際の値は必ず公式ドキュメントで確認してください。

## なぜ単一モデル依存が危険なのか

リスクは「障害」だけではありません。実務では次の4つが同じ重さで効いてきます。

| リスク | 具体例 | 影響が顕在化する速さ |
|---|---|---|
| 可用性 | API 障害、レート制限、リージョン障害 | 分〜時間 |
| コスト | 価格改定、キャッシュ仕様変更、無料枠廃止 | 月〜四半期 |
| 規制・地政学 | 輸出管理、政府指令による提供停止、データ主権要件 | 日〜年 |
| 性能陳腐化 | 競合モデルの性能向上、自社タスクでの相対劣化 | 四半期〜年 |

マイクロソフト CEO のサティア・ナデラ氏は、単一の AI プロバイダーへの長期依存が自律性と競争力を損なうと繰り返し警告し、プロンプト・コンテキスト・メタデータを自社で管理すること、モデル層とメモリ層を分離することを提言していると報じられています。
また、政府指令を受けて特定モデルの提供が広域停止したと報じられた事例もあり、これは「性能の問題」ではなく「ある日突然使えなくなる」リスクが現実であることを示します（いずれも報道ベースの情報です。意思決定に使う場合は一次情報を確認してください）。

重要なのは、これらが**排他的でない**ことです。価格改定のタイミングで障害が起きる、規制対応で乗り換えたいのに移行コストが高すぎて動けない——という複合が実際の詰み筋になります。

## 対策の全体像 — 3層に分離する

ナデラ氏の提言と同じ方向で、次の3層を分けて考えます。

1. **呼び出し層**: どのモデルに投げるかを決める。ここにプロバイダ固有の SDK を閉じ込める
2. **コンテキスト層**: プロンプト、システム指示、Few-shot、ツール定義。プロバイダ非依存の形式で保持する
3. **メモリ層**: 会話履歴、生成結果、メタデータ。自社ストレージに残す

DeepSeek 固有の最適化（`deepseek-reasoner` の推論トークン扱いなど）は呼び出し層の内側にだけ書きます。コンテキスト層とメモリ層に漏らすと、乗り換え時に全部書き直しになります。

## 実装1: プロバイダを差し替え可能にする

まず最小の抽象を切ります。Protocol を使うと型チェックも効き、依存を1ファイルに閉じ込められます。

```python
# llm/base.py
from typing import Protocol, Any

class LLMClient(Protocol):
    name: str
    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str: ...
```

DeepSeek 実装。OpenAI SDK の `base_url` を差し替えるだけで動きます。

```python
# llm/deepseek.py
import os
from openai import OpenAI

class DeepSeekClient:
    name = "deepseek"

    def __init__(self, model: str | None = None) -> None:
        self._client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        # モデル名は設定から注入する（ハードコードしない）
        self._model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        resp = self._client.chat.completions.create(
            model=self._model, messages=messages, **kwargs
        )
        return resp.choices[0].message.content or ""
```

> 代替プロバイダ側も同じ Protocol を満たすクラスを1つ書くだけで済みます。ここで初めて「乗り換え可能」が成立します。

## 実装2: フォールバックとタイムアウト

フォールバックは全リクエストに適用するとコストが倍になります。**クリティカルパスだけ**に限定するのが現実的です。

```python
# llm/chain.py
import logging, time

log = logging.getLogger(__name__)

class FallbackChain:
    def __init__(self, clients: list[LLMClient], timeout_s: float = 20.0):
        self.clients = clients
        self.timeout_s = timeout_s

    def complete(self, messages, **kwargs) -> tuple[str, str]:
        errors = []
        for c in self.clients:
            t0 = time.perf_counter()
            try:
                out = c.complete(messages, timeout=self.timeout_s, **kwargs)
                log.info("provider_ok provider=%s latency_ms=%d", c.name,
                         (time.perf_counter() - t0) * 1000)
                return out, c.name
            except Exception as e:
                log.warning("provider_ng provider=%s err=%r", c.name, e)
                errors.append((c.name, repr(e)))
        raise RuntimeError(f"all providers failed: {errors}")
```

設計上の注意点を3つ挙げます。

- **構造化出力を使うならスキーマ検証を挟む**。モデルが変わると JSON の崩れ方が変わり、パース失敗がフォールバック先で起きます
- **タイムアウトはプロバイダ別に分ける**。推論系モデルはレイテンシが大きく跳ねるため、共通値にすると片方だけ常に失敗します
- **フォールバック発動をアラートにする**。「動いているからOK」で放置すると、代替経路が壊れていることに障害時に気づきます

## 実装3: プロンプトと応答を自社に残す

メモリ層を自社に持つことが、将来の乗り換えコストを最も下げます。最低限、次の項目を JSON Lines で残しておけば十分です。

```python
# llm/audit.py
import json, time, pathlib

def log_call(path: str, provider: str, model: str,
             messages: list[dict], output: str,
             latency_ms: int, meta: dict | None = None) -> None:
    rec = {
        "ts": time.time(),
        "provider": provider,
        "model": model,
        "messages": messages,
        "output": output,
        "latency_ms": latency_ms,
        "meta": meta or {},
    }
    with pathlib.Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
```

このログは3つの用途で効きます。

1. **評価セットの素材** — 実運用の入力をそのままゴールデンセットに昇格できる
2. **乗り換え判断の根拠** — 別モデルに同じ入力を投げて出力差分を比較できる
3. **監査・説明責任** — 規制対応や社内説明で「何を投げて何が返ったか」を示せる

ただし会話ログには個人情報が混ざります。保存期間・マスキング・アクセス制御はセキュリティシリーズの記事とセットで設計してください。

## 乗り換え可能性を測る — 評価ハーネス

「乗り換えられる設計」と「乗り換えられる状態」は別物です。四半期に一度、代替モデルで評価を回します。

最低限の構成はこれだけです。

- ゴールデンセット: 実運用ログから20〜50件をサンプリングし、期待出力または合否基準を人手で付ける
- 指標: 正答率、JSON パース成功率、平均レイテンシ、p95 レイテンシ、1リクエスト単価
- 判定: 「現行モデル比で正答率 -5pt 以内なら乗り換え可能」など、事前に閾値を決めておく

ポイントは**パース成功率を独立指標にすること**です。品質が同等でも、構造化出力の安定性が低いモデルは本番投入できません。

## コストとレジリエンスの両立

全リクエストを二重化するのはコスト的に無理があります。実務では次のように分けます。

| トラフィック種別 | 方針 |
|---|---|
| ユーザー対面・同期処理 | フォールバック有効、タイムアウト短め |
| バッチ・非同期処理 | 単一モデル、失敗時は翌日リトライ |
| 高難度タスク | 上位モデルにルーティング（難易度で振り分け） |
| 低難度・大量処理 | 安価なモデル、品質はサンプリング監視 |

複数モデルを裏で切り替える構成（Sakana AI の Fugu のような統合システムも登場しています）は、自前で作るかは慎重に判断してよい領域です。ただし「単一依存を避ける」という発想自体は、外注か自前かにかかわらず持つ価値があります。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| フォールバック先で JSON パースに失敗する | スキーマ検証 + 1回リトライ。`response_format` 依存をやめ、プロンプト側でも形式を明示 |
| プロバイダごとにコンテキスト長が違い、長文で片方だけ失敗する | 入力トークン上限を全プロバイダの最小値に揃える。切り詰め処理を共通化 |
| 推論系モデルだけタイムアウトする | プロバイダ別に `timeout` を設定。同期処理には使わない選択肢も検討 |
| 価格改定でコスト試算が崩れる | 単価を設定ファイル / 環境変数化し、`meta` に消費トークンを記録 |
| 障害時に初めて代替経路が壊れていると分かる | カナリアで低頻度に代替プロバイダを叩き、成功を監視する |
| 乗り換えたいが移行コストが見えない | コンテキスト層にプロバイダ固有の記法を書かない。定期的に評価ハーネスを回す |

## まとめ

- 単一モデル依存のリスクは可用性・コスト・規制・性能の4方向から来る。1つ対策しても他が残る
- 対策の本質は「依存をなくす」ではなく「呼び出し層・コンテキスト層・メモリ層を分離し、いつでも乗り換えられる状態にする」こと
- フォールバックはクリティカルパスに限定し、発動を監視する。全リクエスト二重化はコストで破綻する
- プロンプトと応答ログの自社保持が、評価セット・乗り換え判断・監査の土台になる
- 「乗り換えられる設計」と「乗り換えられる状態」は別。四半期ごとの評価ハーネスで初めて担保される

## チェックリスト

- [ ] プロバイダ固有の SDK 呼び出しが1ファイルに閉じている
- [ ] モデル名・エンドポイント・単価がコードにハードコードされていない
- [ ] クリティカルパスにフォールバックが設定されている
- [ ] フォールバック発動がログ・アラートで検知できる
- [ ] プロンプトと応答が自社ストレージに保存されている（保存期間・マスキング済み）
- [ ] 実運用ログから作った評価セットが存在する
- [ ] 代替モデルでの評価を四半期に1回実施している
- [ ] JSON パース成功率を品質指標として独立に計測している
- [ ] モデル名・料金・API 仕様は公式ドキュメントで最終確認した
