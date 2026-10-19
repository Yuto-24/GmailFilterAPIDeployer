# Gmail Filter API Deployer

Gmailに現在登録されているフィルタと、最新版Master XMLとの差分を照合して配備するCLIです。Docker Composeに対応します。

## 最短の運用：配備は1コマンド

最初に1度だけ`local/credentials.json`（Google Cloud OAuth DesktopクライアントJSON）、`.env`（Gmailアドレス）を用意します。Master XMLは`local/masters/`に**元のファイル名のまま**保存します。旧Masterやリネームは不要です。

```bash
cp .env.example .env
# .envの GMAIL_EXPECTED_EMAIL を自分のアドレスに変更
mkdir -p local/masters
# local/credentials.json および local/masters/mailFilters_v0.14.0_proposed_master_20261019.xml 等を配置
docker compose build
```

以後は毎回、以下の**1コマンドだけ**実行します。

```bash
docker compose run --rm --service-ports deployer run

# フィルタごとの処理内容も表示するとき
# docker compose run --rm --service-ports deployer run -v
```

実行内容は **XML検証 → OAuth認証・対象アカウント照合 → 現行フィルタ取得 → 差分計画作成 → **旧→新の置換を含む全変更内容の表示** → 最後に対話確認 `[y/N]` → 承認された場合だけAPI配備 → 読戻し検証** です。通常実行では`[1/6]`〜`[6/6]`の工程、対象Master、Gmail現行件数、追加・削除件数、20件ごとの照合結果を表示します。`run -v`ではさらに**ルール単位の追加・削除結果、対象条件・ラベル、再取得チェックポイント、進捗ログ位置**を表示します（OAuthトークン等の認証情報は表示しません）。
Action/Deleteも含めてMaster XMLを正本として差分配備します。変更がなければそのまま終了します。`--approve`や`--allow-removals`は**`run`では不要**です。初回OAuthでは表示URLをブラウザで開いて認証します（ホスト側ローカルポート`127.0.0.1:8765`）。

プロンプトでは**`y`のみ実行**、Enter・`n`は中止します。TTYを使えない環境（`-T`、CI、パイプ等）では自動承認せず停止します。既存フィルタを削除する変更も、対象の全件を表示してから**最後に1回だけ**確認します。TRASHへの追加は今後の一致メールを実際にゴミ箱へ移しますので、表示した条件を確認してください。メールそのものを削除する機能はありません。

中断した場合は`local/run_pending.json`に固定した計画に対し、現在のGmail設定がその計画の途中状態であることを検証して再開します。別の変更が見つかった場合は停止します。計画ごとのログは`local/logs/`に保存するため、**翌週の新しい配備でもログをリネームする必要はありません**。計画全文は`local/run_plan.json`に保存されます。`local/`と`.env`はGit管理対象外です。

## `deployer`以降のCLI仕様

`docker compose run --rm --service-ports deployer <subcommand> [options]` の`deployer`はComposeのサービス名、`<subcommand>`以下はPython CLIの引数です。`--service-ports`はDocker Composeのオプションで、初回OAuth認証を行うコマンド（`run`/`plan`）で使います。認証済みでも付けたままで問題ありません。

| サブコマンド | 意味 | Gmailへの変更 | 使用例 |
|---|---|---|---|
| `run` | 検証から承認・配備・検証まで一括 | `y`でのみ変更 | `... deployer run` |
| `validate` | XML構文・処理内容の静的検証 | なし | `... deployer validate` |
| `plan` | 実フィルタから差分計画を保存 | なし | `... deployer plan` |
| `apply` | 保存された`plan.json`を厳格に適用 | あり | `... deployer apply --approve HASH --allow-removals` |
| `verify` | 実フィルタとMasterの一致を確認 | なし | `... deployer verify` |

### `run`：推奨

- `--target PATH`：バージョン自動選択を使わず、対象Master XMLを明示。
- `--batch-size N`：APIを読み戻して途中一致を検証する追加件数。**1〜25、既定20**。UIインポート単位ではありません。
- `-v`, `--verbose`：フィルタ単位の処理結果や照合状況を詳しく標準出力に表示。実行結果の保存先は従来どおり`local/logs/`です。
- `--expected-email ADDRESS`：`.env`の`GMAIL_EXPECTED_EMAIL`より優先するアカウント照合先。
- `--local-dir PATH`：Masterと配備記録の保存フォルダ。既定`/app/local`（Docker内）。
- `--client-secrets PATH`：OAuth DesktopクライアントJSON。既定`/app/local/credentials.json`。
- `--token PATH`：OAuth認証トークン。既定`/app/local/token.json`。

`run`に`--approve`/`--allow-removals`はありません。承認ハッシュは内部で作成・照合し、対話確認を行います。

### `validate`：オフライン検証

