# GitHub・Cloudflare接続案（確認待ち）

## 現状

2026-09-14、操作用ブラウザでGitHub・Cloudflareとも未ログインを確認。ログインを運営者へ依頼済み。GitHubアカウントMasaaki-jpの公開リポジトリ一覧は取得。非公開リポジトリは未確認。

## 作成・接続する対象案

- GitHub: Masaaki-jp/deepseek-ac、Private、mainブランチ
- 初回push: このサイトのソース・教材下書き・検証記録。履歴書原文、住所、生年月日、電話番号、APIキーは含めない。
- Cloudflare Pages: deepseek-ac、上記リポジトリのみGitHub Appへのアクセスを許可
- ルートディレクトリ: リポジトリのルート
- 本番ブランチ: main
- ビルド: pnpm test && pnpm build
- 出力: dist
- NODE_VERSION: 22
- PNPM_VERSION: 11.19.0
- Astroは静的配信。Functions、DB、公開API実行は作成しない。
- GitHub Actions: check.ymlを追加済み。読取権限のみ。実行は未検証。

名前の重複、既存Pages/Workers、プラン、Git連携権限はログイン後に調べる。無料枠と予算設定を確認し、追加課金を有効にしない。

## 公開操作の区切り

GitHubのPrivateリポジトリ作成・ソース保存と、CloudflareのWeb公開は別の操作。Pagesの初回デプロイはpages.devで外部から取得可能になる。noindexは非公開化ではないため、公開承認前にデプロイしない。

教材3本は公開前レビュー中。現時点でビルドすると準備中サイトだけが出力される。初期公開は教材1・2を文章演習としてレビュー後に公開し、教材3は実API検証後に追加する案。教材1・2についても未実施のサービス操作を検証済みと表示しない。

教材・利用条件・プロフィールの最終確認後、対象コミットと出力内容を提示して公開確認を得る。Cloudflare上でカスタムドメインdeepseek.acを追加し、自動で作成されるDNSレコードの内容を確認する。wwwの転送は別途設定。MX等のメールレコードは今回の対象外。

## 日常運用

作業ブランチでCMS編集 → 下書き保存 → ローカルプレビュー → 公開対象と日付を設定 → PR → Site checks → 差分確認 → mainへマージ → Pagesビルドと検査成功 → 配信。

GitHubブランチ保護が利用できる場合はSite checksを必須とする。プラン上利用できない場合は、手順で確認する制限があることを記録する。Pages自身のビルドにもテストを含める。

## 確認する内容

- リポジトリ作成先・可視性とCloudflareのアカウント
- 初回公開する教材、メールアドレス、再利用条件
- GitHub Appの許可範囲（当該リポジトリだけ）
- Pagesデプロイ成功、HTTPS、独自ドメイン、404
- 失敗ビルドで旧公開版が維持されることとロールバック

参照:
- https://developers.cloudflare.com/pages/configuration/git-integration/
- https://developers.cloudflare.com/pages/configuration/build-configuration/
- https://github.com/actions/checkout
- https://github.com/actions/setup-node
- https://github.com/pnpm/action-setup

## 2026-09-15 再開時の確認

Cloudflareの対象アカウントでログイン済みを確認。Workers & Pages一覧はNo projects found。GitHubは操作用ブラウザで未ログインのためログインを依頼。リポジトリ可視性・新規作成先の確認は未回答なので作成していない。

標準のpnpm run buildが正常終了。ローカル環境ではnpm_config_store_dirをインストール時と一致させて実行した。CloudflareやCIの新規環境ではこのローカル一時パスは使用しない。
