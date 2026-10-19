# Gmail Filter API Deployer

Gmailの現行フィルタをAPIから取得し、最新のMaster XMLとの**差分を安全に配備**するPython CLIです。Docker Compose対応。

**毎回のXMLリネームや旧Master XMLの準備は不要です。** `local/masters/`にバージョン付きのMaster XMLを元の名前で置くだけで、`validate` / `plan` / `verify` は最新版を自動選択します。`apply`は承認済み計画を使い、途中で別の最新版が届いても勝手に対象を切り替えません。

## 使い方（Docker）

```bash
mkdir -p local/masters
# Google Cloud OAuth Desktop client JSONを local/credentials.json に配置
# 例: local/masters/mailFilters_v0.14.0_proposed_master_20261019.xml を配置（リネーム不要）
docker compose build

# 1. 最も新しいMaster XMLを自動選択してオフライン検証
docker compose run --rm deployer validate

# 2. Gmailの現在のフィルタ一覧をAPIから取得し、差分を計画（書き込みなし）
docker compose run --rm --service-ports deployer plan --expected-email your-address@gmail.com

# 3. local/plan.jsonを読み、追加・削除対象と承認ハッシュを確認
# 4. 明示的に承認したときだけ配備
# 削除0件の場合は --allow-removals は省略可
docker compose run --rm deployer apply --expected-email your-address@gmail.com \
  --approve PLAN_OUTPUT_DIGEST --allow-removals

# 5. 配備後のGmail API上の全ルールとMaster XMLを照合
docker compose run --rm deployer verify --expected-email your-address@gmail.com
```

OAuth認証は初回 `plan` 時にブラウザから行います。DockerのOAuthコールバック用に `plan` には `--service-ports` を指定してください。Docker Composeは `127.0.0.1:8765` だけにポートを公開します。資格情報のアクセス権は `gmail.settings.basic`、`gmail.labels`、`openid`、`userinfo.email` です。

次回以降は、新版を`local/masters/`へ**そのまま追加保存**するだけで、自動的に最新版へ切り替わります。古い版は残して構いません。

### Master XMLの自動選択ルール

- `local/masters/*.xml` と `local/*.xml` を検索します。
- `mailFilters_v0.14.0_proposed_master_20261019.xml` のように、`vX.Y.Z` と `master` を含むファイルを候補にします。
- `patch`、`export`、`cumulative`、`incremental`等は選びません。
- **バージョン順、次にファイル名内の日付順**で最も新しいものを選びます。同順位が2つある場合は自動配備せず停止します。
- 明示指定が必要な場合のみ `--target local/masters/ファイル名.xml` を付けます。
- 旧来の `local/target.xml` も、候補が1つもない場合に限り互換性のため利用します。

### 比較元はGmail実設定

従来の`local/baseline.xml`は不要です。`plan`ではGmail APIからフィルタ一覧を取得し、ラベル名へ正規化した**実際の現行状態を基準スナップショット**として`local/plan.json`に保存します。そのスナップショットと最新版Masterとの差分を出します。比較元を任意に厳格指定したい場合に限って`--base some-old-master.xml`を指定できます。

`plan.json`は個人情報を含む場合があるため、Gitへコミットしないでください。`apply`は計画の承認ハッシュ、OAuth認証アカウント、XMLのSHA-256、Gmail実状態を再確認します。追加後にAPI一覧を再取得し、古いフィルタの削除は最後に実施します。途中で中断しても元のplanとlogを保持して同じ承認ハッシュで再開できます。

## 不変条件と安全制約

- `plan` / `validate` / `verify` はフィルタの書き込みを行いません。`apply`だけ書き込みます。
- **Action/DeleteとTrash例外のフィルタは変更禁止**。ライブGmail設定との差分でも厳格検証します。
- APIの現在状態を正本として差分計画に含めますが、**余分な既存フィルタの削除は `apply --allow-removals` と承認ハッシュの両方が必要**です。削除件数と詳細を`local/plan.json`で確認してください。
- 未知の複雑な条件、未対応アクションや転送フィルタ、曖昧なMaster候補、追加中の1000件上限超過、大量差分、計画からの予期しない状態変化は停止します。
- APIで読み戻して一致しても、Gmail UIのfresh exportと実際の新着メール分類を別途検証する必要があります。**実Gmailへの配備はまだ未検証**です。
- `local/`内にはOAuthクライアントJSON、アクセストークン、Master XML、配備計画などの**個人的なデータ**があるため、公開GitHubリポジトリへPushしないでください。`local/`はGit / Docker buildから除外しています。

## Dockerを使わず実行する場合

```bash
pip install -r requirements.txt
python gmail_filter_deployer.py validate
python gmail_filter_deployer.py plan --expected-email your-address@gmail.com
python -m unittest discover -s tests -v
```

Gmail API公式資料：[Managing Filters](https://developers.google.com/workspace/gmail/api/guides/filter_settings) / [users.settings.filters](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters)。