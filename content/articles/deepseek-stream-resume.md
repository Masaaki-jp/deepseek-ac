---
title: "中断と再開 — ストリームは「切れないもの」ではなく「切り分けて再開するもの」として設計する"
description: "DeepSeek APIのストリーミングは途中で切れる前提で設計する。チェックポイント保存、セグメント分割、冪等な再開をPython/JSで実装し、リトライで最初からやり直さない方法を解説する。"
category: implementation
tags:
  - DeepSeek
  - API
  - Python
  - JavaScript
  - ストリーミング
publishedAt: 2026-11-02
draft: false
series: "DeepSeek APIの実装"
difficulty: intermediate
minutes: 14
---

## リード

ストリーミングが途中で切れたとき、リトライで最初から生成し直していませんか。
この記事では、受信側にチェックポイントを残し、出力をセグメントに分け、失敗した分だけ再開する設計を扱います。
結果として、二重課金と二重表示を避けながら、切れた地点から処理を続けられます。

## 前提

- 想定読者: DeepSeek API でストリーミング出力を本番に組み込もうとしているエンジニア
- 必要な環境: Python 3.10+ / `httpx`、または Node.js 18+（`fetch` がグローバルにある環境）
- 前提知識: Server-Sent Events（SSE）の基本的な読み方、HTTP のリトライと冪等性

> 注記: エンドポイント、モデル名、エラーコードの意味、assistant メッセージの末尾配置（プレフィル）の可否は変わることがあります。実装前に公式ドキュメントで確認してください。

## 本文

### ストリームは切れる

DeepSeek API のストリーミングは SSE で届きます。`stream: true` を指定すると `data: {...}` 行が逐次送られ、最後に `data: [DONE]` で終わります。この形式は途中で切れる可能性を常に持ちます。

- モバイル回線や Wi-Fi の切断
- ロードバランサやプロキシのアイドルタイムアウト（無音时间が一定を超えると切る）
- クライアント側の読み取りタイムアウト
- 429（レート制限）や 5xx による途中終了
- ユーザーによる明示的なキャンセル

重要なのは、これらが例外ではなく通常起こる事象だという前提です。「切れないもの」として設計すると、切れた瞬間に設計が破綻します。

### 「切れない」前提が生む4つの損失

1. **コスト**: リトライで同じプロンプトを最初から投げ直すと、すでに生成済みのトークン分も再度課金されます。
2. **時間**: 最初のトークンが返るまでの待ち時間（TTFT）を毎回やり直します。長い出力ほど損失が大きくなります。
3. **ユーザー体験**: 途中まで表示した文章が消える、あるいは最初から二重に表示される。
4. **観測性**: どこまで受信できたかが残らず、「なぜ切れたか」を後から追えません。

### 設計方針：長い1本を、短い複数のセグメントに分ける

発想を変えます。巨大な1本のストリームを「切れないように守る」のではなく、最初から**短いセグメントの連結**として設計します。

- 1セグメント = 見出し1つ、関数1つ、章1つなど、意味のある単位
- 各セグメントは独立して再生成できる
- 完了したセグメントは状態を `done` として保存し、再実行時はスキップする
- 失敗したセグメントだけを再試行する

これは情報システムの運用設計で広く共有されている原則と同じです。異常を検知したら処理を止め、関連データの整合性を確認してから再開する。中途半端な出力をそのまま次の工程に流すと、前後でデータの不整合が起きます。ストリームでも「止める → 記録する → 確認する → 再開する」の4ステップを踏みます。

### 実装1：受信しながらチェックポイントを残す（Python）

まず、受信したチャンクを1行1JSONで追記します。プロセスが落ちても、そこまでが残ります。