- `--target PATH`：検証対象を指定。省略時は最新Master。
- `--base PATH`：任意の旧Masterと比較し、Delete/Trash変更等を検出。
- `--out PATH`：検証結果JSONを保存。
- `--local-dir PATH`：Master検索先を変更。

### `plan`：詳細な配備計画

- `--target PATH`：配備先Masterを指定（省略時は最新版）。
- `--base PATH`：現行Gmailがその旧Masterと完全一致する場合だけ計画する、厳格モード。
- `--also-baseline PATH`：`--base`と同時に追加する既知Master。複数指定可。
- `--out PATH`：計画保存先。既定`/app/local/plan.json`。
- `--expected-email`、`--local-dir`、`--client-secrets`、`--token`：`run`と同じ。

### `apply`：上級者向け分割操作

- `--approve HASH`：`plan`の`approval_digest`を指定。**必須**。
- `--allow-removals`：旧フィルタの削除を伴う配備を明示的に承認。ない場合は削除対象があれば停止。
- `--plan PATH`：`plan.json`のパス。既定`/app/local/plan.json`。
- `--log PATH`：進捗記録。既定`/app/local/deploy_log.json`。
- `--batch-size N`：API読戻し間隔（1〜25、既定20）。
- `-v`, `--verbose`：`run`と同じ詳細表示。
- `--expected-email`、`--local-dir`、`--client-secrets`、`--token`：`run`と同じ。

### `verify`：配備後の完全比較

- `--target PATH`：照合対象Master。
- `--out PATH`：照合結果JSONの保存先（既定`/app/local/verify.json`）。
- `--expected-email`、`--local-dir`、`--client-secrets`、`--token`：`run`と同じ。

コマンドの全オプションは次で確認できます。

```bash
docker compose run --rm deployer --help
docker compose run --rm deployer run --help
docker compose run --rm deployer apply --help
```

## XML自動選択の規則

`local/masters/*.xml`と`local/*.xml`のうち、ファイル名に`vX.Y.Z`と`master`を含むファイルからバージョン・日付が最も新しいものを選びます。`patch`、`export`、`cumulative`、`incremental`、`baseline`、`target`は候補から除外します。同順位の候補が複数あれば停止し、明示`--target`指定を求めます。候補がない場合のみ旧`local/target.xml`を使います。

## トラブルシューティング：Gmail API無効（HTTP 403）

`Gmail API has not been used in project ... or it is disabled` / `accessNotConfigured` は**Google CloudのGmail APIが有効になっていない**ことを示します。エラー内のGoogle Cloudプロジェクトを選択し、[Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)を有効化してから同じコマンドを再実行してください。反映には数分かかる場合があります。

これはOAuth同意やメールのラベル設定とは別の問題です。`[3/6] Gmailの現行ラベル・フィルタを取得中`で発生した場合は**配備処理は開始されておらず、Gmailフィルタは一切変更されていません**。スクリプトもこの原因を個別に表示するようにしてあります。配備が実際に開始された後で別のエラーが出た場合は、進捗ログと現在のGmail設定を確認してから再開してください。

## 安全上の制約

- 配備対象アカウントを`.env`の`GMAIL_EXPECTED_EMAIL`で固定し、実際にOAuthログインしたGoogleアカウントと照合します。指定がない場合は停止します。
- `Action/Delete`もMaster XMLに合わせて追加・削除・置換します。ただしDeleteは**ラベル付与のみ**で、既読化・スター・重要・Trashの複合操作は禁止です。Trash移動アクションもMaster XMLを正本として差分更新します。新Master内のTrashアクションは従来どおり`in:spam`条件に限定し、変更があれば全件と警告を最終確認前に表示します。既存Gmail側の余分なTrashフィルタは承認後に削除対象となります。自動Archiveは禁止します。
- UIベースのフィルタXMLをGmail API形式に変換できない条件、転送フィルタ等の予期しないアクション、1000件上限超過、途中状態不一致の場合は停止します。大規模差分は件数と条件をすべて表示して承認を求めます。
- 承認前にAction/Deleteを含む新規追加・削除・置換の全差分を表示し、最後の1回の確認で`y`を入力した場合のみ変更します。Gmail APIで現在のフィルタを取得し、追加後の読戻し確認を行ってから旧ルールを削除します。
- API読戻し一致は**Gmail UIのfresh exportや新着メッセージの分類が完全であることの証明ではありません**。実配備後は必要に応じて別途検証してください。
- Gmail実アカウントでのAPI配備はまだ実施していません。初回の`run`は特に削除対象と差分の詳細を確認してください。

依存ライブラリは`requirements.txt`を使用します。Dockerを使わない場合は`pip install -r requirements.txt`後に`python gmail_filter_deployer.py run`で同じCLIを呼べます（環境変数の設定が別途必要です）。

Gmail API公式資料：[Managing Filters](https://developers.google.com/workspace/gmail/api/guides/filter_settings) / [users.settings.filters](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters)。