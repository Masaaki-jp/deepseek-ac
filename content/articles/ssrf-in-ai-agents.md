---
title: "DeepSeekエージェントのfetchツールに潜むSSRF — 国内推論基盤では防げない理由と3層防御"
description: "DeepSeekを国内推論基盤（OpenAI互換API）経由で使っても、fetchツールのSSRFは防げない。推論の所在地とツールの送信先は別レイヤ。宛先検証・出口制御・能力分離の3層で設計する方法を、tool_callの実装例とともに解説する。"
category: security
tags:
  - DeepSeek
  - SSRF
  - エージェント
  - tool_call
  - セキュリティ
publishedAt: 2026-11-21
series: "DeepSeekセキュリティ"
order: 3
difficulty: intermediate
minutes: 14
draft: false
---

[![動画を見る](https://img.youtube.com/vi/nQUsW6r_m8w/0.jpg)](https://www.youtube.com/watch?v=nQUsW6r_m8w)

## リード

DeepSeekでメール要約エージェントを組むとき、`fetch` ツールは便利だがSSRFの入口になる。
「国内DC・ZDRの推論基盤を使っているから安全」は誤りで、防御すべきはバックエンドの出口だ。
この記事では、宛先検証・出口制御・能力分離の3層で fetch を閉じ込める設計を示す。

## 前提

- 想定読者: DeepSeekで `tool_call`（function calling）を使うエージェントを実装しているエンジニア
- 必要な環境: Python 3.11+ / OpenAI互換SDK / コンテナまたはVPC内での実行
- 前提知識: OpenAI互換APIの `tools` 指定、HTTPのリダイレクト、プライベートIPレンジ

> 注記: さくらのAI Engine のモデル名・エンドポイントURL・料金・ZDRの適用条件は変更されうる。実装前に公式ドキュメントで確認してほしい。本記事のコードはエンドポイントを環境変数で受ける前提にしている。

## 本文

### まずレイヤを分ける — 推論の所在地と、ツールの送信先

SSRFの議論が混乱するのは、次の2つが同じ「ネットワーク」の話に見えるからだ。

| レイヤ | 何を保証するか | SSRFとの関係 |
|---|---|---|
| モデル層（国内推論基盤 / OpenAI互換API） | 推論データの所在地、委託先、学習利用の有無 | **無関係**。モデルがどこで動いても、ツールの送信先は変わらない |
| エージェント層（自社VPC内のバックエンド） | `tool_call` を受けて実際にHTTPを送る | **ここが本体**。到達性と権限を持つのはこの層 |
| ツール層（fetch / 固定API） | 宛先と資格情報 | 設計次第で穴にも壁にもなる |

なぜこの区別が重要か。SSRFは「攻撃者が自分の端末からは届かない場所へ、信頼されたサーバーの位置と権限を借りて到達する」攻撃だからだ。社内統合型のエージェントは、メールAPI・カレンダー・基幹システム・社内文書RAGの資格情報の近くにいる。**内部到達性が高いほど、fetchの1本の穴の影響は大きい。**

国内DCで推論しZDRで入力が保存されない構成は、データ主権と越境移転の論点を整理する。しかし `fetch` が `169.254.169.254` を叩けるかどうかは、モデルのホスティング場所では1ミリも変わらない。

### DeepSeekをOpenAI互換エンドポイントで呼ぶ

推論基盤を差し替えても、エージェント側のコードは `base_url` の変更で済むことが多い。

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ["LLM_BASE_URL"],  # 互換エンドポイント。URLは公式で確認
)

resp = client.chat.completions.create(
    model=os.environ["LLM_MODEL"],        # モデル名も公式の一覧で確認
    messages=messages,
    tools=TOOLS,
    tool_choice="auto",
)
```

**なぜ環境変数に逃がすか**: モデル名・URLは公式側の更新で変わる。コードに埋めると、切り替えのたびにデプロイが必要になる。

**注意**: OpenAI互換と言っても、並列 `tool_call` の扱い・`tool_choice` の強制・ストリーミング時の差分通知は実装ごとに差がある。「互換」を「同一」と読まないこと。挙動は公式ドキュメントと実リクエストで確認する。

### `tool_call` は「URLの決定権をモデルに渡す」設計だ

fetchツールの定義はこうなる。

```python
TOOLS = [{
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "外部URLを取得して本文テキストを返す（読み取り専用）",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
}]
```

ここで押さえるべき事実は1つ。**URLを決めるのはモデルであり、モデルへの入力には外部コンテンツ（メール本文、Webページ）が含まれる。** つまりURLの決定権は、プロンプトインジェクションを通じて攻撃者に渡りうる。

だから「モデルが選んだURLをそのまま `requests.get()` に渡す」実装は、入力の信頼性に関係なく危険だ。検証は必ずツール実装側＝コード側で行う。プロンプトで「内部IPにはアクセスしないでください」と書くのは対策ではない。

### 第1層: 宛先検証 — 文字列ではなく「解決されたIP」を見る

ドメインのallowlist照合だけでは足りない。理由は4つ。

1. **DNSリバインディング**: 検証時は正規IP、接続時は内部IPに解決させる
2. **リダイレクト**: 初回が許可ドメインでも、302先が内部IPなら到達する
3. **IP表現の揺らぎ**: `2130706433`（10進）、`0x7f000001`（16進）、IPv6のULAなど
4. **allowlistドメイン自体が内部IPに解決される**: 名前は完全一致のまま内部へ

したがって検証対象は「ホスト名」ではなく「実際に接続するIP」になる。

```python
import ipaddress
import socket
from urllib.parse import urlsplit

BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16",            # リンクローカル = クラウドメタデータを含む
    "172.16.0.0/12", "192.168.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
)]

