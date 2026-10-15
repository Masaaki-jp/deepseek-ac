---
title: "DeepSeek APIのプロンプトをコードとして管理する"
description: "プロンプトを文字列リテラルで埋め込む運用は破綻する。Pythonでプロンプトをコードとして管理し、バージョン・テスト・差分を扱えるようにする設計を、DeepSeek APIの呼び出しと合わせて解説する。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - プロンプト管理
publishedAt: 2026-10-15
draft: false
series: "DeepSeek APIの実装"
order: 5
difficulty: intermediate
minutes: 10
---

## リード

プロンプトをソースコードに直接書くと、変更履歴もテストも効かなくなる。
この記事では、プロンプトをコードとして扱うための最小構成を、DeepSeek APIの呼び出しと組み合わせて示す。

## 前提

- 想定読者: DeepSeek APIをPythonから呼び出しているエンジニア
- 必要な環境: Python 3.10+、`openai` SDK（DeepSeekはOpenAI互換APIを提供）
- 前提知識: Chat Completions APIの基本、`messages` 配列の構造

> 注: モデル名・エンドポイント・価格は変更される可能性があるため、実装時は公式ドキュメントで最新値を確認してください。

## 本文

### なぜプロンプトを「コードとして」扱うのか

文字列リテラルの埋め込みで始めたプロンプトは、次の順で破綻します。

1. 変更理由が分からなくなる（git blame が「1行の巨大文字列」になる）
2. どのバージョンが本番か分からなくなる
3. 回帰テストが書けない
4. 再利用できない（同じ system prompt を3か所にコピー）

対策は「プロンプトを独立したモジュール・ファイルに切り出し、バージョンを持たせ、テスト可能にする」ことです。

### 最小構成: prompts パッケージ

ディレクトリを切って、プロンプトを名前付きで管理します。

```text
project/
├── prompts/
│   ├── __init__.py
│   ├── base.py       # 共通の型・ローダ
│   └── summarize.py  # 個別プロンプト
└── main.py
```

プロンプトは「関数」として定義します。変数を渡せるようにするためです。

```python
# prompts/summarize.py
from dataclasses import dataclass

VERSION = "v2"

SYSTEM = """\
あなたは技術記事を要約するアシスタントです。
- 必ず日本語で出力する
- 箇条書きは使わず、3文以内で要約する
- 原文にない情報を追加しない
"""

def build_user(text: str) -> str:
    return f"次の記事を要約してください:\n\n{text}"
```

呼び出し側はこうなります。

```python
# main.py
from openai import OpenAI
from prompts import summarize

client = OpenAI(
    api_key=...,          # 環境変数から読む。直書きしない
    base_url="https://api.deepseek.com",
)

def summarize(text: str) -> str:
    resp = client.chat.completions.create(
        model="deepseek-chat",   # モデル名は公式ドキュメントで要確認
        messages=[
            {"role": "system", "content": summarize.SYSTEM},
            {"role": "user", "content": summarize.build_user(text)},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content
```

**なぜ関数にするか**: `SYSTEM` を `str.format` で埋め込むと、ユーザー入力に `{}` が含まれたときに壊れます。`build_user` のような関数で組み立てれば、その事故を防げます。

### バージョンを明示する

プロンプトはモデルと同じく「変えたら結果が変わる資産」です。`VERSION` を持たせ、ログに残します。

```python
import logging

logging.info("prompt=%s version=%s", "summarize", summarize.VERSION)
```

**なぜログに残すか**: 出力品質が落ちたとき、「どのプロンプトの版で出たか」が追えないと原因調査ができません。A/Bテスト（シリーズ「運用」）の前提にもなります。

### テストを書く

プロンプトのテストは「壊れていないこと」を保証する回帰テストとして書きます。

```python
# tests/test_summarize_prompt.py
from prompts import summarize

def test_build_user_contains_input():
    user = summarize.build_user("テスト本文")
    assert "テスト本文" in user

def test_system_rules_present():
    # ルールが消えたら気づけるようにする
    assert "日本語" in summarize.SYSTEM
    assert "3文以内" in summarize.SYSTEM
```

**なぜ書くか**: プロンプトの「削ってはいけない一文」をテストで固定できます。レビューで消されて品質が落ちる事故を防げます。

### 外部ファイルにするか、Pythonにするか

| 形式 | 向くケース | 注意 |
|---|---|---|
| `.py` モジュール | 変数展開・条件分岐がある | ロジックが混ざると見通しが悪化 |
| `.txt` / `.md` + ローダ | 長文・条件分岐なし | バージョンはファイル名かメタで持つ |
| Jinja2 テンプレート | 複雑な条件・ループ | 依存が増える。過剰になりやすい |

**目安**: 分岐が1つ以下なら `.py`、長文の固定文なら `.txt`。Jinja2 は本当に必要になってから導入するのが無難です。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| `{}` を含む入力でエラー | `str.format` をやめ、f-string か関数で組み立てる |
| 本番とテストでプロンプトが違う | プロンプトを1か所に集約し、importで共有する |
| 変更前後で品質が比較できない | `VERSION` をログに残し、評価用データセットを用意する |
| APIキーをコミットしてしまう | 環境変数＋`.gitignore`。プロンプトと鍵は別管理 |

## まとめ

- プロンプトは関数として切り出し、変数展開の事故を防ぐ
- バージョンを持たせ、ログに残して追跡可能にする
- 削ってはいけないルールをテストで固定する
- ファイル形式は分岐の少なさで選ぶ。Jinja2は後回しでよい

## チェックリスト

- [ ] プロンプトを `prompts/` など1か所に集約した
- [ ] 変数展開を `str.format` ではなく関数/f-stringで行っている
- [ ] プロンプトに `VERSION` を持たせ、ログに出している
- [ ] ルール文を固定する回帰テストを書いた
- [ ] APIキーをコード・Gitに含めていない
