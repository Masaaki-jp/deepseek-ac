# deepseek.ac

Astroで構築する日本語のDeepSeek教育サイト。公開用HTML・Markdown・サイトマップを生成します。

## 開発

Node.js 22.12以降の22系、pnpm 11を使用します。

```sh
pnpm install --frozen-lockfile
pnpm dev
pnpm test
pnpm build
```

## 教材とCMS

未公開教材はこのリポジトリとその履歴から除外しました。教材がない状態でもビルドできます。
公開用原稿の配置先は `content/lessons/*.mdoc` です。
**Publicリポジトリでは、draft指定や作業ブランチもGitHubから閲覧できます。非公開の下書きをCMSでこのリポジトリへ保存しないでください。**
ブラウザ更新用Decap CMSは `/admin/`、ローカル用Keystaticは `/keystatic`。認証設定・保存・公開の実運用確認は別途必要です。

## 配信

Cloudflare Pagesはmain更新時に `pnpm test && pnpm build` を実行し、`dist/` を配信します。
秘密情報は環境変数で管理し、Gitへ保存しません。
本文表示に生成AI APIへの接続は不要です。Pythonサンプルは `examples/` にあります。
