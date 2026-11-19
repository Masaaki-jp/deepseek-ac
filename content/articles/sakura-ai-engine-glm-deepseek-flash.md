---
title: "さくらのAI EngineにDeepSeek-V4.1-Flash-1017が登場——料金と使い方を整理"
description: "2026年11月18日、さくらのAI EngineがDeepSeek-V4.1-Flash-1017とGLM-5.3-Flashのパブリックプレビューを開始。月3,000リクエスト無料、1万トークンあたり入力0.4円・出力4.0円という料金体系と、OpenAI互換APIでの呼び出し方をコード付きで整理します。"
category: model
tags:
  - DeepSeek
  - V4.1
  - 料金
  - マルチモーダル
publishedAt: 2026-11-19
draft: false
difficulty: beginner
minutes: 8
---

[![動画を見る](https://img.youtube.com/vi/SJuHjnpbf3I/0.jpg)](https://www.youtube.com/watch?v=SJuHjnpbf3I)

## リード

2026年11月18日、さくらのAI EngineがDeepSeek-V4.1-Flash-1017のパブリックプレビュー提供を開始しました。本記事では、公式発表で確認できる事実（モデル名・料金・呼び出し方）と、まだ一次情報で裏取りできていない点を分けて整理します。

## 前提

- 想定読者: DeepSeek を国内ホストのAPI経由で使いたいエンジニア
- 必要な環境: Python 3.10+ または Node.js 18+、`openai` SDK、さくらのAI Engineのアカウントトークン
- 前提知識: OpenAI Chat Completions 互換APIの基本

## 何が発表されたか

さくらインターネットの公式発表で確認できる範囲は以下です。

| 項目 | 内容 |
|---|---|
| 発表日 | 2026年11月18日 |
| 追加モデル | `preview/DeepSeek-V4.1-Flash-1017`、`preview/GLM-5.3-Flash` |
| 提供形態 | 通常モデル（Chat completions） |
| 入力の性質 | 画像とテキストのマルチモーダル |
| 無償プラン | 1ヶ月あたり3,000リクエストまで |
| 課金（入力） | 10,000トークンあたり 0.4円（税込） |
| 課金（出力） | 10,000トークンあたり 4.0円（税込） |

2モデルは同一の料金体系です。単価が出力側に10倍寄っているため、**コストは「どれだけ書かせるか」でほぼ決まる**という構造になります。

## DeepSeek-V4.1-Flash-1017 とは

ここは公式発表だけでは仕様の全容が分かりません。現時点で言えること／確認が必要なことを分けます。

**言えること**

- `preview/` 接頭辞が付くため、仕様変更・提供終了の可能性があるプレビュー扱い
- 画像入力に対応する（発表文にマルチモーダルと明記）
- DeepSeek のモデルをさくらが自社ホストし、入力データをモデル提供元と共有しない構成

**要確認（公式ドキュメントで裏取りしてください）**

- コンテキスト長、MoE構成、KVキャッシュの圧縮率などのスペック
- `1017` の意味（重み更新の日付を指すという説がありますが、発表文には記載なし）
- ツール呼び出し・reasoning パラメータの対応状況
- 対応パラメータの一覧（`top_p` や `stop` などが無視されるか、エラーになるか）

推測でスペックを書くと後で記事の信頼を落とすので、ここは空欄のまま公開し、後日追記する前提が安全です。

## 料金を試算する

単価が「1万トークンあたり」なので、実装では1,000トークン単位に直すと扱いやすいです。

```python
# さくらのAI Engine DeepSeek-V4.1-Flash-1017 の単価
INPUT_YEN_PER_1K = 0.04    # 0.4円 / 10,000トークン
OUTPUT_YEN_PER_1K = 0.40   # 4.0円 / 10,000トークン

def yen(prompt_tokens: int, completion_tokens: int) -> float:
    return (
        prompt_tokens / 1000 * INPUT_YEN_PER_1K
        + completion_tokens / 1000 * OUTPUT_YEN_PER_1K
    )

# 入力2,000 / 出力500 のよくあるチャット1回
print(round(yen(2000, 500), 3))  # 0.28
```

この条件だと1リクエスト0.28円。月3,000リクエストを使い切った場合でも約840円で、検証用途なら無償枠に収まる規模です。

なお、DeepSeek 公式APIはキャッシュヒット時の割引やオフピーク割引を設定しています（**最新の料金は公式ページで要確認**）。同じ長いプレフィックスを繰り返し送るエージェント用途では、単価だけでなくキャッシュ割引の有無が効いてきます。比較表を作る場合は、必ず両者の公式料金ページを同日に確認して日付を明記してください。

## 呼び出し方

OpenAI Chat Completions 互換なので、`base_url` と `model` を差し替えるだけで動きます。

```bash
pip install openai
export SAKURA_AI_TOKEN="発行したアカウントトークン"
```

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://api.ai.sakura.ad.jp/v1",
    api_key=os.environ["SAKURA_AI_TOKEN"],  # 直書きしない
)

MODEL = "preview/DeepSeek-V4.1-Flash-1017"

res = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "FizzBuzzをPythonで書いて"}],
    max_tokens=1024,
)
print(res.choices[0].message.content)
print(res.usage)  # トークン数はレスポンスから取る
```

画像を読ませる場合は `content` を配列にします。

```python
res = client.chat.completions.create(
    model="preview/GLM-5.3-Flash",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "このグラフから読み取れる傾向を3点で"},
            {"type": "image_url", "image_url": {"url": "https://example.com/chart.png"}},
        ],
    }],
    max_tokens=1024,
)
```

TypeScript ならこうなります。

```ts
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "https://api.ai.sakura.ad.jp/v1",
  apiKey: process.env.SAKURA_AI_TOKEN,
});

