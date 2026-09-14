# 設計判断（2026-09-14）

## 最小構成

Astro静的出力 + Keystaticローカル編集 + Git管理 + Cloudflare Pages（接続予定）。7つの日本語ページ、404、教材テンプレート、下書き3本。会員機能・検索・API公開デモなし。外部フォントや解析なし。月額固定費の追加を避けるため、編集環境もPC内で動かす。

## CMS比較

|候補|原稿保存|認証・運用|判断|
|---|---|---|---|
|Keystatic local|ローカルGit対象ファイル|PCで起動。外部認証不要。公開はGit運用と分離|初期実装に採用。導入済み|
|Keystatic GitHub|GitHub|GitHub連携とサーバー側のCMS実行環境が必要|アカウント確認後に再評価|
|Decap CMS|GitHub等のGit|OAuth構成の選定が必要。editorial_workflowで下書きPRと公開マージを分離できる|遠隔ブラウザ運用の比較候補|

ローカルCMSには承認者を分けるワークフローはない。保存→プレビュー→公開対象指定→ビルド→Git反映の運営者手順で分ける。Gitリモートとブランチ保護、認証方式は未確定。手順だけの制限を技術的な承認制御とは呼ばない。

## 配信候補

Cloudflare PagesのGit連携で本番ブランチmain、ビルドpnpm build、出力distを想定。無料枠の上限と利用中アカウントを公開設定時に再確認。既存プロジェクトがWorkersの場合は構成を再評価し、二重に作成しない。

下書きプレビューはローカル限定。ブランチの公開プレビューへ下書きを載せない。ビルド・リンク確認が失敗したら配信工程に進まない。正常なCloudflare公開版を維持する動作は、接続後に実機確認する。

## バージョン

現行最新のAstro 7ではなく、Keystatic 0.5 / Astro integration 5との組み合わせでAstro 5.18.2を初期確認した。これは長期採用の確定ではない。公開前に脆弱性監査とサポート状況を確認し、必要なら対応バージョンへ更新する。

## 実装順序と20〜30時間の配分目安

1. 環境・資産確認と構成決定 2〜3時間
2. ページ・教材構造・CMS 6〜8時間
3. 既存資産の教材化と操作検証 6〜9時間
4. Git/Cloudflare接続、公開テスト 3〜4時間
5. 運営手順・制作事例の仕上げ 3〜4時間

認証連携や教材検証が膨らむ場合は初期教材数を減らし、未検証の教材を公開しない。遠隔CMSは次段階へ移す。上記は実績時間ではなく見積もり。

## 調査元

- https://keystatic.com/docs/local-mode
- https://keystatic.com/docs/installation-astro
- https://keystatic.com/docs/recipes/astro-disable-admin-ui-in-production
- https://decapcms.org/docs/editorial-workflows/
- https://developers.cloudflare.com/pages/framework-guides/deploy-an-astro-site/
- https://developers.cloudflare.com/pages/platform/limits/

独立した教育サイトと明記。AIが初期コード・下書きを作成、ユーザーが要件を提示。実際の教材内容・書籍対応・権利・公開の確認は未完了。
