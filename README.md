# deepseek.ac

Astroで構築する日本語のDeepSeek教育サイト。公開用HTML・Markdown・サイトマップを生成します。

## 開発

Node.js 22.12以降の22系、pnpm 11を使用します。

```sh
pnpm install --frozen-lockfile
pnpm dev
pnpm build
pnpm preview
```

## コンテンツ構成

公開記事は `content/articles/*.md` に配置します。

- スキーマ定義: `src/content.config.ts`
- 記事テンプレート: `templates/article_template.md`
- 一覧ページ: `/ja/articles/`
- 記事ページ: `/ja/articles/<slug>/`

### フロントマター必須フィールド

| フィールド | 型 | 内容 |
|---|---|---|
| `title` | string | 記事タイトル |
| `description` | string | 概要（120字程度） |
| `category` | enum | `implementation` / `model` / `pattern` / `operations` / `legal` |
| `publishedAt` | date | 公開日（例: `2026-10-05`） |

任意フィールド: `tags`, `updatedAt`, `draft`, `series`, `order`, `difficulty`, `minutes`

> **注意**: `draft: true` の記事はビルドから除外され、サイトに公開されません。

## 記事執筆用CLI（tools/chat.py）

DeepSeek APIを使った対話型の記事執筆支援CLIです。

### セットアップ

```sh
# APIキーを環境変数に設定（~/.config/deepseek/env などに保存）
export DEEPSEEK_API_KEY='sk-...'
```

`.env` やAPIキーは**絶対にコミットしない**でください。

### 起動

```sh
python3 tools/chat.py
```

### 起動時の自動読み込み

- `templates/article_template.md`
- `src/content.config.ts`

これにより、記事生成時に自動でテンプレートとスキーマが参照されます。

### コマンド

| コマンド | 動作 |
|---|---|
| `/ls` | 読み込み可能ファイル一覧 |
| `/read <path>` | ファイルを読み込んで参考資料に追加 |
| `/files` | 読み込み中ファイル一覧 |
| `/drop <path>` | 読み込み解除 |
| `/paste` | 複数行ペースト（`EOF` で終了） |
| `/save <path>` | 直前の返答を `content/articles/` 配下に保存 |
| `/clear` | 履歴リセット |
| `/reload` | システムプロンプト再読み込み |
| `exit` / `quit` | 終了 |

### 安全装置

- 読み書きは `~/projects/deepseek-ac` 配下のみ
- 書き込みは `content/articles/*.md` のみ
- `.git`, `node_modules`, `dist`, `.env*` などは除外
- 100KBを超えるファイルは読み込まない

### 記事執筆の流れ

```text
you> 「DeepSeek APIの環境構築」という記事を、テンプレートとスキーマに沿って書いて
deepseek> （記事ドラフトを生成）

you> /save content/articles/deepseek-api-setup.md
[save] content/articles/deepseek-api-setup.md に保存しました（XXXX 文字）
```

## 配信

Cloudflare Pagesはmain更新時に `pnpm build` を実行し、`dist/` を配信します。
秘密情報は環境変数で管理し、Gitへ保存しません。
本文表示に生成AI APIへの接続は不要です。

## セキュリティ

- APIキー、SSH鍵、`.env` は**絶対にコミットしない**
- `.gitignore` で除外済み
- Publicリポジトリのため、`draft: true` でもGitHub上では誰でも閲覧可能
- 非公開にしたい内容は、このリポジトリに置かないこと
