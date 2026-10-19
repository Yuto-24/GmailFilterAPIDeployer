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

**Gmailの1000フィルタ上限対策：** 新規配備計画では、最終承認後に**旧フィルタを先に削除し、次に新フィルタを追加**します。現行846件・削除257件・追加341件なら、846→589→930件となり上限を超えません。削除と追加の間には一時的に旧振り分け条件がない時間帯が生じます。配備は各操作後にGmail APIで照合し、途中中断時も承認した手順の進捗から再開します。過去の追加先行方式で実行中だった計画は、その実行順序のまま再開します。表現差による不要な差分の最適化は [Issue #1](https://github.com/Yuto-24/GmailFilterAPIDeployer/issues/1) に分離しています。\n\n**Gmail APIの読戻しが一時的に不一致の場合：** 削除後または追加の検証タイミングで期待したフィルタ集合と一致しなければ、**次の変更操作を行わず**、2秒、5秒の待機を挟んで最大2回の追加読取り（計3回）を行います。期待するルール集合が完全一致した場合のみ続行します。なお、3回とも不一致・未対応のAPIエラー・外部変更の場合は停止し、承認済み計画とログを保持します。失敗した書込み操作（HTTP 500等）を自動で再実行する機能ではありません（[Issue #2](https://github.com/Yuto-24/GmailFilterAPIDeployer/issues/2)）。\n\n不一致を検出した際は、**最初の読戻し結果を含めて毎回** `local/logs/diagnostics/readback_*.json` に時刻・実フィルタID・ルール内容・差分ハッシュを保存します。削除後の照合では、削除直前のID一覧と比較した不足IDも記録します。不一致が後で解消しても、診断記録は保持されます。診断ファイルは個人情報を含むため、公開リポジトリ等にはアップロードしないでください。\n\n**API呼び出し数の削減：** 旧ルール削除直前のラベル・フィルタ一覧の完全取得は維持し、削除後の照合ではその時点で確認済みのラベルID対応を再利用してフィルタ一覧だけ読み直します。原則、削除1件あたり5リクエストから4リクエストに減ります。未知のラベルIDを検出した場合は完全取得に戻します。追加段階の照合、最終照合は従来どおりラベル一覧も再取得します。\n\n中断した場合は`local/run_pending.json`に固定した計画に対し、現在のGmail設定がその計画の途中状態であることを検証して再開します。別の変更が見つかった場合は停止します。計画ごとのログは`local/logs/`に保存するため、**翌週の新しい配備でもログをリネームする必要はありません**。計画全文は`local/run_plan.json`に保存されます。`local/`と`.env`はGit管理対象外です。

## 削除対象の事前検証と一部未適用

最終承認前に、削除予定ルールがGmail API上のフィルタID1件に一意に対応するか検証します。0件（存在しない）、複数件（重複）、ID欠落は「スキップ予定」として明細を表示します。特定できないルールは削除せず、他の承認済みルールは処理します。

スキップしたルールは承認済みの有効ターゲットに残し、配備後もMasterとの完全一致と区別します。スキップによって1000件上限に達する場合は最終承認前に原因を表示します。最終承認直前にはGmail実設定を再取得して、差分取得時点から変更がないか照合します。

配備完了後には追加・削除成功・スキップ・Masterへの未適用差分を表示します。外部から途中で設定が変わり、処理済み状態を確定できない場合はそれ以上の変更をせず、途中計画を保存します。

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
- `-v`, `--verbose`：フィルタ単位の処理結果や照合状況に加え、API読取・削除・追加の開始/終了時刻（UTCオフセット付きISO 8601）と所要秒数を標準出力に表示。エラー発生時にも失敗した処理と経過時間を表示します。承認待ちを含む`run`全体と配備処理のみの所要時間も分けて表示します。実行結果の保存先は従来どおり`local/logs/`です。Docker内タイムゾーンがUTCなら`+00:00`になるため、ホスト端末のJSTと時刻を比較する際はオフセットを考慮してください。
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