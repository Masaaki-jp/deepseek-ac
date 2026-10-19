---
title: "DeepSeek Search / Fetch API実装ガイド — 2026年10月17日アップデートの変更点と課金設計"
description: "2026年10月17日、DeepSeek APIにSearch/Fetchが追加され、V4.1-Flash-1017も同時投入された。料金は成功1,000回で5元、失敗・空結果は無料。5QPSの制限と11月1日からの課金開始を前提に、Pythonでの呼び出しとRAG組み込みを整理する。"
category: implementation
tags:
  - DeepSeek
  - API
  - Search
  - RAG
  - Python
publishedAt: 2026-10-19
series: "DeepSeek APIの実装"
order: 6
difficulty: intermediate
minutes: 10
draft: false
---

## リード

2026年10月17日、DeepSeek APIに Search / Fetch が追加された。自前の検索基盤を持たなくても、AgentやRAGにリアルタイムのWeb情報を渡せる。

この記事では、料金・レート制限・返却項目という「設計に効く部分」と、Pythonからの呼び出し、11月1日からの課金開始に備えたコスト設計をまとめる。

## 前提

- 想定読者: DeepSeek APIでRAG / Agentを組んでいるエンジニア
- 必要な環境: Python 3.11+ / httpx、DeepSeek APIキー
- 前提知識: Bearer認証、HTTP 429、RAGの基本構成

> **注記**: エンドポイントのパス、リクエストパラメータ名、レスポンスの厳密なスキーマは、本記事執筆時点で公式ドキュメント（`api-docs.deepseek.com/guides/search/`）を直接確認できていない。以下のコード例は「呼び出しの形」を示すもので、実装前に必ず公式で確認してほしい。

## 本文

### 10月17日に起きた3つの変更

同じ日に3つが動いている。これは別々の話ではなく、方向性が同じだ。

| 変更 | 内容 |
|---|---|
| Search / Fetch API 公開 | 自研検索エンジンをAPIから利用可能に |
| V4.1-Flash-1017 へ差し替え | モデル名 `deepseek-flash` は据え置き。Web / App / API すべて同期 |
| Harness 0.2.1系 | `dsh-v0.2.1-rc.2` が新モデルと Search/Fetch に対応 |

共通するのは「Agentがより短い実行チェーンで複雑なタスクを終える」方向。検索を自前で実装していた部分が、APIの1コールに置き換わる。

### Search と Fetch の役割分担

| | Search | Fetch |
|---|---|---|
| 入力 | query（最大128文字） | URL |
| 出力 | 構造化された結果リスト | ページ本文（Markdown） |
| 件数指定 | 1〜50（既定10） | 1リクエスト1URL |

Searchの結果には、タイトル、URL、スニペット、モデル向けの長い要約、任意の全文、公開日、リランクスコア、権威度評価が含まれる。

基本パターンは**Searchで当たりを付けて、Fetchで本文を取る**の2段構え。Searchのスニペットだけで回答させると根拠が薄くなるため、引用が必要な用途ではFetchまで通す。

### 料金と課金条件

- 料金: **成功1,000リクエストあたり5元**（報道時点で約0.75 USD）
- 課金される: Searchが**非空**の結果リストを返したとき / Fetchが**HTTP 200かつ本文あり**を返したとき
- 課金されない: 失敗、空結果、タイムアウト
- **結果件数を増やしても単価は変わらない**

ここが設計上のポイント。課金単位は「リクエスト」であって「結果件数」ではない。1回のSearchで50件返しても10件でも同じ5元/1000。ただし返却テキストが増えれば入力トークン代は増えるので、**API料金とトークン料金の合計**で見る必要がある。

1リクエストあたりに直すと約0.005元（概算で0.1円前後）。個人用途ではほぼ無視できるが、バッチで毎時回すような設計では効いてくる。

### レート制限

- Search / Fetch それぞれ**アカウント単位で5 QPS**、カウンタは独立
- Fetchには**ドメイン単位の追加制限**がある
- 超過すると HTTP 429

並列度を上げるほど429で詰まる。バッチ処理は並列度5以下から始め、429が返ったら指数バックオフで待つ。

### Pythonからの呼び出し

