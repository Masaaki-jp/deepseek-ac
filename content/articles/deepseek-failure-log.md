---
title: "DeepSeek API の失敗を記録し、次の設計に活かす ―― エラーは「資産」になる"
description: "失敗ログを読むだけで終わらせず、正規化シグネチャ・検証フラグ・プロンプト注入の3段階で再利用可能な資産に変える設計を解説。DeepSeek API のエラー分類と、DSH プラグインに見る実装例を照合する。"
category: operations
tags:
  - DeepSeek
  - API
  - エラーハンドリング
  - 可観測性
publishedAt: 2026-10-26
draft: false
series: "DeepSeek運用"
difficulty: intermediate
minutes: 13
---

## リード

DeepSeek API の失敗は、放置すると同じ形で繰り返される。この記事では、失敗を「記録 → 正規化 → 検証 → 再利用」の4層で扱い、次の設計判断に効かせる方法を示す。読者は、リトライで隠れていた失敗原因を可視化し、検証済みの対処だけをモデルに戻す実装の骨格を持ち帰れる。

## 前提

- 想定読者: DeepSeek API を本番または検証環境で呼び出しているエンジニア
- 必要な環境: Python 3.10 以上、`openai` SDK（DeepSeek API は OpenAI 互換インターフェースで呼び出す）、SQLite
- 前提知識: HTTP ステータスコード、例外処理、プロンプトの基本構造
- 注意: エラー型・レート制限の具体的な数値やヘッダ名は変更されうる。実装前に公式ドキュメントで要確認

## 本文

### 失敗が「資産」にならない3つの理由

失敗ログは、多くの現場で次のどれかに落ちる。

1. **消える** — コンテナ再起動やログローテーションで、原因の証拠が残らない
2. **散らばる** — 同じ原因がパラメータ違いで別物として記録され、頻度が読めない
3. **読まれない** — 記録はあるが、次のリクエスト生成時に参照されない

3番目が最も深刻だ。記録しても参照されなければ、モデルは同じ誤りを同じ確率で再生する。逆に言えば、**参照される形に整形できた失敗だけが資産になる**。

### まず失敗を3種類に分ける

扱いを間違えると、リトライで恒久的な失敗を隠してしまう。

| 種類 | 例 | 取るべき行動 |
|---|---|---|
| 一時的 | 429、5xx、タイムアウト、接続断 | 指数バックオフで再試行。記録はするが設計は変えない |
| 恒久的 | 400（リクエスト不正）、401（認証）、422（スキーマ違反） | 再試行しない。即座に記録し、原因を潰す |
| 設計的 | 出力形式の違反、ツール引数の誤り、幻覚的な引数 | プロンプト・スキーマ・検証層の設計変更に回す |

一時的な失敗まで「設計ミス」として扱うと、ノイズで対策が埋もれる。逆に設計的失敗をリトライで吸収すると、再現性が失われる。

### 実装1: 失敗を構造化して残す

ポイントは、**可変部分を落としたシグネチャ**で記録すること。これでパラメータ違いの同一原因が1行に集約される。

```python
# failure_log.py
import hashlib
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)

VOLATILE = re.compile(r"\b[0-9a-f]{8,}\b|\d+")

def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def signature(error_type: str, status: int | None, model: str, message: str) -> str:
    """ID・数値・トークン数を潰して、同一原因を同じキーに寄せる"""
    normalized = VOLATILE.sub("<n>", message or "")[:200]
    raw = f"{error_type}|{status}|{model}|{normalized}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

@dataclass
class Failure:
    signature: str
    error_type: str
    status: int | None
    model: str
    message: str
    request_id: str | None
    phase: str  # "request" | "parse" | "tool"
    occurred_at: str

SCHEMA = """
CREATE TABLE IF NOT EXISTS failures (
    signature   TEXT PRIMARY KEY,
    error_type  TEXT NOT NULL,
    status      INTEGER,
    model       TEXT NOT NULL,
    message     TEXT NOT NULL,
    request_id  TEXT,
    phase       TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    trials      INTEGER NOT NULL DEFAULT 1,
    successes   INTEGER NOT NULL DEFAULT 0,
    verified_at TEXT
);
"""

def record(conn: sqlite3.Connection, f: Failure) -> None:
    conn.execute(SCHEMA)
    conn.execute(
        """
        INSERT INTO failures (signature, error_type, status, model, message,
                              request_id, phase, occurred_at)
        VALUES (:signature, :error_type, :status, :model, :message,
                :request_id, :phase, :occurred_at)
        ON CONFLICT(signature) DO UPDATE SET
            trials      = trials + 1,
            occurred_at = excluded.occurred_at
        """,
        asdict(f),
    )
    conn.commit()
```