```python
import json
import httpx

API_URL = "https://api.deepseek.com/chat/completions"

def stream_to_jsonl(messages, ckpt_path, api_key, model="deepseek-chat"):
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    parts = []

    with ckpt_path.open("a", encoding="utf-8") as f, httpx.stream(
        "POST",
        API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": messages, "stream": True},
        # read は「チャンク間の無音」に対するタイムアウト
        timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            delta = json.loads(data)["choices"][0]["delta"].get("content")
            if not delta:
                continue
            parts.append(delta)
            f.write(json.dumps({"delta": delta}, ensure_ascii=False) + "\n")
            f.flush()  # 落ちても直前まで残す

    return "".join(parts)
```

ポイントは `read` タイムアウトを「全体の所要時間」ではなく「チャンク間の無音時間」として捉えることです。プロキシのアイドルタイムアウトより短く設定しておくと、切られる前に自分で検知できます。

読み戻すときは、末尾の壊れた行を捨てます。

```python
import json

def load_partial(ckpt_path) -> str:
    if not ckpt_path.exists():
        return ""
    parts = []
    for line in ckpt_path.read_text(encoding="utf-8").splitlines():
        try:
            parts.append(json.loads(line)["delta"])
        except (json.JSONDecodeError, KeyError):
            continue  # 書き込み途中で切れた行は無視
    return "".join(parts)
```

### 実装2：続きだけを生成させる

再開時は、元の指示と途中までの出力を渡し、「続きだけ」を要求します。

```python
CONTINUE_INSTRUCTION = (
    "上はあなたが途中まで書いた出力です。"
    "繰り返しや前置きをせず、続きだけを出力してください。"
    "文の途中で切れている場合は、その文を完結させてから続けてください。"
)

def resume_messages(system: str, user: str, partial: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
        {"role": "assistant", "content": partial},
        {"role": "user", "content": CONTINUE_INSTRUCTION},
    ]
```

> 注記: assistant メッセージを会話の末尾に置いて続きを生成させる方式（プレフィル）が DeepSeek API で公式にサポートされているかは、公式ドキュメントで確認してください。使えない場合は、途中出力を user メッセージ内に「以下は途中までの出力です」と埋め込む形に置き換えます。

長い出力では、途中までの全文ではなく末尾 2,000 文字程度だけを渡すとトークン消費を抑えられます。ただし文脈の一貫性が落ちるため、セグメントの先頭で要約を渡す設計と組み合わせます。

### 実装3：セグメント単位の冪等ジョブ

セグメントにIDを振り、`done` はスキップします。状態保存は一時ファイル経由で原子的に置き換えます。

```python
import hashlib
import json

def segment_id(task: str, index: int, prompt: str) -> str:
    return hashlib.sha256(f"{task}:{index}:{prompt}".encode()).hexdigest()[:16]

def save_state(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 書き込み途中の破損を避ける

def run_segments(segments, state_path, generate):
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    for seg in segments:
        prev = state.get(seg["id"])
        if prev and prev["status"] == "done":
            seg["output"] = prev["output"]
            continue

        seg["status"] = "running"
        state[seg["id"]] = seg
        save_state(state_path, state)

        try:
            seg["output"] = generate(seg["prompt"])
            seg["status"] = "done"
        except Exception:
            seg["status"] = "failed"
            state[seg["id"]] = seg
            save_state(state_path, state)
            raise

        state[seg["id"]] = seg
        save_state(state_path, state)

    return segments
```

`done` をスキップするので、同じジョブを何度実行しても課金は増えません。独立したセグメント同士は並列化できますが、前のセグメントの出力を入力に使う場合は直列にしてください。

### 実装4：クライアント側の中断と再開（JavaScript）

ブラウザや Node.js では `AbortController` で無音タイムアウトを実装できます。

