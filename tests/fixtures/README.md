# 条件式正規化の旧計画fixture

`condition-equivalence-legacy.json`は、正規化実装前のcommit
`f2240c9b662fbc4c510ac3f353540f133b8d1c00`の実モジュールで作成した合成データです。
実アカウントのMaster・フィルタ・ID・ラベル・journalは含みません。

v3計画とグループ承認済みv4計画について、旧実装の`perform`をFakeBatchで1バッチだけ実行し、
削除1件が読戻し済み、追加2件が未実行の状態を保存しています。既存ルールと追加予定ルールに
送信者ORと連続subjectスペースを含め、旧解釈と新解釈の取り違えを検知できるようにしています。

再生成はローカルGit履歴がある環境で、リポジトリルートから実行します。

```sh
python3 tests/fixtures/generate_condition_equivalence_legacy.py
```

ネットワーク・SDK・認証情報は不要です。旧実装のソースは`git show`で一時ディレクトリへ読み、
時計を固定します。可搬な`/synthetic-legacy/master.xml`へ保存先を置き換えた後、
**旧実装自身のhash関数**でapproval digestを確定してfixtureを凍結します。
XML構築とインメモリ通信の補助に既存のテストhelperを使用しますが、計画・条件比較・payload・
進捗は旧実装が生成します。FakeClientの新規正規化は通さず、旧`to_api`の出力を投入します。

恒常テストはJSONだけを読みます。v3/v4それぞれを`apply`とpending `run`で再開し、
ファイル読み取り先だけを一時Masterへ置き換えます。fixtureのplan・digestは書き換えず、
旧payload一致、既存ID維持、削除再送なし、旧journalイベント保持、新方式の補助情報なしを確認します。
再生成はshallow checkoutのCI要件ではありません。

## nativeフィールド保持前のv5計画

`native-fields-v5.json`は、PR #16マージcommit
`7331e42eb49ec5ea7a9d09cd34e77e4576c1573d`の実モジュールで生成した合成データです。
未選択のv5と、通常グループだけを選択したv5について、最初の削除バッチを読戻し確認した
途中journalを固定しています。残す既存ルールと未送信の追加payloadにfrom OR・subject中括弧を含め、
新compilerを誤用すると再開が失敗する入力にしています。選択済みの例ではAction/Delete変更を
見送り、既存のDeleteルールを残します。

```sh
python3 tests/fixtures/generate_native_fields_v5.py
```

上と同様、ローカルの旧Gitソースと固定時計を使い、実装を再現するモデルは作りません。
可搬な`/synthetic-v5/master.xml`への変更後、旧モジュールのhash関数で計画digestを確定して凍結します。
再開テストではplan/digestを書き換えず、ファイル参照先だけを一時XMLへ置き換えます。
v5 × 未選択/選択済み × apply/runの4経路でcompiler=legacy・normalizer=query-v1を確認し、
旧payload・既存ID・旧イベント・見送りグループを維持します。compilation監査は後付けしません。
旧未選択planを現在の選択関数へ渡した結果も、旧モジュールが生成した選択済みplanと完全一致させます。
