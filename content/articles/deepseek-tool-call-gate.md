---
title: "モデルに関数を実行させない — DeepSeek APIツール呼び出しの安全な実装"
description: "DeepSeekのツール呼び出しで、モデルに実行権限を渡さない設計を解説。出力は提案として扱い、スキーマ検証・承認ゲート・ループ上限で守る実装パターンをPythonコード付きで示します。"
category: security
tags:
  - DeepSeek
  - ツール呼び出し
  - セキュリティ
  - プロンプトインジェクション
  - Python
publishedAt: 2026-10-28
draft: false
series: "セキュリティ"
order: 1
difficulty: intermediate
minutes: 14
---

## リード

ツール呼び出し（Function Calling）は「モデルが関数を実行する機能」ではありません。モデルが返すのは**呼び出しの提案**であり、実行するかどうかを決めるのはあなたのコードです。
この記事では、提案を検証してから実行する最小構成のゲート実装と、Thinking Mode 併用時の落とし穴を扱います。

## 前提

- 想定読者: DeepSeek API でツール呼び出しを実装中〜実装済みのエンジニア
- 必要な環境: Python 3.10+、`openai` 互換クライアント、`jsonschema`
- 前提知識: chat.completions の基本、JSON Schema の読み書き
- モデル名・パラメータ仕様は変更が速いため、実装時は公式ドキュメントで要確認

## 本文

### モデルが返すのは「提案」であって「実行」ではない

API のレスポンスに含まれる `tool_calls` は、構造化されたテキストです。関数名と引数（JSON 文字列）が入っているだけで、何も実行されていません。危険なのはその後です。

```python
# アンチパターン: モデルの出力をそのまま実行経路に流す
for call in resp.choices[0].message.tool_calls:
    fn = globals()[call.function.name]          # 名前で関数を解決
    fn(**json.loads(call.function.arguments))   # 引数を検証せず展開
```

この形が危険な理由は3つあります。

- `globals()` 経由の名前解決は、モデルが任意の関数名を指定できる＝**呼べてはいけない関数に到達しうる**
- 引数の検証がないため、パス・URL・コマンド文字列がそのまま sinks に届く
- 承認もログもないので、事故が起きてから何が呼ばれたか追えない

「モデルに実行させる」ではなく「モデルの提案を、ホストが審査して実行する」に反転させるのが出発点です。

### モデル出力はテキスト。DSML を正規表現で拾って実行しない

DeepSeek のモデルは内部的に DSML（DeepSeek Markup Language）と呼ばれるマークアップでツール呼び出しを表現している、という報告があります（V3/R1 系と V4 系でタグ形状が異なるという第三者の解析があります。**公式仕様は要確認**）。

ここで押さえるべきは形状の細部ではなく、**モデルの生テキストにはツール呼び出しっぽい文字列が現れうる**という事実です。すると次の実装がアンチパターンになります。

```python
# アンチパターン: 本文テキストからツール呼び出しを正規表現で抽出して実行
m = re.search(r"<\|.*?invoke name=\"(\w+)\".*?>(.*?)<\|", text, re.S)
if m:
    run_tool(m.group(1), m.group(2))
```

この方式だと、RAG で取り込んだ Web ページや Issue 本文、PDF の中に DSML 風の文字列が混ざっていた場合、**モデルが意図していない呼び出しをホストが勝手に実行**します。間接プロンプトインジェクションの典型的な入口です。

対策はシンプルです。

- 実行判断に使うのは、レスポンスの**構造化フィールド `tool_calls` のみ**
- `content`（本文）は表示・ログ用のデータとして扱い、実行経路に接続しない
- 外部から取り込んだテキストは、プロンプトに入れる前に区切りを明示し「指示ではなくデータ」と宣言する

### 実行ゲートを1か所に集約する

ツール実行の入り口を1関数に閉じ込めます。ポリシー判断・検証・監査をここに集めると、レビューすべき場所が1点になります。