```js
async function streamOnce(messages, onDelta, signal) {
  const res = await fetch("https://api.deepseek.com/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${process.env.DEEPSEEK_API_KEY}`,
    },
    body: JSON.stringify({ model: "deepseek-chat", messages, stream: true }),
    signal,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let full = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line.startsWith("data:")) continue;

      const data = line.slice(5).trim();
      if (data === "[DONE]") return full;

      const delta = JSON.parse(data).choices?.[0]?.delta?.content;
      if (delta) {
        full += delta;
        onDelta(delta, full);
      }
    }
  }
  return full;
}
```

チャンクを受けるたびにタイマーを張り直します。

```js
const ac = new AbortController();
const IDLE_MS = 30_000;
let idle = setTimeout(() => ac.abort(new Error("idle timeout")), IDLE_MS);

try {
  const full = await streamOnce(messages, (delta) => {
    clearTimeout(idle);
    idle = setTimeout(() => ac.abort(new Error("idle timeout")), IDLE_MS);
    render(delta);
  }, ac.signal);
} finally {
  clearTimeout(idle);
}
```

中断後は、保存した部分出力があればそれを使い、なければ元のメッセージで最初からやり直します。

```js
const partial = await loadPartial(jobId);
const nextMessages = partial
  ? [...baseMessages, { role: "assistant", content: partial }, { role: "user", content: CONTINUE_INSTRUCTION }]
  : baseMessages;
```

### 失敗を分類してリトライ方針を決める

すべてを同じようにリトライすると、直らないエラーを繰り返して課金だけが増えます。

| 種類 | 例 | 方針 |
|---|---|---|
| ネットワーク断・無音タイムアウト | 接続リセット、読み取りタイムアウト | チェックポイントから再開 |
| 429 | レート制限 | 待機して再開（指数バックオフ） |
| 5xx | サーバ側の一時障害 | バックオフして再開 |
| 4xx（400/401/402/422 など） | リクエスト不正、認証、残高不足 | リトライしない。原因を修正する |

エラーコードの具体的な意味は API 仕様で変わるため、公式ドキュメントの一覧を確認して分類表を自前で持っておくと安全です。

## つまずきポイント

| 症状 | 対処 |
|---|---|
| リトライのたびに最初から生成され、コストが二重になる | 部分出力を JSONL で保存し、「続きだけ」を生成するプロンプトに切り替える |
| 再開時に同じ文が繰り返される | 「繰り返すな」の指示に加え、直前の数百文字をプロンプトに含めて開始位置を重ねる |
| チェックポイントが壊れて再開できない | 1行1JSONで追記し、読み込み時に壊れた行を捨てる。状態ファイルは一時ファイル経由で置換する |
| 60秒ほどで必ず切れる | プロキシやLBのアイドルタイムアウトを疑い、readタイムアウトをそれより短くする。セグメントも短くする |
| 二重実行で同じセグメントが2回課金される | セグメントIDで `done` をスキップし、状態を原子的に保存する |
| 再開した出力の文体が変わる | セグメントの先頭に、直前までの要約と文体の指示を毎回渡す |

## まとめ

- ストリームは切れる前提で設計する。切れないように守るより、切れた地点から再開できる形にする
- 受信側にチェックポイントを残す。1行1JSONの追記と、壊れた行の切り捨てが最小構成
- 長い1本ではなく短いセグメントに分け、`done` をスキップする冪等なジョブにする
- 再開時は「続きだけ」を指示し、繰り返しと前置きを抑える
- 失敗を分類し、再試行可能なものだけバックオフしてリトライする

## チェックリスト

- [ ] 受信したチャンクを逐次保存している（プロセスが落ちても残る）
- [ ] 保存形式は追記可能で、末尾の破損行を読み飛ばせる
- [ ] 出力を意味のある単位のセグメントに分割している
- [ ] 完了済みセグメントをスキップする仕組みがある
- [ ] 状態ファイルを原子的に置き換えている
- [ ] 再開用プロンプトで「繰り返さない」「続きだけ」を指示している
- [ ] assistant 末尾配置（プレフィル）の可否を公式ドキュメントで確認した
- [ ] 無音タイムアウトをプロキシのアイドルタイムアウトより短く設定している
- [ ] リトライ可否をエラー種別ごとに分類している