呼び出し側はこう包む。`message` の属性名は SDK のバージョンで揺れるため `getattr` でフォールバックしておく。

```python
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

def call(conn: sqlite3.Connection, model: str, messages: list[dict]):
    try:
        return client.chat.completions.create(model=model, messages=messages)
    except APIStatusError as e:
        msg = getattr(e, "message", None) or str(e)
        record(conn, Failure(
            signature=signature(type(e).__name__, e.status_code, model, msg),
            error_type=type(e).__name__,
            status=e.status_code,
            model=model,
            message=msg,
            request_id=getattr(e, "request_id", None),
            phase="request",
            occurred_at=now(),
        ))
        raise
    except (APITimeoutError, APIConnectionError) as e:
        record(conn, Failure(
            signature=signature(type(e).__name__, None, model, str(e)),
            error_type=type(e).__name__,
            status=None,
            model=model,
            message=str(e),
            request_id=None,
            phase="request",
            occurred_at=now(),
        ))
        raise
```

**やってはいけないこと**: `messages` 全体や `Authorization` ヘッダを保存すること。プロンプトには業務データや個人情報が入る。保存するのはシグネチャと短い正規化メッセージまでにする。

### 実装2: 同一シグネチャの再発を検知して、リトライを止める

同じシグネチャが短時間に複数回出ているなら、リトライは無駄打ちになる。閾値を超えたら「次のリクエストに注意書きを足す」方向へ切り替える。

```python
def retry_hint(conn: sqlite3.Connection, sig: str,
               window_min: int = 30, threshold: int = 2) -> str | None:
    row = conn.execute(
        """
        SELECT error_type, message, trials, successes FROM failures
        WHERE signature = ?
          AND occurred_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
        """,
        (sig, f"-{window_min} minutes"),
    ).fetchone()
    if row is None:
        return None
    error_type, message, trials, successes = row
    if trials < threshold or successes > 0:
        return None
    return (
        f"直近{window_min}分で同じ失敗が{trials}回発生している: "
        f"{error_type} / {message[:120]}。"
        "同じ引数での再試行は避け、前提条件を確認してから実行すること。"
    )
```

`occurred_at` を UTC の ISO 文字列で保存しているので、比較も `strftime` で揃える。ここを `datetime('now')` と混ぜると、タイムゾーンずれで検知が効かなくなる。

### 実装3: 「検証済み」だけをプロンプトに戻す

記録した失敗をそのまま注入すると、文脈を圧迫するうえ誤った教訓まで定着する。戻すのは**成功裏に再利用された印**がついたものだけにする。

```python
def verified_lessons(conn: sqlite3.Connection, limit: int = 5) -> list[str]:
    rows = conn.execute(
        """
        SELECT message FROM failures
        WHERE successes > 0
        ORDER BY successes DESC, verified_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]

def mark_success(conn: sqlite3.Connection, sig: str) -> None:
    conn.execute(
        "UPDATE failures SET successes = successes + 1, verified_at = ? WHERE signature = ?",
        (now(), sig),
    )
    conn.commit()
```

