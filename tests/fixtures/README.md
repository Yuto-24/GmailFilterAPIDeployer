# 条件式正規化の旧計画fixture

`condition-equivalence-legacy.json`は、条件式の正規化を実装する前のコミット`f2240c9b662fbc4c510ac3f353540f133b8d1c00`の実モジュールで作成した合成データです。実アカウントのMaster XML・フィルタ・ID・ラベル・journalは一切含みません。

このfixtureには、v3計画とグループ承認済みv4計画のそれぞれについて、旧実装の`perform`をFakeBatchで1バッチだけ実行し、「削除1件が読み戻し確認済み、追加2件が未実行」となった途中状態を保存しています。既存ルールと追加予定ルールには送信者ORと連続した`subject`スペースを含めており、再開時に旧解釈（`legacy`）と新解釈を取り違えた場合に検知できるようにしています。

fixtureの再生成は、ローカルGit履歴がある環境でリポジトリルートから実行します。

```sh
python3 tests/fixtures/generate_condition_equivalence_legacy.py
```

- 再生成にネットワーク接続・Google SDK・認証情報は不要です。
- 生成スクリプトは、旧実装のソースコードを`git show`で一時ディレクトリへ読み出し、時計を固定します。保存先のパスを可搬な`/synthetic-legacy/master.xml`へ置き換えた後、**旧実装自身のhash関数**で`approval_digest`を確定してfixtureを凍結します。
- XML構築とインメモリ通信の補助には既存のテストhelperを使用しますが、計画・条件比較・送信payload・進捗はすべて旧実装が生成します。FakeClientの新規正規化は通さず、旧`to_api`の出力をそのまま投入します。
- 恒常テストは保存済みのJSONだけを読み込みます。v3・v4のそれぞれを`apply`とpending `run`で再開し、ファイルの読み取り先だけを一時Master XMLへ置き換えます。fixtureのplanやdigestは書き換えず、旧payloadとの一致、既存IDの維持、完了済み削除の再送なし、旧journalイベントの保持、および新方式の補助情報が後付けされないことを確認します。
- なお、fixtureの再生成はshallow checkout環境でのCI要件ではありません。

## nativeフィールド保持前のv5計画

`native-fields-v5.json`は、PR #16のマージコミット`7331e42eb49ec5ea7a9d09cd34e77e4576c1573d`の実モジュールで生成した合成データです。グループ未選択のv5計画と、通常グループだけを選択したv5計画について、最初の削除バッチを読み戻し確認した時点の途中journalを固定しています。

残す既存ルールと未送信の追加payloadの両方に`from`のOR式と`subject`の中括弧式を含めており、新コンパイラ（`xml-fields-v1`）を誤って適用すると再開が失敗する入力にしています。グループ選択済みの例では、`Action/Delete`の変更を見送り、既存のDeleteルールを残す設定にしています。

```sh
python3 tests/fixtures/generate_native_fields_v5.py
```

- 上記と同様にローカルの旧Gitソースと固定時計を使用し、旧実装を模倣する別モデルは作りません。可搬な`/synthetic-v5/master.xml`へパスを置き換えた後、旧モジュールのhash関数で計画digestを確定して凍結します。
- 再開テストではplanやdigestを書き換えず、ファイルの参照先だけを一時XMLへ置き換えます。「v5 × 未選択/選択済み × `apply`/`run`」の4経路で`compiler=legacy`・`normalizer=query-v1`が使われることを確認し、旧payload・既存ID・旧イベント・見送りグループが維持されることを検証します。`compilation_audit`は後付けしません。
- また、旧未選択planを現在のグループ選択関数へ渡した結果が、旧モジュールが生成した選択済みplanと完全一致することも確認します。

## 限定spam括弧正規化前のv6計画

`spam-wrappers-v6.json`は、nativeフィールド保持を実装したコミット`76e5407bfa86d6a89cad947cbf665a695f272a78`の実モジュールから生成した合成データです。以下の3例について、削除工程が確認済み・追加工程が未送信の途中journalを固定しています（nativeの`from` OR式・`subject`も含みます）。

1. 二重括弧のTrashルールを既存のまま残す未選択計画
2. Trashの変更を選択して新規作成する計画
3. Trashの変更を見送り、通常ルールだけを配備する計画

```sh
python3 tests/fixtures/generate_spam_wrappers_v6.py
```

- 旧モジュール自身がplan・payload・checkpointを生成し、固定時刻と可搬パス`/synthetic-v6/<scenario>/master.xml`を使ったうえで、旧hash関数でdigestを確定して凍結します。
- 恒常テストでは保存済みJSONと一時XMLのみを使い、planやdigestを変更・再署名しません。「3例 × `apply`/`run`」の6経路で`xml-fields-v1`/`query-v1`を最後まで維持し、新規Trashの二重括弧payload、保持・見送りTrashの元IDと原criteria、旧イベント、および完了済み削除の再送なしを確認します。
- `query-v2`の正規化理由を旧journalへ挿入しないこと、ならびに再開完了後の再実行でもcreateが増えないことを確認します。
