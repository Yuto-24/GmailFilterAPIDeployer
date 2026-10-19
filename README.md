# Gmail Filter API Deployer

Gmailのフィルタを、Gmail設定画面からエクスポートしたAtom XMLを正本として、Gmail API経由で**差分配備**するPython CLIです。Docker Composeによる実行に対応します。

**既定では書き込みをしません。** `plan`はGmailフィルタを読み取って差分計画を生成し、`apply`だけが承認ハッシュを要求して配備します。既存の`Action/Delete`ルールとSpam防御ルールは変更しない方針です。過去メールのラベルや受信トレイには触れません。

> **公開リポジトリへの情報漏えい防止**：個人のMaster XML、GmailからのExport、OAuthクライアントJSON、アクセストークン、配備計画・ログをGitに登録しないでください。すべて `local/` に格納します。ファイル内容の検索式やラベル名から個人的な情報を推測できる場合があります。

## 必要条件

- Docker Engine / Docker Desktop と Docker Compose v2（`docker compose`）
- Google Cloudで有効化した **Gmail API** とOAuthクライアント（**Desktop application**）
- GmailのOAuth認可画面で利用するGoogleアカウントの許可

OAuthの利用権限：`gmail.settings.basic`、`gmail.labels`、`openid`、`userinfo.email`。メール本文は読みません。OAuth同意画面がテストモードの場合、テストユーザーを登録してください。

## 1. 秘密情報とXMLの配置

```bash
mkdir -p local
```

次のように各ファイルを**ローカルの `local/` にだけ**置きます。

```text
local/
├── credentials.json  # Google CloudのOAuth Desktop client JSON
├── baseline.xml     # 配備済みと想定する旧Master（例 v0.12.0）
├── target.xml       # 配備予定の新Master（例 v0.14.0）
└── token.json        # 初回OAuth後に自動生成（共有・commit禁止）
```

`baseline.xml`は必ずしも現在のGmail設定と一致しているとは限りません。`plan`が実GmailのAPIフィルタを読み取り、**既知の基準と完全一致**した場合だけ配備計画を生成します。途中のMasterも検証する場合は `--also-baseline local/v013.xml` を指定します。

Linuxで書き込み権限が不足する場合は、ホストの `id -u` / `id -g` を `.env` の `LOCAL_UID` / `LOCAL_GID` に設定してください。

## 2. Dockerビルドとオフライン検証

```bash
docker compose build
docker compose run --rm deployer --help

docker compose run --rm deployer validate \
  --base local/baseline.xml \
  --target local/target.xml \
  --out local/validation.json
```

`validate` はGmail認証なしでXMLの変換、重複、Delete/Trash変更、追加先行時1000件制限などを検査します。

## 3. 認証と配備計画（読み取りのみ）

```bash
docker compose run --rm --service-ports deployer plan \
  --expected-email your-address@gmail.com \
  --base local/baseline.xml \
  --target local/target.xml \
  --out local/plan.json
```

初回はターミナルにGoogle OAuth URLが表示されます。**実行したPCのブラウザ**で開いて認可します。OAuthリダイレクトは `http://localhost:8765/` で、Docker Composeはホストの `127.0.0.1:8765` だけを公開しています。

ブラウザが自動起動しないのは正常です。認証中はこのコマンドを実行したコンテナを終了しないでください。次回以降は保存された `local/token.json` を再利用します。

Docker Composeの `run` で認証ポートを公開するには**`--service-ports`が必要**です。同時に複数の認証コンテナを起動しないでください。

既存のGmailフィルタがMasterと一致しない場合は安全のため停止します。新しい設定を無条件に上書きしません。

## 4. 承認して配備

`plan` が表示した差分と `local/plan.json` をレビューし、`approval_digest` を指定します。

```bash
docker compose run --rm deployer apply \
  --expected-email your-address@gmail.com \
  --plan local/plan.json \
  --approve <planが表示したapproval_digest> \
  --log local/deploy_log.json
```

このコマンド**だけ**がフィルタを作成・削除します。プランのSHA検証、Googleアカウント照合、Delete/Trash不変検証、実状態の段階的照合を行います。新規フィルタを先に追加して検証し、最後に旧ルールを削除します。APIの操作は原子的ではないため、途中で中断した場合は**元のplanとlogを保持したまま**再実行してください。想定外の変更が見つかった場合は停止します。

## 5. 配備後の確認

```bash
docker compose run --rm deployer verify \
  --expected-email your-address@gmail.com \
  --target local/target.xml \
  --out local/verify.json
```

APIから取り直したフィルタがMasterと完全一致したことを検証します。加えてGmail設定画面の**新しいExport**を取得し、XMLの意味的な差分確認と新着メールのラベル挙動監査をしてください。API一致だけではフィルタ検索式の期待動作を保証しません。

## 開発者向け（Docker外）

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python gmail_filter_deployer.py validate --base local/baseline.xml --target local/target.xml
```

## 安全性・制限

- Gmail APIのフィルタ機能はXML直接インポートではないため、`criteria`/`action`に変換します。APIとUIの表現が一致しない複雑な検索式は安全側で止めます。
- GoogleのOAuthトークンは `local/token.json` に保存します。`local/` はGitとDocker build contextから除外しますが、PC上のファイル権限も保護してください。
- 作成中に既存フィルタ数が1000件を超える計画は拒否します。
- 現行プログラムはルール一式を無差別に置き換えません。**`Action/Delete`とTrash例外を不変**として扱います。
- Gmail APIによる実アカウントへの配備は未検証です。初回は `plan` と実際のGmail Exportによる整合確認を優先してください。
- Windows PowerShellでは行継続はバックスラッシュではなくバッククォートです。コマンドを1行にしても構いません。

Gmail API公式資料：[フィルタの管理](https://developers.google.com/workspace/gmail/api/guides/filter_settings)、[Filters API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters)。