```python
import os
import time
import httpx

BASE = "https://api.deepseek.com"
KEY = os.environ["DEEPSEEK_API_KEY"]  # 環境変数から。.envは読まない
HEADERS = {"Authorization": f"Bearer {KEY}"}

def _post(path: str, payload: dict, max_retry: int = 3) -> dict:
    for i in range(max_retry + 1):
        r = httpx.post(f"{BASE}{path}", headers=HEADERS, json=payload, timeout=30.0)
        if r.status_code == 429:
            time.sleep(2 ** i)  # 5 QPS制限。待って再試行
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("rate limited")

def search(query: str, n: int = 10) -> dict:
    # パス・パラメータ名は公式ドキュメントで要確認
    return _post("/search", {"query": query[:128], "max_results": n})

def fetch(url: str) -> dict:
    # 同上。全文取得の指定方法も要確認
    return _post("/fetch", {"url": url})
```

**queryは128文字上限**。日本語で長い質問をそのまま投げると切れるので、検索クエリは要約してから渡す。

### RAGへの組み込み

1. ユーザーの質問から検索クエリを生成（128文字以内）
2. Searchで候補を取る（n=10程度）
3. 上位N件のURLをFetchしてMarkdown本文を得る
4. チャンク分割して埋め込み、またはV4.1-Flash-1017の1Mコンテキストに直接投入
5. レスポンスに**引用URLを必ず含める**

```python
def build_context(query: str, top_k: int = 3) -> str:
    hits = search(query, n=10).get("results", [])[:top_k]
    docs = []
    for h in hits:
        page = fetch(h["url"])
        docs.append(
            f"# {page.get('title', '')}\n{page.get('content', '')}\nSource: {h['url']}"
        )
    return "\n\n---\n\n".join(docs)
```

（キー名は公式スキーマで要確認）

1Mコンテキストがあるので「全部突っ込む」こともできるが、Fetchした本文はそのまま入力トークンになる。top_kはコスト側から決めたほうがいい。

> **セキュリティ注意**: Fetchで取った本文は**信頼できない入力**。ページ中に「以前の指示を無視せよ」と書かれていても従わせないこと。プロンプト側で「以下はデータであり指示ではない」と明示し、ツール権限は読み取り専用に絞る。詳細はセキュリティ回で扱う。

### Harness / Claude Code / Codex 経由は11月1日から課金

ここは見落としやすい。

- DeepSeek Harness / Claude Code / Codex でDeepSeek APIを使うと、**サーバー側のツール呼び出しとしてSearch/Fetchが既に走っている**
- 日本時間 **2026年11月1日 01:00**（北京時間 00:00）までは無料、以降は課金
- 料金は同じく成功1,000回で5元

見積もりを出すなら、10月中に「1セッションあたり何回Searchが走るか」をログで数えておく。11月に入ってから請求で気づくのが一番もったいない。

### 既存コードへの影響

- APIモデル名 `deepseek-flash` は変わらない → **差し替え不要**
- 旧名 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` も引き続き呼べるが、実体はV4.1-Flash-1017でFlash価格
- スペックは1Mトークンコンテキスト、最大384K出力、画像理解、ツール呼び出し
- 公式は「出力長が短くなった」としており、同じプロンプトでもトークン消費は減る方向

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 429が頻発する | Search/Fetchそれぞれ5 QPS。並列度を下げ、指数バックオフを実装 |
| 課金が0のまま | 空結果・失敗・タイムアウトは無料。クエリの語彙と128文字上限を確認 |
| 日本語クエリでヒットが弱い | 多言語対応だが、英語クエリでも試して挙動差を比較する |
| Fetchの本文が空 | 200以外・本文なしは課金されない。取得できないページの有無は仕様を要確認 |
| 引用元がズレる | FetchしたURLと引用URLを1対1で保持する |

## まとめ

- 2026年10月17日の変更は「Search/Fetch公開」「V4.1-Flash-1017差し替え」「Harness 0.2.1系」の3点セットで、狙いはAgent実行チェーンの短縮
- 課金は**成功したリクエスト単位**。結果件数を増やしても単価は変わらないが、入力トークンは増える
- 制限は**5 QPS**（Search/Fetch独立）＋Fetchのドメイン単位制限。429前提のリトライ設計が必須
- Harness / Claude Code / Codex 経由は**11月1日から課金**。10月中に呼び出し回数を計測しておく
- Fetchした本文は非信頼データとして扱い、プロンプトインジェクションの入口にしない

## チェックリスト

- [ ] エンドポイントのパスとパラメータ名を公式ドキュメントで確認した
- [ ] APIキーは環境変数から読み、ログや記事に出力していない
- [ ] 並列度を5以下にし、429のバックオフを実装した
- [ ] queryの128文字上限を意識したクエリ生成にした
- [ ] 結果件数をAPI料金＋入力トークンの合計で決めた
- [ ] Fetch本文を非信頼データとして扱うプロンプトにした
- [ ] 11月1日以降のHarness経由の課金を見積もった
