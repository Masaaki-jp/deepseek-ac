---
title: "DeepSeekエージェントの権限と承認をどう設計するか — RBAC/ABACと承認ゲートの実装"
description: "権限（誰が何をしてよいか）と承認（人間がどこで止めるか）を分けて設計する方法を解説。ロール設計、ABACの条件、承認ゲートをツール実行層に置く実装、監査ログまでをPythonコード付きで示します。"
category: pattern
tags:
  - DeepSeek
  - エージェント
  - セキュリティ
  - RBAC
  - ツール呼び出し
publishedAt: 2026-10-30
draft: false
series: "実装パターン"
order: 4
difficulty: intermediate
minutes: 12
---

## リード

エージェントに「送る・書く・払う」を任せる前に決めるべきは、モデルの賢さではなく権限と承認の境界です。
この記事では、権限を静的な許可、承認を動的な停止として分離し、承認ゲートをツール実行層に置く設計を、Python のコードで示します。

## 前提

- 想定読者: DeepSeek API でツール呼び出し（function calling）を使うエージェントを組むエンジニア
- 必要な環境: Python 3.11 以上、OpenAI 互換 SDK
- 前提知識: tool use の基本フロー、RBAC / ABAC という用語の雰囲気

> DeepSeek API の `base_url`・モデル名・パラメータ仕様は変更されることがあります。本記事のコードは OpenAI 互換 SDK を前提にした骨格なので、実際の値は公式ドキュメントで要確認してください。

## 本文

### 権限と承認は別物として設計する

混同すると設計が破綻します。

| | 答える問い | 実装場所 | 性質 |
|---|---|---|---|
| 権限 | この主体はこの操作をしてよいか | ポリシー判定関数 | 静的・事前に決まる |
| 承認 | 今この操作を実行してよいか | 実行直前のゲート | 動的・人と状況に依存 |

権限だけでは足りません。権限は「できる」を表すだけで、「今やってよいか」は表さないからです。
逆に承認だけでも足りません。全操作に人間の承認を挟むと承認疲れが起き、担当者が中身を読まずに押すようになります。これは承認がないより危険です。

### 権限モデル: RBAC を土台に、条件は ABAC に逃がす

まずロールで大枠を作り、細かい条件は属性で表現します。ロールに条件を詰め込むとロール数が爆発するためです。

- ロールは「職務」で切る（閲覧・編集・承認・管理）
- ロールの入れ子（継承）は便利だが複雑化する。実務では 2 階層までに抑えると説明可能なまま保てる
- 金額・送信先・データ分類・時間帯などの条件は ABAC 側で扱う
- 職務分掌（SoD）: 作成者と承認者は必ず分ける。エージェントに自己承認させない
- エージェントは人間のロールを流用せず、`agent:*` として別主体にする

判定は 3 値にすると見通しが良くなります。`allow` / `deny` / `require_approval` の 3 つです。

```python
from dataclasses import dataclass
from enum import Enum

class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"

@dataclass(frozen=True)
class Request:
    subject_role: str        # "agent:writer" など
    action: str              # "send_email" / "read_doc" など
    amount: int = 0          # 金額などのスカラ属性
    external: bool = False   # 外部への送信か
    irreversible: bool = False

# 上から評価し、最初に一致したルールを採用する
RULES = [
    # エージェントに権限管理そのものを触らせない
    (lambda r: r.subject_role.startswith("agent:")
               and r.action in {"grant_role", "delete_user"}, Decision.DENY),
    # 読み取りは承認不要
    (lambda r: r.action.startswith("read_"), Decision.ALLOW),
    # 外部送信・不可逆・高額は人間の承認を要求
    (lambda r: r.external or r.irreversible or r.amount >= 50_000,
     Decision.REQUIRE_APPROVAL),
]

def authorize(req: Request) -> Decision:
    for cond, decision in RULES:
        if cond(req):
            return decision
    return Decision.DENY  # 既定は拒否（allowlist 方式）
```

既定を `deny` にするのが要点です。新しいツールを追加したときに、ルールを書き忘れても「勝手に実行される」ことはなくなります。

### 承認ゲートはツール実行層に置く

最も多い失敗は、プロンプトに「危険な操作はしないでください」と書いて安心するパターンです。プロンプトは確率的な指示であり、保証ではありません。ゲートは必ずコード側、それも**ツールを実際に実行する直前**に置きます。

操作カテゴリごとの既定は、次のように割り切ると運用が回ります。

| 操作の性質 | 既定 | 例 |
|---|---|---|
| 読み取り・冪等 | 承認不要 | 検索、ドキュメント取得 |
| 外部への送信 | 承認必須 | メール送信、外部 API への POST |
| 不可逆 | 承認必須 | 削除、公開、返金 |
| 高額・契約 | 多段階承認 | 金額閾値超え、対外文書の発行 |
| 権限変更 | 拒否（人間のみ） | ロール付与、ユーザー削除 |

承認フローの形は 3 つを使い分けます。