def validate_and_pin(url: str, allowed_hosts: set[str]) -> tuple[str, str]:
    """戻り値: (接続に使うIP, Host/SNIに使う元ホスト名)"""
    p = urlsplit(url)

    if p.scheme not in ("http", "https"):
        raise ValueError("scheme not allowed")
    if p.username or p.password:          # user@host 形式を拒否
        raise ValueError("userinfo not allowed")
    if not p.hostname or p.hostname not in allowed_hosts:
        raise ValueError("host not allowed")

    port = p.port or (443 if p.scheme == "https" else 80)
    infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)

    ips = set()
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped          # ::ffff:127.0.0.1 を素通ししない
        ips.add(ip)

    if not ips:
        raise ValueError("no address resolved")

    for ip in ips:                        # 返った全IPを検証する
        if any(ip in net for net in BLOCKED_NETS):
            raise ValueError(f"blocked address: {ip}")

    # 検証済みIPにピン留めして返す（接続時に再解決させない）
    return str(sorted(ips)[0]), p.hostname
```

**なぜ「全IP」を検証するか**: 1つのホスト名が複数Aレコードを返す場合、1つだけ見て通すと残りが内部IPという構成で破られる。

**なぜIPv4-mapped IPv6を見るか**: `::ffff:192.168.0.1` はIPv6アドレスとして扱われるとレンジ判定をすり抜けることがある。正規化してから判定する。

### リダイレクトは「無効」か「毎ホップ再検証」

初回URLだけ検証して `follow_redirects=True` にするのが、最も典型的な抜け穴だ。

```python
import httpx

def safe_fetch(url: str, allowed_hosts: set[str], max_hops: int = 3) -> str:
    current = url
    with httpx.Client(follow_redirects=False, timeout=5.0) as client:
        for _ in range(max_hops + 1):
            ip, host = validate_and_pin(current, allowed_hosts)
            p = urlsplit(current)
            target = f"{p.scheme}://{ip}" + (f":{p.port}" if p.port else "")
            target += f"{p.path}" + (f"?{p.query}" if p.query else "")

            r = client.get(
                target,
                headers={"Host": host},
                extensions={"sni_hostname": host},
            )
            if r.is_redirect:
                current = str(r.next_request.url)   # 次のホップを再検証
                continue
            r.raise_for_status()
            return r.text[:200_000]                 # サイズ上限
    raise ValueError("too many redirects")