const res = await client.chat.completions.create({
  model: "preview/DeepSeek-V4.1-Flash-1017",
  messages: [{ role: "user", content: "FizzBuzzをPythonで書いて" }],
  max_tokens: 1024,
});
console.log(res.choices[0].message.content);
```

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 401 / 403 が返る | `base_url` が `https://api.ai.sakura.ad.jp/v1` か、トークンが有効かを確認 |
| モデルが見つからないエラー | `preview/` 接頭辞の付け忘れを疑う。モデルIDは完全一致で指定する |
| 出力が途中で切れる | `max_tokens` を明示する。省略時のデフォルトは仕様を確認 |
| 指定したパラメータが効かない | 対応パラメータ表を確認。互換APIでは未対応パラメータが黙って無視されることがある |
| 長文で400エラー | 入力トークンと `max_tokens` の合計上限を確認（上限値は公式仕様で要確認） |
| 本番で突然動かなくなった | `preview/` は予告なく変更・終了しうる。モデル名を設定ファイル化して切替可能にする |

## まとめ

- さくらのAI Engineで `preview/DeepSeek-V4.1-Flash-1017` が使えるようになった（2026-11-18発表）
- 料金は入力0.4円・出力4.0円（各1万トークン、税込）、月3,000リクエストまで無料
- 出力単価が入力の10倍なので、コスト設計は出力側から考える
- `preview/` は評価用。本番投入はモデル名を外出しにした上で判断する
- スペックや公式APIとの価格差は一次情報での裏取りが必須

## チェックリスト

- [ ] さくらの公式発表でモデル名と料金を確認した
- [ ] アカウントトークンを環境変数で管理している（コードに直書きしていない）
- [ ] `max_tokens` を明示している
- [ ] レイテンシと出力品質を、自分のユースケースのプロンプトで測った
- [ ] DeepSeek公式APIの最新料金と比較し、比較日を記事に書いた
- [ ] `preview/` 前提の設計（モデル名の外部化・フォールバック）になっている
