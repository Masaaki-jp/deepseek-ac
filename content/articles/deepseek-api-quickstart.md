---
title: DeepSeek API アカウント取得から最初のリクエストまで
description: DeepSeek API を初めて使うエンジニア向けに、アカウント取得からPythonでの最初のAPI呼び出しまでを最短でまとめます。
category: implementation
tags:
  - 入門
  - Python
  - API
publishedAt: 2026-10-02
draft: false
difficulty: beginner
minutes: 10
series: api-quickstart
order: 1
---

## この記事の目的

DeepSeek API を初めて使う方が、最短で「動くコード」にたどり着くまでの手順をまとめます。

## 前提

- Python 3.10以上
- ターミナル操作の基本
- クレジットカード（API利用料の支払い用）

## 1. アカウント取得

公式サイトから登録します。メールアドレスとパスワードで登録し、確認メールのリンクをクリックします。

## 2. APIキーの発行

管理画面の「API Keys」から新しいキーを発行します。APIキーは発行直後の1回しか表示されません。必ず安全な場所に保存してください。

## 3. 環境変数の設定

APIキーをソースコードに直接書くのは避けます。環境変数で管理します。

DEEPSEEK_API_KEY=sk-...

.env は必ず .gitignore に追加してください。Git にコミットされると、APIキーが漏洩します。

## 4. Pythonでの最初のリクエスト

OpenAI SDK と互換性があるため、base_url を変更するだけで使えます。詳細なコードは今後加筆します。

## 5. 次のステップ

- レスポンスのJSON構造を理解する
- エラーハンドリングを実装する
- ストリーミング応答を使う

---

この記事は最低限の動作確認用です。今後、詳細な解説に更新します。
