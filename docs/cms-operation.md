# ブラウザ更新用CMS（接続前）

2026-09-15: Decap CMS 3.16.2 の管理画面、GitHub backend、editorial workflow、Cloudflare Pages Functions用認証処理を実装。外部アカウント接続と実際の保存・公開は未検証。

## 役割

- `/admin/`: Decap CMS。原稿は `content/lessons/*.mdoc`（YAML frontmatter + Markdown）。
- GitHub: `Masaaki-jp/deepseek-ac` を非公開で作成する予定。原稿・編集履歴を保存。
- Cloudflare Pages: main のビルド成功時に静的ファイルを反映。
- Pages Functions: `/api/auth` と `/api/callback` だけGitHub認証を処理。本文表示には呼び出さない。
- 既存のKeystaticは開発PC用の補助手段。Web更新の本番経路はDecapへ変更。

## 接続設定（未実施）

1. GitHub専用リポジトリ作成とソース登録。
2. Cloudflare Pagesをそのリポジトリだけに接続。本番ブランチ main、出力 dist、Node 22、ビルドは `pnpm test && pnpm build`。依存関係はlockfile固定でインストール。
3. **Preview deploymentsは無効**にする。公開対象に変更した原稿がCMSの作業ブランチにある段階でも公開URLを作らない。プレビューは認証済みCMS内で行う。
4. CMS用GitHub Appを作成。Contents read/write、Pull requests read/write、Metadata read。インストール対象はこのリポジトリだけ。Webhooks不要。GitHub Appのユーザー認証フローを使用し、広いOAuth `repo` scopeは要求しない。Decapとの実接続適合性は要検証。
5. Callback URLは管理画面と同じ確定したHTTPS originの `/api/callback`。PagesのProduction環境で `CMS_ORIGIN`、`GITHUB_CLIENT_ID`、暗号化Secret `GITHUB_CLIENT_SECRET` を設定。秘密値はユーザーが画面で入力し、チャットやGitへ保存しない。
6. 公開先確定後、下記の受入確認を行う。GitHub Appのユーザートークンが期限切れの場合はCMSからログアウトして再認証する。

## 日常の更新（接続後に実地確認する手順）

1. `/admin/` でGitHubログインし、教材を選択または新規作成。
2. 教材IDは ds-004 形式で重複なし。作成後は変更しない。
3. 本文・概要・所要時間・環境を編集。検証日は実施したときだけ入力。
4. Saveで作業ブランチへ保存。編集画面のプレビューを確認する。
5. 初回のサイト掲載時は「サイト公開対象」を公開対象にし、公開日・更新日を入力する。この項目だけでなく、最後のPublish操作も必要。
6. ワークフローでレビュー後、Publish。mainへ反映され、Cloudflareのビルドが開始する。
7. ビルド成功と公開ページを確認。失敗時は旧公開版を維持し、GitHub/Cloudflareのログから原稿を修正する。

status=draft の原稿はPublishでmainへ保存してもサイトから除外される。CMSのワークフロー状態と原稿のサイト公開対象は別。公開済み教材の修正も作業ブランチで保存し、Publishまでは旧本文を維持する。

画像は公開対象の素材だけを登録する。Gitから配信される public/images 以下は本文のstatusとは独立して公開されるため、秘密情報・未公開資料をアップロードしない。

## 接続後の受入確認

- ログイン、既存教材の取得、本文編集、新規作成、画像と代替テキスト、コード表示。
- Save後に作業ブランチができ、mainと公開版が変わらない。
- CMS内プレビューで日本語・コード・注意事項を確認できる。
- Publish後にmainへ反映し、ビルド成功後にHTML/Markdownが一致する。
- ビルド失敗時の旧版維持、トークン期限切れ時の再ログイン。
- 下書き用の公開プレビューURLが生成されない。

参考: https://decapcms.org/docs/editorial-workflows/ 、 https://decapcms.org/docs/github-backend/ 、 https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app