```

**なぜ `follow_redirects=False` を既定にするか**: 自動追従は、検証を1回しか通していないリクエストを内部へ運ぶ。無効化するか、ホップごとに `validate_and_pin` をやり直す。

**注意**: `extensions={"sni_hostname": host}` はSNIの指定であり、**TLS証明書のホスト名検証は接続先IPに対して行われる**。IPピン留めとホスト名検証を両立させる方法はHTTPクライアントの実装依存なので、利用ライブラリのバージョンで挙動を確認してほしい。ここを曖昧にしたままにすると「検証はしたがTLSが壊れる／検証が効かない」のどちらかになる。

**なぜGET限定・認証ヘッダなし・サイズ上限か**: fetchを読み取り専用に閉じ込めるため。POSTが通ると副作用（投稿・送信・状態変更）まで攻撃面になる。

### 第2層: 出口制御 — アプリ層が破られたときの最後の壁

アプリ層の検証はコードの変更で壊れうる。ネットワーク層は、エージェントの実行環境ごと閉じる。

- **メタデータIPの明示ブロック**: `169.254.169.254` と各クラウドのメタデータホストへのアウトバウンドを、ファイアウォール／セキュリティグループ／egressポリシーで拒否する。併せてIMDSv2を強制し、トークンなしの取得を不可能にする
- **プライベートレンジへの出口拒否**: RFC1918・ループバック・リンクローカルを、コンテナ／サブネット／プロキシのレベルで落とす
- **出口プロキシの強制**: fetchの通信を専用プロキシ経由に固定し、プロキシ側で宛先IP・ポート・ホスト名を検証する。アプリから直接ソケットを開けない構成にする
- **NetworkPolicy**: KubernetesならPodのegressを明示的に許可制にする。「デフォルト全許可」をやめるだけで防げる事故が多い

**注意**: 推論基盤への接続を閉域・専有経路にしても、それは**モデル呼び出しの経路**の話だ。fetchの出口制御とは別に設計する。ここを混同すると「閉域だから内部通信も安全」という誤認になる。

### 第3層: エージェントの能力分離 — `lethal trifecta` の脚を折る

社内統合型エージェントは「社内データ」「外部コンテンツ」「外部通信」を同時に持つ。この3つが揃うと、取得した外部コンテンツに仕込まれた指示で社内データを外へ出す経路が成立する。設計で脚を1本折る。

- **fetchに資格情報を渡さない**: 固定ツール（メール・カレンダー・基幹API）のトークンをfetchから到達不能にする。「認可APIは固定、それ以外はfetch」の振り分けだけでは、fetchが抜け道になる
- **要約・振り分けモードでは送信系ツールを無効化**: 返信・招待・POST系を落とし、下書き止まりにする。外部参加者の招待は人間承認を挟む
- **社内文書は generic fetch で読ませない**: ユーザーACLを尊重する社内検索／RAG経由に限定する。fetchで読むとサービスアカウント権限で権限バイパスが起きる
- **取得コンテンツを信頼しない**: スクリプト・コメント・非表示テキストを除去し、データ境界をマーカーで明示する
- **出口でDLPと監査ログ**: 送信リクエストに社内ID・個人情報パターンが含まれないか検査し、「どのURLをfetchし何を引用したか」を記録する

## つまずきポイント

| 症状 | 対処 |
|---|---|
| 許可ドメインなのに内部IPへ到達する | リダイレクト先を検証していない。`follow_redirects=False` にしてホップごとに再検証 |
| IPレンジ検証をすり抜ける | ホスト名で判定している。`getaddrinfo` の全IPを正規化してから判定 |
| 検証時と接続時でIPが違う | DNSリバインディング。検証済みIPにピン留めし、再解決させない |
| `::ffff:127.0.0.1` が通る | IPv4-mapped IPv6 を正規化していない |
| メタデータの資格情報が取れる | egressで `169.254.169.254` を拒否＋IMDSv2強制 |
| fetchで社内文書が読めてしまう | generic fetchを禁止し、ACL付きRAGに限定 |
| TLS検証が壊れる／効かない | IPピン留めとホスト名検証の両立方針をライブラリ仕様で確認 |
| 「国内基盤だから安全」と言われる | レイヤ違い。推論の所在地はfetchの宛先を守らない |

## まとめ

- 推論基盤の国内化・ZDRは「モデルのデータ所在地」の話であり、fetchツールのSSRFとはレイヤが違う
- 防御は3層で組む。**宛先検証（解決IPの検証とピン留め）／出口制御（egress拒否とプロキシ強制）／能力分離（資格情報を渡さない・送信系を切る）**
- 社内統合型は内部到達性が高いぶん、fetchの穴の影響が大きい。1層だけの対策に頼らない

## チェックリスト

- [ ] fetchのURL検証を、ホスト名ではなく解決後IPで行っている
- [ ] 全A/AAAAレコードを検証し、IPv4-mapped IPv6を正規化している
- [ ] 検証済みIPにピン留めし、接続時の再解決を排除している
- [ ] リダイレクトを無効化、または全ホップで再検証している
- [ ] GET限定・認証ヘッダなし・タイムアウト・サイズ上限を設定している
- [ ] メタデータIPとプライベートレンジへのegressをネットワーク層で拒否している
- [ ] IMDSv2を強制している
- [ ] fetchから固定ツールの資格情報に到達できない
- [ ] 要約モードで送信系ツールを無効化している
- [ ] 社内文書はACL尊重のRAG経由に限定している
- [ ] 取得コンテンツをサニタイズし、データ境界を明示している
- [ ] fetch先と引用内容を監査ログに残している