`successes` を増やすタイミングは「教訓を注入したリクエストが成功したとき」に限る。注入しただけで加算すると、検証の意味がなくなる。

### DSH プラグインに見る、同じ問題への4つの答え

DeepSeek Harness（DSH）向けのコミュニティプラグインには、この4層をそれぞれ分担する実装がある。設計の参考として対応関係を整理する（プラグイン名・作者・追加日は検索結果由来。仕様やバージョンは各リポジトリで要確認）。

| 層 | プラグイン | 役割 |
|---|---|---|
| 記録 | `dsh-failbook`（G1en-114） | ツール呼び出し失敗を構造化エラー・非ゼロ終了コード・拒否などから拾い、同一失敗原因でクラスタリング。閾値超過時に次のリクエストへ助言を注入 |
| 構造化 | `dsh-error-atlas`（chaos-miku） | 実行エラーとタスク復盤を署名（種別＋ツール＋メッセージ）で重複排除し、件数・初末時刻・関与セッションを保持。教訓をプロンプトへ戻す |
| 検証 | `dsh-experience-library`（libiwolve） | セッションストリームから信号を集め、三层チェックを通った手順だけを「技能書」として認定 |
| 継承 | `dsh-tool-meta`（yeyupiaoling） | 失敗を SKILL ファイルとして沉淀し、`trials` / `successes` で選択。次セッションで自動継承 |

注目すべきは、どれも**「起きたこと」ではなく「効いた手順」を残す**方向に寄っている点だ。`dsh-experience-library` は自らを記憶プラグインと対比し、「記憶は何が起きたかを記録するが、これは実際に効いたプロセスを記録する」と説明している。ログ収集で満足せず、検証済み手順まで持っていくのが共通の設計思想といえる。

### 何を残し、何を残さないか

保持ポリシーは先に決めておく。決めずに運用すると、失敗ログが個人情報の溜まり場になる。

- **残す**: シグネチャ、エラー種別、ステータス、モデル名、正規化メッセージ、発生時刻、成否カウント
- **残さない**: リクエスト本文、レスポンス全文、APIキー、ユーザー識別子
- **期限**: シグネチャ単位の集計は長期、個別イベントは短期（例: 30日）で消す

## つまずきポイント

| 症状 | 対処 |
|---|---|
| ログは増えるが再利用率がゼロ | `successes` 列と `verified_at` を持ち、検証済みのみ注入する |
| 同じ失敗が毎回別レコードになる | 数値・ID・トークン数を正規化してからシグネチャ化する |
| 一時障害の記録で対策が埋もれる | `status` が 5xx / `None`（タイムアウト）の行は設計課題から除外する |
| 注入した注意書きでプロンプトが肥大する | 件数上限（例: 5件）と文字数上限（例: 各120字）を設ける |
| タイムゾーンずれで再発検知が効かない | 保存も比較も UTC ISO 文字列で統一する |
| 失敗ログに業務データが混入 | `messages` を保存対象から外し、キーは環境変数のみで扱う |

## まとめ

- 失敗は「記録しただけ」では資産にならない。参照される形に整形して初めて資産になる
- 一時的・恒久的・設計的の3分類を先に行い、リトライ対象を限定する
- 可変部分を落としたシグネチャと `successes` カウントの2つが、再利用の分岐点になる
- DSH のプラグイン群も「起きたこと」ではなく「効いた手順」を残す方向で設計されている

## チェックリスト

- [ ] 失敗を一時的 / 恒久的 / 設計的の3種類に分類した
- [ ] 数値・ID を正規化したシグネチャで記録している
- [ ] `successes` と `verified_at` を持ち、検証済みのみプロンプトに戻している
- [ ] 保存対象からリクエスト本文・レスポンス全文・APIキーを除外した
- [ ] 保持期間と件数上限を決めた
- [ ] タイムゾーンを UTC で統一した
- [ ] エラー型とレート制限の仕様を公式ドキュメントで確認した