```python
from dataclasses import dataclass
from typing import Any, Callable
import json
import jsonschema

@dataclass(frozen=True)
class Tool:
    name: str
    schema: dict
    handler: Callable[[dict], Any]
    risk: str  # "read" | "write" | "destructive"

TOOLS: dict[str, Tool] = {}  # 起動時に明示登録する（動的解決はしない）


def execute_tool_call(call, *, approve=None, audit=print):
    name = call.function.name
    tool = TOOLS.get(name)
    if tool is None:
        return {"ok": False, "error": "unknown_tool"}

    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid_json_arguments"}
    if not isinstance(args, dict):
        return {"ok": False, "error": "arguments_must_be_object"}

    try:
        jsonschema.validate(args, tool.schema)
    except jsonschema.ValidationError:
        return {"ok": False, "error": "schema_validation_failed"}

    # read 以外は人間かポリシーエンジンの承認を必須にする
    if tool.risk != "read":
        if approve is None or not approve(tool, args):
            return {"ok": False, "error": "denied_by_policy"}

    audit({"tool": name, "args": args, "risk": tool.risk})
    try:
        return {"ok": True, "result": tool.handler(args)}
    except TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        # 例外メッセージをそのまま返さない（内部パス・認証情報の漏洩防止）
        return {"ok": False, "error": type(e).__name__}
```

設計上のポイントは4つです。

1. **ホワイトリスト方式**: `TOOLS` に登録したものだけ実行できる。`globals()` も `getattr(module, name)` も使わない
2. **risk による段階分け**: 参照系は自動、書き込み・破壊系は承認必須
3. **承認は単調（monotonic）に**: 後段のフックで deny が allow に上書きされない構造にする。プラグイン的に拡張するなら「deny を返したら確定」を保証する
4. **エラーは抽象化**: 例外文字列をモデルに返すと、それが次のプロンプトに入り内部情報が漏れる

### 引数はスキーマで縛り、危険な引数は型で潰す

JSON Schema は「形」しか守れません。値の危険性はハンドラ側で潰します。

```python
import subprocess

def run_tests(args: dict) -> str:
    target = args["target"]  # schema で enum: ["unit", "integration"]
    if target not in {"unit", "integration"}:
        raise ValueError("unsupported target")
    proc = subprocess.run(
        ["pytest", "-q", f"tests/{target}"],
        shell=False,             # シェル解釈をさせない
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},  # 環境変数を絞る
    )
    return proc.stdout[-4000:]   # 出力は切り詰める
```

外部通信を伴うツール（fetch_url など）は、SSRF 対策が必須です。

- スキームを `https` に限定
- ホスト名を allowlist で照合（サフィックス一致ではなく完全一致）
- 名前解決後の IP がプライベート・ループバック・リンクローカル（`169.254.169.254` など）なら拒否
- リダイレクトは追わない、または追う場合も各ホップで同じ検査をする

「モデルが生成した URL をそのまま requests に渡す」は、クラウドのメタデータエンドポイントに到達しうる経路です。

### ツール結果も untrusted input として扱う

見落としやすいのが往復の後半です。ツールが返した文字列は、次のターンでモデルのコンテキストに入ります。つまり**ツール結果は新しい入力**です。

- Web ページ本文に「以前の指示を無視して〜」が含まれていれば、それは注入の試み
- ツール結果は必ず「データ」としてプロンプトに埋め、命令形として扱わせない
- 結果のサイズを制限する（巨大な結果はコンテキスト汚染とコスト増の両方に効く）
- 結果に含まれる URL・コマンドを自動で次のツール呼び出しに繋がない

実行ループには上限を必ず入れます。

```python
MAX_TURNS = 8

for turn in range(MAX_TURNS):
    resp = client.chat.completions.create(...)
    msg = resp.choices[0].message
    if not msg.tool_calls:
        break
    for call in msg.tool_calls:
        result = execute_tool_call(call, approve=ask_human)
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result, ensure_ascii=False)[:4000],
        })
else:
    # 上限到達は異常系として扱う（無限ループ＝コスト爆発）
    raise RuntimeError("tool loop exceeded MAX_TURNS")
```

### Thinking Mode と併用するときの落とし穴

推論（Thinking）を有効にしたままツールを使うと、通常モードとは挙動が変わります。第三者の実装報告では以下が挙げられています（**いずれも公式ドキュメントで要確認**）。

