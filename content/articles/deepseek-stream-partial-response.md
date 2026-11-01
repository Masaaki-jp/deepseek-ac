---
title: "DeepSeek APIで部分的な応答を扱う — 打ち切り・切断・再開の実装"
description: "ストリーミングが途中で切れたとき、受信済みテキストを捨てずに「不完全」として扱い、finish_reason判定・継続生成・安全なリトライで復帰する実装をまとめます。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - ストリーミング
publishedAt: 2026-11-01
draft: false
series: "DeepSeek APIの実装"
difficulty: intermediate
minutes: 10
---

## リード

LLM の応答は「全部返るか、何も返らないか」ではありません。トークン上限・ネットワーク断・クライアント側のタイムアウトで、**途中まで返った状態**が日常的に発生します。
この記事では、その部分応答を捨てずに保持し、原因を判別して復帰させる実装方針を示します。

## 前提

- 想定読者: DeepSeek API をアプリに組み込み始めたエンジニア
- 必要な環境: Python 3.10+ / `openai` SDK（DeepSeek は OpenAI 互換エンドポイント）
- 前提知識: SSE（Server-Sent Events）によるストリーミングの基礎

> 注: DeepSeek API のパラメータ名・`finish_reason` の値・課金単位は変更され得ます。本記事のコードは OpenAI 互換の範囲で書き、仕様依存の箇所は公式ドキュメントで要確認としてください。

## 本文

### 部分応答が起きる3つの場面

| 場面 | 起きること | 判別方法 |
|---|---|---|
| 出力トークン上限 | 文が途中で終わる。APIは正常終了扱い | `finish_reason == "length"` |
| ネットワーク断 / タイムアウト | ストリームが無言で止まる | 例外、または終端チャンク未受信 |
| クライアント都合の中断 | ユーザーが「停止」を押す | アプリ側のフラグ |

重要なのは、**1つ目は「エラーではない」**という点です。HTTP 200 で返ってくるので、例外処理だけでは検知できません。`finish_reason` を必ず見ます。

### 受信済みを捨てない: 部分応答を状態として持つ

まず「途中まで受信したテキスト」と「なぜ終わったか」を1つの値にまとめます。UI もログもこの値だけ見れば済むようにするのが目的です。

```python
from dataclasses import dataclass

@dataclass
class PartialResult:
    text: str = ""
    finish_reason: str | None = None
    error: str | None = None

    @property
    def is_incomplete(self) -> bool:
        # stop 以外は「途中で終わった」とみなす（値は公式ドキュメントで要確認）
        return self.finish_reason != "stop" or self.error is not None
```

### ストリーミングを「落ちても壊れない」形で受ける

`for chunk in stream` は例外で抜けると、それまでの `delta` がスコープから消えがちです。受信ループの外に状態を置き、`finally` で必ず閉じます。

```python
from openai import OpenAI

client = OpenAI(api_key="...", base_url="https://api.deepseek.com")

def stream_chat(messages, result: PartialResult, max_tokens: int = 2048):
    stream = client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        max_tokens=max_tokens,
        stream=True,
    )
    try:
        for chunk in stream:
            if not chunk.choices:
                continue  # usage のみを含む終端チャンク
            choice = chunk.choices[0]
            delta = choice.delta.content or ""
            if delta:
                result.text += delta
                yield delta
            if choice.finish_reason:
                result.finish_reason = choice.finish_reason
    except Exception as e:
        result.error = repr(e)      # 受信済み text は保持される
    finally:
        stream.close()
```

呼び出し側は、例外を握りつぶさずに `result` を保存します。**部分テキストをDBに書くタイミングを「完了時」にしない**のが要点です。数十トークンごとに upsert しておけば、プロセスが落ちても直前まで残ります。

### 終端を確認する: `[DONE]` と `finish_reason`

SSE では最終的に `data: [DONE]` が流れ、その直前のチャンクに `finish_reason` が入ります。生の HTTP を叩く場合は、この2つを両方確認してください。

```python
# 生 SSE を読む場合の最小チェック
done = False
for line in response.iter_lines():
    if not line or not line.startswith(b"data: "):
        continue
    payload = line[len(b"data: "):]
    if payload == b"[DONE]":
        done = True
        break
    ...
if not done:
    result.error = "stream_closed_without_done"
```

`finish_reason` が `None` のまま終わっているのに `[DONE]` が来ていないなら、それは切断です。逆に `[DONE]` が来ていれば、少なくとも API 側は正常終了しています。

### 続きを生成させる（継続プロンプト）

