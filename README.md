# deepseek.ac 初期実装

公開準備中。教材3本は下書きで、API実行・書籍対応・Cloudflare接続は未検証です。

## ローカルで使う

Node.js 22.12以降の22系、pnpm 11を想定。依存はpnpm-lock.yamlで固定しています。

```sh
pnpm install --frozen-lockfile
pnpm dev
```

- サイト: http://127.0.0.1:4321/ja/
- CMS: http://127.0.0.1:4321/keystatic
- 下書き確認: http://127.0.0.1:4321/ja/preview/ds-001/
- ds-002、ds-003も同様です。

CMSはローカルファイルを編集します。認証はありません。必ず127.0.0.1で起動し、ネットワーク公開しないでください。ブラウザだけで遠隔から更新する構成は未導入です。

## 更新手順

1. 作業ブランチを作り、`pnpm dev`を起動。
2. CMSで教材を編集。公開状態は「下書き」のまま保存。
3. `/ja/preview/教材ID/`で確認。未検証の内容に検証日を入れない。
4. 本文・画像・コードの権利、出典、費用、手順、書籍の対応、検証結果を確認。
5. CMSで「公開対象」に変更し、公開日・更新日を入力して保存。これだけで外部公開されることはありません。
6. `pnpm test`、`pnpm build`を実行。`pnpm preview`で公開用ビルドを確認。
7. `git diff`で差分と秘密情報混入の有無を確認してコミット。
8. 公開先・公開内容の確認後、承認されたGitリモートへpushし、本番ブランチへ反映。

既に公開した教材を更新する場合は、公開対象のまま作業ブランチ上で編集します。本番ブランチへ直接pushしない運用にしてください。公開状態を下書きに戻して本番反映すると公開中の教材は消えます。

本文はcontent/lessons/*.mdoc。一般的なMarkdownのみを使用します。Markdoc固有タグはビルドで拒否します。画像はpublic/images/以下へ置き、本文では`/images/ファイル名`を使います。内部メモ・秘密情報は公開原稿に書かないでください。

## 公開用の出力

`dist/`だけをCloudflare Pagesへ渡す構成です。CMS・プレビュー・下書きは出力しません。HTML・Markdown・コピー・公開一覧・サイトマップ・llms.txtは同じ公開原稿に基づきます。

現在は全ページnoindex、robots.txtでクロールを停止しています。これはアクセス制御ではありません。公開承認後に、Base.astroのrobotsとpublic/robots.txtを公開用に変更してください。

## 記録

- [構成とCMS比較](docs/decisions.md)
- [検証結果](docs/verification.md)
- [Cloudflare接続と公開前の残作業](docs/release.md)
