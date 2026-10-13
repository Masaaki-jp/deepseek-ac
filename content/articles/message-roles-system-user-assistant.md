---
title: "DeepSeek APIにおけるシステム・ユーザー・アシスタントの役割分担"
description: "system / user / assistant の3ロールは何が違うのか。Chat APIで迷いやすい「指示はどこに書くか」「履歴はどこまで残すか」をコード例つきで整理する。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - messages
publishedAt: 2026-10-13
draft: false
series: "DeepSeek APIの実装"
order: 2
difficulty: beginner
minutes: 8
---

## リード

Chat APIの `messages` は「誰が何を言ったか」を区別するだけでなく、モデルの振る舞いを制御する設計情報でもある。本記事では system / user / assistant の役割の違いと、実装で迷いやすい配置ルールを整理する。

## 前提

- 想定読者: Chat API を初めて触る、または `messages` の書き方に迷っているエンジニア
- 必要な環境: Python 3.10+ / `openai` SDK（DeepSeekはOpenAI互換エンドポイントを提供）
- 前提知識: Chat Completions API の基本形

> 注: モデル名・エンドポイント・SDKの対応状況は変更される可能性がある。最新の仕様は公式ドキュメントで要確認。

## 本文

### 3つのロールは「発話者」ではなく「制御レイヤ」

`messages` 配列の各要素は `role` を持ち、DeepSeekを含む多くのChat APIでは次の3つが基本になる。

| role | 発話者 | 主な用途 |
|---|---|---|
| `system` | 開発者 | 振る舞い・制約・ペルソナの定義 |
| `user` | エンドユーザー | 質問・入力・タスク |
| `assistant` | モデル | 過去の応答（会話履歴の再送） |

重要なのは、これが「誰が喋ったか」の記録ではなく、**モデルにどの層の指示として読ませるか**の区別だという点。同じ文面でも `system` に置くか `user` に置くかで挙動が変わる。

### system は「毎回同じ前提」を置く場所

system メッセージは会話を通じて一貫させたい指示を書く。ペルソナ、出力フォーマット、禁止事項など。

```python
from openai import OpenAI

client = OpenAI(
    api_key="YOUR_DEEPSEEK_API_KEY",
    base_url="https://api.deepseek.com",
)

messages = [
    {
        "role": "system",
        "content": (
            "あなたはシニアのPythonエンジニアです。"
            "回答は必ずコード例を1つ含め、日本語で簡潔に述べてください。"
            "推測が含まれる場合は『要確認』と明記してください。"
        ),
    },
    {"role": "user", "content": "requestsでタイムアウトを設定する方法を教えて"},
]

resp = client.chat.completions.create(
    model="deepseek-chat",
    messages=messages,
)
print(resp.choices[0].message.content)
```

**なぜ system に置くか:** user に混ぜると、会話を重ねるうちに指示が履歴の奥に埋もれ、後続の user 発話に引っ張られやすくなる。system は役割として上位に扱われるため、制約が効きやすい。

### user は「今回の入力」だけを書く

user メッセージには、そのターンでモデルに処理させたい内容だけを書く。テンプレートや制約をここに毎回貼ると、本来の入力が埋もれて指示の優先度が下がる。

```python
# 悪い例: user に制約を全部書く
{"role": "user", "content": "あなたは関西弁で話すアシスタントです。以下の文章を要約して。---- 要約対象 ----"}

# 良い例: 制約は system、入力は user
[
    {"role": "system", "content": "あなたは関西弁で話すアシスタントです。"},
    {"role": "user", "content": "以下の文章を要約して。---- 要約対象 ----"},
]
```

### assistant は「モデルの過去応答」を再送する

マルチターン会話では、モデルの返答も `messages` に積んで再送する。Chat API はステートレスなので、サーバー側が会話を覚えているわけではない。

```python
def append_turn(messages: list, user_input: str, assistant_reply: str):
    messages.append({"role": "user", "content": user_input})
    messages.append({"role": "assistant", "content": assistant_reply})
    return messages

# 会話が続くたびに role を交互に追加していく
messages = append_turn(messages, "続けて", resp.choices[0].message.content)
```

**注意点:** `assistant` ロールを自分で捏造して「こう答えたことにして」と混ぜるのは、モデルに誤った履歴を渡すことになる。few-shot として意図的に使う場合を除き、実際の応答だけを積む。

### 順序と個数のルール

- 先頭の `system` は1つにまとめる（分割すると優先度が読みにくい）
- `user` と `assistant` は会話の時系列に沿って交互に並べる
- 最後は必ず `user` で終えてからリクエストを送る（モデルに返答させるため）

```text
system → user → assistant → user → assistant → user(今回の入力)
```

## つまずきポイント

| 症状 | 対処 |
|---|---|
| system の指示が無視される | user 側に制約を混ぜていないか確認し、system に集約する |
| 会話の文脈が途切れる | assistant の応答を `messages` に積み忘れていないか確認 |
| 長い会話で指示が薄まる | 古い履歴を切り、system は毎回先頭に固定する |
| 出力フォーマットが揺れる | フォーマット指示を system に移し、JSON例など具体形で書く |

## まとめ

- `role` は発話者の区別ではなく、モデルに読ませる制御レイヤの指定
- 一貫させたい前提は `system`、今回の入力は `user`、過去応答は `assistant`
- Chat APIはステートレス。履歴は自分で積んで再送する
- 最後は `user` で終えてリクエストを投げる

## チェックリスト

- [ ] system に振る舞い・制約をまとめた
- [ ] user には今回の入力だけを書いた
- [ ] assistant に実際の過去応答だけを積んだ
- [ ] messages が user で終わっている
- [ ] 長い会話で履歴を切る方針を決めた