- 1 段階: 投稿者が編集者に回す。件数が多い定常作業向け
- 多段階: 部署承認 → 広報承認のように、観点の異なる承認を直列に置く
- 並列（n 人中 m 人）: 承認者 1 人の不在で止めたくない場合

### DeepSeek 側の実装: 承認待ちで会話を止める

ツール呼び出しを受けたら、実行前に `authorize()` を通します。承認が必要なら実行せず、状態を永続化して停止します。

```python
import json, time, uuid
from openai import OpenAI

client = OpenAI(
    api_key="...",                       # 環境変数から読む。直書きしない
    base_url="https://api.deepseek.com", # 公式ドキュメントで要確認
)

def to_request(tool_call) -> Request:
    args = json.loads(tool_call.function.arguments)
    return Request(
        subject_role="agent:writer",
        action=tool_call.function.name,
        amount=args.get("amount", 0),
        external=args.get("external", False),
        irreversible=args.get("irreversible", False),
    )

def tool_msg(call_id: str, payload: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False),
    }

def run_tools(tool_calls, messages, store) -> str:
    for tc in tool_calls:
        req = to_request(tc)
        decision = authorize(req)

        if decision is Decision.DENY:
            messages.append(tool_msg(tc.id, {"error": "denied by policy"}))
            continue

        if decision is Decision.REQUIRE_APPROVAL:
            record = {
                "approval_id": str(uuid.uuid4()),
                "request": req.__dict__,
                "tool_call": tc.model_dump(),
                "status": "pending",
                "expires_at": time.time() + 3600,
            }
            store.save(record)  # DB / キューに永続化
            messages.append(tool_msg(tc.id, {
                "status": "pending_approval",
                "approval_id": record["approval_id"],
            }))
            return "paused"

        messages.append(tool_msg(tc.id, execute(tc)))

    return "continue"
```

承認後に再開するときは、保存した `tool_call_id` に対して**同じ ID で**ツール結果を返し、そのまま会話を続けます。ここで ID を変えるとモデル側の対応関係が壊れます。

```python
def resume(record, messages, approved: bool):
    tc_id = record["tool_call"]["id"]
    if not approved:
        messages.append(tool_msg(tc_id, {"status": "rejected"}))
    else:
        messages.append(tool_msg(tc_id, execute(record["tool_call"])))
    return client.chat.completions.create(
        model="deepseek-chat",  # モデル名は公式ドキュメントで要確認
        messages=messages,
        tools=TOOLS,
    )
```

### 承認画面に「何が起きるか」を出す

承認設計で見落とされやすいのが承認者の情報不足です。承認者が権限の全体像を把握できないまま押すと、誤承認が起きます。実務では次の 3 点を承認リクエストに必ず含めます。

- 実行される操作の要約（宛先・金額・対象ファイル名まで）
- 差分（送信本文、変更前後の値）
- 誰の権限で、どのポリシーに該当して承認が必要になったか

さらに、承認者自身のなりすましを防ぐため、承認操作は必ず人間の認証済みセッションから行い、エージェント経由の API では承認できないようにします。

### 記録と失効

- 承認ログには「誰が・いつ・何を・なぜ承認したか」を残す
- プロンプト全文や個人情報はそのまま保存しない（マスクまたは要約）
- 承認待ちには TTL を設け、期限切れは自動却下にする
- 承認者の代理・エスカレーションを決めておく。1 人承認だと休暇で止まる

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 承認待ちのまま会話が進まない | ツール結果に `pending_approval` を返し、状態を永続化して停止する |
| 同じ操作が二重実行される | `approval_id` を冪等キーとして実行側で重複排除する |
| 権限判定があちこちに散る | `authorize()` 1 箇所に集約し、ツール追加時はここだけ更新する |
| ロールが増えすぎて説明できない | ロールは職務のみ。条件は ABAC 属性に逃がす |
| エージェントが自分で承認してしまう | 承認 API をエージェントの資格情報から呼べない構成にする |
| 承認者が中身を見ずに押す | 承認画面に差分・宛先・金額・該当ポリシーを表示する |

## まとめ

- 権限は「してよいか」、承認は「今してよいか」。分けて設計する
- 判定は `allow` / `deny` / `require_approval` の 3 値にし、既定は拒否にする
- 承認ゲートはプロンプトではなくツール実行層に置く
- エージェントは人間のロールを継承させず、別主体として扱う
- 承認ログと TTL、代理承認まで含めて初めて運用に耐える

## チェックリスト

- [ ] ツール一覧を「読み取り / 外部送信 / 不可逆 / 高額 / 権限変更」に分類した
- [ ] ポリシー判定を 1 つの関数に集約し、既定を deny にした
- [ ] 承認が必要な操作で、実行前に処理を止め状態を永続化している
- [ ] 承認後に同じ `tool_call_id` で結果を返している
- [ ] 承認ログに実行者・時刻・対象・理由を残している
- [ ] 承認待ちの TTL と代理承認者を決めた
- [ ] API キーをコードに直書きせず環境変数から読んでいる