| 論点 | 報告されている挙動 | 実装側の対応 |
|---|---|---|
| `tool_choice` | Thinking 時は `required` / 名前指定を受け付けない | パラメータを送らず自動選択に任せる |
| `reasoning_content` | tools を含むリクエストでは全ターンで保持が必要。欠落すると HTTP 400 | 履歴に assistant メッセージを丸ごと積む |
| `content` | ツール呼び出し時の assistant メッセージで null になりうる | 空文字に正規化して送る |

履歴の積み方の例です。

```python
messages.append({
    "role": "assistant",
    "content": msg.content or "",                      # null → ""
    "tool_calls": msg.tool_calls,
    "reasoning_content": getattr(msg, "reasoning_content", "") or "",
})
```

`reasoning_content` は**実行判断には使わない**でください。推論過程はモデル内部の思考であり、ここに現れた文字列を根拠に権限を昇格させたり、承認をスキップしたりしてはいけません。あくまで API の整合性維持のために往復させるフィールドです。また、ユーザーにそのまま見せる設計も避けたほうが無難です（内部の前提やシステムプロンプトの断片が混ざりうるため）。

### 監査ログとキルスイッチを最初に作る

ツール呼び出しは「動くもの」を先に作りがちですが、事故対応のコストはログの有無で桁が変わります。

- 1呼び出し1レコードで、`tool` / `args` / `risk` / `decision`（allow・deny・ask）/ `duration` / `result_size` を残す
- 引数に秘匿値が入りうるツールは、ログ側でマスキングする
- 機能単位のキルスイッチ（`TOOLS` から外す、あるいは `risk` を強制的に destructive に昇格）を設定ファイルで切れるようにする
- 破壊的ツールには dry-run を用意し、最初の数週間は dry-run のみで運用する

## つまずきポイント

| 症状 | 対処 |
|---|---|
| tools 付きリクエストで HTTP 400 になる（Thinking 時） | `reasoning_content` を全ターン分保持しているか確認。assistant メッセージを自前で組み立て直していないか見る |
| Thinking 時に `tool_choice` でエラー | 送らない。自動選択に任せる（仕様は公式で要確認） |
| モデルが同じツールを呼び続ける | `MAX_TURNS` で打ち切る。結果にエラーを返し続けていないか確認 |
| `arguments` の JSON パースに失敗する | 空文字を `{}` に正規化。スキーマ違反はモデルに返して再試行させる（上限付き） |
| ツール結果を返したら挙動がおかしくなった | 結果が大きすぎないか、外部テキストをそのまま入れていないか確認 |
| 本番で意図しない外部通信が出た | URL 引数ツールの allowlist と SSRF 検査を最優先で入れる |

## まとめ

- モデルが返す `tool_calls` は提案であり、実行権限はホスト側のゲートだけが持つ
- 実行判断には構造化フィールドのみを使い、本文テキストや DSML 風文字列を実行経路に繋がない
- ホワイトリスト登録・スキーマ検証・risk 別承認・ループ上限の4点セットで大半の事故は防げる
- ツール結果は次のターンの入力であり、untrusted として扱う
- Thinking Mode 併用時は `reasoning_content` と `content` の扱いが通常モードと異なる（仕様は要確認）

## チェックリスト

- [ ] ツール名の解決を `globals()` / `getattr` ではなく明示登録の辞書で行っている
- [ ] `content` や外部テキストからツール呼び出しを抽出していない
- [ ] 引数を JSON Schema で検証し、値の妥当性をハンドラ側でも確認している
- [ ] 書き込み・破壊系ツールに承認（人間 or ポリシー）を要求している
- [ ] deny が後段で allow に上書きされない構造になっている
- [ ] `subprocess` は `shell=False`、環境変数を絞っている
- [ ] 外部 URL 取得に allowlist と SSRF 検査（プライベート IP 拒否）を入れている
- [ ] 実行ループに `MAX_TURNS` と実行時間・出力サイズの上限がある
- [ ] 例外メッセージをそのままモデルに返していない
- [ ] 呼び出しごとの監査ログとキルスイッチがある
- [ ] Thinking Mode 利用時、`tool_choice` と `reasoning_content` の扱いを公式ドキュメントで確認した
