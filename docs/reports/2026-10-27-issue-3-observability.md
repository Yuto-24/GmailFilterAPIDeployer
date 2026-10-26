# Issue #3: 配備時間・API試行数の観測

対象: [Issue #3](https://github.com/Yuto-24/GmailFilterAPIDeployer/issues/3)。既存の読戻し診断・再開・50操作ごとの照合に、実行単位の時間とAPI試行数の集計を追加した。動作検証はオフライン合成テストで行い、別途、実Gmailの一覧APIを読取り専用で各3回計測した。フィルタ・ラベル変更やメール操作、実配備は行っていない。

## 要件と検証

| 要件 | 実装・検証 |
| --- | --- |
| API種類ごとの時間・試行数 | 固定7種類について試行/成功/失敗・中断/合計/平均/最大。新しい `test_observability.py` で合成時計による成功、失敗、リトライを検証。ラベル名・ID・条件をキーにしない。 |
| 実APIの遅延箇所 | 一覧API各3試行の読取り専用実測で、`filters.list`平均28.023秒、`labels.list`平均0.481秒を確認。一覧取得のAPI時間の約98.3%が`filters.list`。配備全体や変更APIの速度は今回測定していない。 |
| 試行と論理的な反映の区別 | 失敗した通信後に読戻しで反映を確認しても、そのAPI試行は失敗として残る。再送した場合は別試行。配備成功数は従来の進捗・最終結果に保持。バックオフはAPI所要時間に加算しない。 |
| Batchの観測限界 | 外側の実HTTP呼出しだけを時間計測し、内部要求は成功応答/失敗応答/応答不明を別集計。SDKを使った合成multipart応答で外側200+内部500、外側500、timeout、内部401再送拒否、callback保存失敗を検証。内部レイテンシは生成しない。 |
| 全6フェーズ | `run -v`でXML検証/認証/取得/差分/承認/配備の開始・終了をoffset付きISO 8601で表示。所要時間は`perf_counter`。例外・Ctrl-C時は到達したフェーズを停止として出力し、未到達フェーズの値は作らない。 |
| 承認待ち分離 | 実際の`input()`区間だけをグループ承認・最終承認で累積。合成OAuth 11秒、3回の入力各5秒、API各2秒により区別を検証。EOF終了も計上し、プロンプト中に計測ログを挿入しない。 |
| 通常表示と終了経路 | 非verboseは既存進捗と最終時間1行。正常、変更不要、辞退、上限による途中終了、反映済み再開、事前例外、配備中Ctrl-Cを検証。集計状態は`ContextVar`をresetし、連続呼出しやテスト間で持ち越さない。 |
| applyへの適用 | 3フェーズとAPI集計・最終時間を共通基盤で表示。入力承認待ちは0秒。合成CLI配備で検証。 |
| 不一致時の再照合と診断 | 既存の2秒→5秒、計3回の読戻しとID/ルール診断保存を維持。`test_readback.py`、`test_batch.py`が一過性欠落・恒久欠落・同件数の外部変更・失敗を検証。今回再実装していない。 |
| 復旧・照合間隔・要求数 | `test_cadence.py`の49/50/51操作、工程切替、末尾、途中終了、応答喪失、再開、CLI要求数の既存検証を維持。`test_recovery.py`、`test_one_line.py`で再送防止と承認順も検証。 |

単独APIはSDK要求実行の試行と時間を計測するため、要求準備・送信前認証更新を含む可能性がある。実送信パケット数を保証する計測ではない。Batchの時間は外側HTTP呼出しであり、callbackのジャーナル保存時間を含めない。全体・フェーズ・APIは包含関係があるため加算しない。

## 既存実装を含む完了監査

Issue #3への対応は今回の差分だけではなく、以下のマージ済みPRと今回の計測・検証を合わせて評価する。PR対応はローカルGit履歴、関数とテストは現在のコードを確認した。既存対応の再実装や、合意済み照合間隔の再変更は不要だった。

| 対応 | 既存PR・現在の関数 | 実物のテストと保証 |
| --- | --- | --- |
| API時刻と失敗時所要時間 | [PR #4](https://github.com/Yuto-24/GmailFilterAPIDeployer/pull/4)、`timed_operation`。今回`Observation`で試行集計と全フェーズへ拡張。 | `test_one_line.TestOneLine.test_verbose_run_reports_operation_timestamps_and_durations`、`test_operation_timer_reports_failure_without_swallowing_exception`。今回の`test_observability.MeasurementTests`、`CommandMeasurementTests`が全終了経路・二重加算防止・待ち時間分離を追加。 |
| 一過性読戻し不一致の再照合・診断保存 | [PR #5](https://github.com/Yuto-24/GmailFilterAPIDeployer/pull/5)、commit `eadde81`。旧経路の`verify_partial`から、現在の`perform_batched.readback`にも2秒→5秒・計3回の読取りと診断保存を継承。 | `test_readback.ReadbackTests.test_one_transient_missing_filter_recovers_without_mutation`、`test_persistent_missing_filter_fails_closed_after_three_reads`、`test_same_count_unapproved_external_change_does_not_pass`。現在の`perform`を通る`test_readback_glitch_after_delete_recovers_before_any_create`で診断JSONと、後続追加前の再照合成功を検証。 |
| HTTP失敗・rate-limitの有界再試行 | [PR #7](https://github.com/Yuto-24/GmailFilterAPIDeployer/pull/7)、commit `f0437f6`。`transient_api_error`、`read_operation`、`recover_mutation`、`SingleAttemptHttp`。再送前に変更未適用を照合し、通信層の暗黙再送を禁止。 | `test_recovery.ReadRetryTests.test_transient_read_statuses_retry_with_bounded_backoff`が429、403のrate-limit理由、500/502/503/504等を検証。`RecoveryTests.test_repeated_write_failures_are_bounded_and_keep_progress`が3回上限と進捗保持を検証。今回`test_rate_limit_exhaustion_stops_then_resumes_the_approved_plan`で429の3回停止→同じ計画で再開→最終一致も明示検証。 |
| 部分成功、進捗破損、再開 | [PR #8](https://github.com/Yuto-24/GmailFilterAPIDeployer/pull/8)と[PR #9](https://github.com/Yuto-24/GmailFilterAPIDeployer/pull/9)、`batch_progress`、`perform_batched`、`execute_filter_single/batch`。PR #9のcommit `d9295ec`で単独HTTPによる既存checkpoint再開を追加。 | `test_batch.BatchTests.test_partial_delete_stops_then_resumes_only_missing_steps`が429を含む部分失敗を再開し未反映分のみ実行。`test_journal_tamper_or_outside_change_blocks_resume_before_send`が不正な操作番号の進捗を送信前に拒否。`test_lost_response_after_nonprefix_creates_does_not_duplicate_on_resume`が応答喪失後の重複追加防止を確認。 |
| 同件数でもID・内容・ラベル不整合を停止 | `filter_snapshot`、`batch_progress`、`perform_batched.readback`。PR #7/#8/#9の厳密照合を現在の照合境界にも維持。 | `test_batch.BatchTests.test_wrong_existing_id_or_content_or_labels_stops_after_first_batch`がID差替え・条件変更・ラベル変更・予定外追加を拒否し診断を保存。`test_recovery.RecoveryTests.test_duplicate_known_ids_still_stop_before_mutation`が重複IDを変更前に拒否。 |
| API要求削減と照合境界の回帰 | [PR #14](https://github.com/Yuto-24/GmailFilterAPIDeployer/pull/14)、実装commit `692b559`、merge `68d5b44`。`perform`の既定`readback_interval=50`と`perform_batched`のcheckpoint境界で一覧要求を削減。初回・工程切替・末尾・途中終了も照合。 | `test_cadence.CadenceTests.test_read_request_reduction_does_not_change_mutations`が88操作で一覧178→6を検証。`test_49_50_51_and_repeated_boundaries_for_single_writes`、`test_run_and_apply_use_50_without_redundant_final_read`、`test_external_change_is_detected_at_boundary_before_51st_write`、`test_failed_readback_after_50_acks_stops_and_resume_does_not_repeat`が境界・CLI要求数・外部変更停止・読戻し失敗後の再開を検証。今回以下の合成時計比較も追加。 |

### 診断JSONの内容

現在の配備経路`perform_batched.readback`は不一致のたびに`diagnostics/batch_*.json`へ、`timestamp_utc`、`before_filters`、`observed_filters`、`labels`、`pending`、`expected_count`、`observed_count`、`missing_filter_ids`、`unexpected_filter_ids`を保存する。フィルタ行はIDと`filter.criteria`/`filter.action`を含む。送信中なら`pending`に操作種別・step・削除ID・成功/失敗応答等が残り、正常応答後の照合境界では`pending=null`となる。照合境界の操作進捗は同じ配備journalの`batch_checkpoint`と`acknowledged_*`/`completed_*`から追跡できる。

今回、`test_readback_glitch_after_delete_recovers_before_any_create`のassertを強化し、実際に保存された不足IDのcriteria/actionが元のルールに一致すること、観測行がその不足IDを除いた期待行と一致すること、ラベル・pending・offset付き時刻を検証した。`test_batch.BatchTests.test_google_error_reason_is_saved_with_partial_progress`は既にGoogleのstatus/reasonと部分進捗の保存を検証している。これにより「どのID・条件・処理を観測したか」の調査材料を残せる。Google側の一貫性や過去824/822不一致の根本原因を証明するものではない。

## 88操作の要求数・合成配備時間の比較

[`scripts/benchmark_readback_cadence.py`](../../scripts/benchmark_readback_cadence.py)を追加し、現在の実行器`perform()`へ同じ合成fixtureを渡して`readback_interval=1`と`50`を比較した。既存ラベルを使い、45削除→43追加、変更88件、初回46件→最終44件である。各Fake API要求の**execute時**に合成時計を一律1秒進める。API種類ごとの条件は両試行で同一で、実時間のsleepやGmail通信は行わない。setup・OAuth・承認・CPU・ディスク時間は時計に加算せず、配備区間のAPI待機だけを表す。

| 指標 | 照合間隔1 | 照合間隔50 |
| --- | ---: | ---: |
| `labels.list` | 89 | 3 |
| `filters.list` | 89 | 3 |
| `filters.delete` | 45 | 45 |
| `filters.create` | 43 | 43 |
| 一覧要求数 | 178 | 6 |
| 変更要求数 | 88 | 88 |
| 全要求数 | 266 | 94 |
| 合成時計の配備所要秒数 | 266.0 | 94.0 |

変更要求の内容・順序のSHA-256と最終ID/ルール状態のSHA-256は両試行で一致し、双方とも対象XMLに一致した。`test_cadence.CadenceTests.test_controlled_clock_comparison_preserves_mutations_and_target`でこれらの結果を検証する。これはPR #14で導入した要求削減の再評価であり、今回さらに高速化した値ではない。間隔1も現在の実行器を使う比較条件であり、過去リリース全体の再現ではない。

再現コマンド（SDK入りイメージ、リポジトリルートから実行）:

```bash
docker run --rm --network none --entrypoint python \
  -v "$PWD/gmail_filter_deployer.py:/app/gmail_filter_deployer.py:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/scripts:/app/scripts:ro" \
  gmail-filter-deployer:issue3 /app/scripts/benchmark_readback_cadence.py
```

この266→94秒は**実際のGmail配備時間でも、後述の一覧API平均値からの推定でもない**。本番の変更APIを含む配備前後の性能比較は未実施。制御された同一条件で、要求削減が配備区間のAPI待機を減らすことを検証した結果として扱う。

## オフライン検証結果

- 初回はSDK入りイメージ`gmail-filter-deployer:issue11`、完了監査後は`gmail-filter-deployer:issue3`で `python -m unittest discover -s tests` を実行。
- コンテナは `--network none`。本体、`tests/`、`scripts/`のみread-only mountし、認証情報・実アカウント設定はマウントしていない。
- 既存155件と計測用16件に、合成比較・429停止再開の2件を追加し、合計173件が成功（skipなし）。既存診断保存テストのassertも強化。`git diff --check`も成功。
- 新規16件の中に実SDKの合成HTTP応答テスト3件を含む。実Gmailの応答時間や本番短縮率の測定ではない。
- 新規計測関数・テストへdocstringを補完。アプリ本体と`test_observability.py`は初回レビュー対象commit `d7fd364`とのdocstring除外ASTが一致し、実行処理を変更していない。ローカルAST監査ではmainとの差分に含まれる本体関数18/18と計測テスト関数36/36がdocstring付き。

再現コマンド（リポジトリルートから実行、事前に依存入りイメージが必要）:

```bash
docker run --rm --network none --entrypoint python \
  -v "$PWD/gmail_filter_deployer.py:/app/gmail_filter_deployer.py:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/scripts:/app/scripts:ro" \
  gmail-filter-deployer:issue3 -m unittest discover -s tests
```

## 合成時計による表示例

`CommandMeasurementTests.test_verbose_six_phases_wait_and_deployment_do_not_double_count`の実行出力から抜粋。壁時計は実際のUTC時刻、所要秒数はテストが進める合成時計である。OAuth 11秒、入力15秒、API計38秒となり、全体64秒・入力除外49秒・配備区間30秒をそれぞれ検証した。

```text
[2026-10-26T16:51:45+00:00] [フェーズ完了] [1/6] 最新Master XMLを選択・検証 (0.00s)
[2026-10-26T16:51:45+00:00] [フェーズ完了] [2/6] Google OAuth認証とアカウント照合 (11.00s)
[2026-10-26T16:51:45+00:00] [フェーズ完了] [3/6] Gmailの現行ラベル・フィルタ取得 (4.00s)
[2026-10-26T16:51:45+00:00] [フェーズ完了] [4/6] 変更差分と保護ルールの照合 (0.00s)
[2026-10-26T16:51:45+00:00] [フェーズ完了] [5/6] 差分内容の確認と承認 (19.00s)
[2026-10-26T16:51:45+00:00] [フェーズ完了] [6/6] Gmailへ配備・API読戻し検証 (30.00s)
[2026-10-26T16:51:45+00:00] [計測] run全体（対話確認を含む）: 64.00s / 承認入力待ち: 15.00s / 承認入力待ち除外: 49.00s / 配備区間: 30.00s / 結果: 完了
[API集計] labels.list: 試行=6 成功=6 失敗/中断=0 合計=12.00s 平均=2.00s 最大=2.00s
[API集計] filters.list: 試行=6 成功=6 失敗/中断=0 合計=12.00s 平均=2.00s 最大=2.00s
[API集計] labels.create: 試行=1 成功=1 失敗/中断=0 合計=2.00s 平均=2.00s 最大=2.00s
[API集計] filters.create: 試行=3 成功=3 失敗/中断=0 合計=6.00s 平均=2.00s 最大=2.00s
[API集計] filters.delete: 試行=3 成功=3 失敗/中断=0 合計=6.00s 平均=2.00s 最大=2.00s
```

## 実Gmailの読取り専用API時間

2026-10-27 01:56 JST頃、アカウントpin照合後に`labels.list`と`filters.list`を各3回実行した。既存トークンはread-only mountし、認証更新はメモリ内のみ。変更API・メールAPIは呼ばず、planやjournalも更新していない。以下は合成時計の値ではなく、実際のAPI呼出しで得た所要時間である。

| API | 試行 | 成功 | 失敗 | 合計秒 | 平均秒 | 最大秒 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `labels.list` | 3 | 3 | 0 | 1.443 | 0.481 | 0.920 |
| `filters.list` | 3 | 3 | 0 | 84.069 | 28.023 | 29.796 |

各取得サイクルは30.719秒、27.523秒、27.279秒で、フィルタ件数はすべて930件だった。API合計85.513秒の約98.3%を`filters.list`が占めており、今回の一覧取得ではフィルタ一覧が支配的な待機箇所と判断できる。

この結果は、既存の50操作ごとの全件照合と重複一覧要求の削減を維持する判断を支持する。ラベル取得の削減だけで得られる余地は、この3回では小さい。外部変更の検出時期との兼ね合いがあるため、今回照合間隔をさらに広げたり、既定の単独HTTPをBatchに変更したりはしない。

3回同件数だったことは確認したが、全ID・ルール内容の不変性、Master一致、変更APIの所要時間、配備全体の性能はこの計測では検証していない。サンプル数3回の観測であり、過去の性能との対照実験や将来の遅延保証ではない。

## 保証と残る限界

Issue #3本文の毎操作全件照合は、Issue #11で通常50操作ごとの照合へ変更済み。開始・再開、工程切替、末尾、途中終了も照合する。外部変更の検知は次の照合まで遅れる。今回の計測追加は要求数、送信順、承認順、journal内容、再開規則を変更しない。

以前の合成88操作比較（一覧178→6要求、総266→94要求）は既存の照合間隔変更の根拠であり、この計測追加による性能改善値でも実Gmailの短縮率でもない。追加Batchの実測判断も変更せず、既定の単独HTTPを維持する。

過去の824/822不一致は、既存の読取り専用診断で永続的欠落が再現されなかったという範囲に留まる。原因は未確定であり、今回原因を解消したとは主張しない。今回の実測は一覧API各3回に限定され、配備全体・承認待ち・変更APIの実運用性能測定は未実施。これらの時間分離と終了経路は上記オフラインテストで検証した。
