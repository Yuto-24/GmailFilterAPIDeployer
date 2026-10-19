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

実行内容は **XML検証 → OAuth認証・アカウント照合 → 差分計算 → 基本の件数表示 → Action/Delete全件表示・グループ承認 → Trash全件表示・グループ承認 → 最終確認 `[y/N]` → 配備 → API照合** です。

**承認の順序は基本 → Action/Delete → Trash → 最終確認**です。基本（Type・Service・Org等）は個別承認せず、最後の全体承認に含めます。基本の詳細は`run -v`で表示できます。DeleteとTrashの差分は`-v`なしでも、追加・削除する全ルールの条件と処理を表示します。条件変更はGmail API上の旧ルール削除と新ルール作成として表示します。該当差分がないカテゴリの確認は省略します。

Action/DeleteとTrashはそれぞれ`[y/N]`で承認または見送れます。見送ったグループはGmailの現行ルールを維持します。**実際のGmail変更は、最後の全体確認に`y`と入力した後にのみ開始**します。`n`・Enter・Ctrl+Cは配備しません（ただし、すでに変更が始まっている配備の再開確認は別です）。

`--approve`および`--allow-removals`は`run`では不要です。初回OAuthでは表示URLをブラウザで開いて認証します（ホスト側ローカルポート`127.0.0.1:8765`）。TTYを使えない環境（`-T`、CI、パイプ等）では自動承認しません。

**Gmailの1000フィルタ上限対策：** 新規配備計画では、最終承認後に**旧フィルタを先に削除し、次に新フィルタを追加**します。現行846件・削除257件・追加341件なら、846→589→930件となり上限を超えません。削除と追加の間には一時的に旧振り分け条件がない時間帯が生じます。配備は各操作後にGmail APIで照合し、途中中断時も承認した手順の進捗から再開します。過去の追加先行方式で実行中だった計画は、その実行順序のまま再開します。表現差による不要な差分の最適化は [Issue #1](https://github.com/Yuto-24/GmailFilterAPIDeployer/issues/1) に分離しています。\n\n中断した場合は`local/run_pending.json`に固定した計画に対し、現在のGmail設定がその計画の途中状態であることを検証して再開します。別の変更が見つかった場合は停止します。計画ごとのログは`local/logs/`に保存するため、**翌週の新しい配備でもログをリネームする必要はありません**。計画全文は`local/run_plan.json`に保存されます。`local/`と`.env`はGit管理対象外です。

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
- 承認前にAction/Deleteを含む新規追加・削除・置換の全差分を表示し、Action/Delete・Trashのグループ承認後、最後の全体承認が得られた場合のみ変更します。未承認のグループはGmail現行状態を維持し、次の`run`で再提案します。Gmail APIで現在のフィルタを取得し、**旧ルールの削除→新ルールの追加**の順に読み戻し確認しながら変更します。
- API読戻し一致は**Gmail UIのfresh exportや新着メッセージの分類が完全であることの証明ではありません**。実配備後は必要に応じて別途検証してください。
- Gmail実アカウントでのAPI配備はまだ実施していません。初回の`run`は特に削除対象と差分の詳細を確認してください。

依存ライブラリは`requirements.txt`を使用します。Dockerを使わない場合は`pip install -r requirements.txt`後に`python gmail_filter_deployer.py run`で同じCLIを呼べます（環境変数の設定が別途必要です）。

Gmail API公式資料：[Managing Filters](https://developers.google.com/workspace/gmail/api/guides/filter_settings) / [users.settings.filters](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters)。