`finish_reason == "length"` のとき、同じ会話に部分テキストを assistant として積んで続きを要求できます。

```python
CONTINUE_PROMPT = "直前の応答の続きだけを出力してください。前置き・繰り返し・謝罪は不要です。"

def continue_messages(messages, partial_text: str):
    return messages + [
        {"role": "assistant", "content": partial_text},
        {"role": "user", "content": CONTINUE_PROMPT},
    ]
```

ただし、モデルは**直前の数文を繰り返すことがあります**。実務では部分テキストの末尾 50〜100 文字を削ってから渡し、後で連結時に重複を検出して繋ぎ直すのが安全です。

```python
def join_without_overlap(head: str, tail: str, window: int = 80) -> str:
    probe = head[-window:]
    for size in range(len(probe), 8, -1):
        if tail.startswith(probe[-size:]):
            return head + tail[size:]
    return head + tail
```

長文を分割生成するなら、最初から「見出しごとに生成する」「章番号を指定して続けさせる」など、**区切りを構造化する**ほうが破綻しにくくなります。

### リトライは「やり直し」と「継続」を分ける

同じリトライでも意味が違います。ここを混ぜると二重出力や課金の無駄が出ます。

| 原因 | 取るべき行動 |
|---|---|
| ネットワーク断（`error` あり） | 指数バックオフで**最初から**再実行、または継続 |
| `length` 打ち切り | リトライではなく**継続生成** or `max_tokens` 増加 |
| 429 / 5xx | 待って再実行。冪等キーで保存側の重複を排除 |

```python
import random, time

def with_backoff(fn, attempts=4, base=1.0):
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(base * (2 ** i) + random.random())
```

保存側にはリクエストID（自分で採番した UUID）を必ず持たせ、再実行時に同じ行を上書きする形にしてください。これがないと、リトライのたびに応答が1件ずつ増えます。

### 不完全な応答をUIで「不完全」と伝える

生成が途中で止まったのに、完成した回答のように表示するのが一番まずいパターンです。UX の原則はシンプルで、

1. 届いたテキストは消さずに残す
2. 「途中で終了しました」と明示する
3. 原因に合った操作を1つだけ出す（再試行 / 続きを生成 / 上限の案内）

「再試行」を出すなら、**最初からやり直すのか続きからなのか**を文言で区別します。ネットワーク断なら再試行、`length` なら「続きを生成」が適切です。上限エラーで再試行ループに入れるのは避けてください。

### JSON・ツール呼び出しの途中切れ

構造化出力は、途中で切れると**パース不能な文字列**になります。ここで例外を握りつぶして `{}` を返すと、バグの原因が見えなくなります。

```python
import json

def parse_or_partial(raw: str):
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, {"incomplete_json": True, "raw": raw, "error": str(e)}
```

ツール呼び出しも同様に、`arguments` が途中で切れた状態で実行しないこと。`finish_reason` が `tool_calls` 以外なら実行をスキップし、不完全フラグ付きで返します。事前に `max_tokens` を出力スキーマの想定サイズより大きめに取っておくのが予防策です。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 応答が文の途中で終わる。例外は出ない | `finish_reason == "length"` を確認。`max_tokens` 増加か継続生成 |
| ストリームが無言で止まる | `[DONE]` 未受信を検出。無通信タイムアウトを自前で持つ |
| 継続生成で同じ段落が繰り返される | 部分テキスト末尾を削って渡す／重複除去して連結 |
| リトライで応答が二重に保存される | 自前のリクエストIDで upsert（冪等化） |
| JSON パースエラーで全体が落ちる | パース失敗を「不完全」として返し、生文字列を保持 |
| 停止ボタンを押すとテキストが消える | 受信ループの外にバッファを持ち、`finally` で保存 |

## まとめ

- 部分応答は例外ではなく**状態**。`text` / `finish_reason` / `error` を1つの値で持つ
- `length` は正常終了なので、例外処理だけでは検知できない
- 継続生成は有効だが重複が起きる前提で、末尾を削る・連結時に除去する
- リトライは「やり直し」と「継続」を分け、保存側は冪等にする
- UI では不完全であることを明示し、原因に合った操作を1つだけ出す

## チェックリスト

- [ ] `finish_reason` をログに残している
- [ ] ストリーム受信中のテキストを途中で永続化している
- [ ] `[DONE]` 未受信＝切断として検出している
- [ ] リトライに指数バックオフと上限を設定している
- [ ] リクエストIDで保存を冪等化している
- [ ] 不完全応答をUIで「不完全」と表示している
- [ ] JSON / ツール呼び出しの途中切れを実行しないガードがある
