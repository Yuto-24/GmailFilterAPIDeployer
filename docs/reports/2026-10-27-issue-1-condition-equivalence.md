# Issue #1 限定正規化初版の検証記録

2026-10-27時点。合意したquery内の単純送信者OR・全体括弧と、native subjectの連続ASCIIスペースを実装した。新計画はv5/query-v1、既存v3/v4はlegacyで再開する。from/subjectとqueryのフィールド跨ぎや、Trash条件の括弧差は解消していないため、Issue #1全体の解決とは扱わない。

## オフラインの検証結果

| 検証 | 結果 |
| --- | --- |
| Hostのunittest全体 | 189件、14.051秒、成功。SDK未導入により11件skip。 |
| SDK入りDockerのunittest全体 | `--network none`、189件、15.258秒、全件成功・skipなし。実装担当と別のレビュー担当が実行。 |
| 旧fixtureの独立再生成 | 旧commitの実モジュールから生成した結果と、保存JSONの構造が完全一致。 |
| 旧バイナリへの新計画入力 | 旧commitの実`planned_states`は、グループ未選択/選択済みのv5を両方とも`DeploymentError`で拒否。 |
| Dockerfileと配布イメージ | 通常build成功、作成イメージの`--help`を`--network none`で実行して成功。 |

SDKテストは既存`gmail-filter-deployer:issue3`イメージに現ソース・tests・scriptsを読み取り専用でmountした。認証情報・実Master・`local/`はmountしていない。ネットワークは無効で、実Gmail APIへのアクセスはない。

配布イメージは`gmail-filter-deployer:issue1-scoped`、IDは`sha256:2ed975a79754346e541ff1a71886fdba850c29dfaf2c05e5facfc1e4e67edc15`。最初のネットワーク無効buildはpip依存取得ができず失敗したため、通常buildで確認した。これは依存取得を伴うbuildの結果であり、上記のネットワーク無効テストとは区別する。

## 追加した検証

`tests/test_condition_equivalence.py`の16テストで、許可する全文一致・入力非変更・冪等性、引用・未知構文・否定・Unicode空白・不整合括弧の保持、actionやフィールド間の真の差分を確認した。XML正規化後の重複は認証前に停止する。

API側だけ表記が異なる場合も同じ比較に揃い、原文・変換理由はfilter本体とAPI payloadから分離される。表示件数はCounterの一致数増分であり、既存の一致枠を埋めた重複や別actionを追加の一致として数えない。複数IDの削除は既存の一意性確認でスキップされる。

新規`run`の表記差だけのケースでは、承認・filter作成/削除・label作成を呼出禁止にしたまま正常完了し、元IDと監査付き計画が保持される。残る対象外の差分には「同等性未確認」が出る。選択済みv5の事前検証→送信→読戻しが一致し、後から許可範囲内の表記が変わっても再送せず、subjectの実変更は不一致として停止する。監査改変はdigest検証でAPI取得前に拒否される。未知schema/versionは`run`/`apply`ともXMLコンパイル・認証・承認前に拒否される。

旧fixtureの4経路（v3/v4 × apply/pending run）では、最初のXMLコンパイルと全API取得がlegacyになり、凍結済みのapproval digest・旧payload・既存ID・旧journalイベントを維持して完了する。済んだ削除は再送しない。fixtureの生成元と再現方法は[fixture説明](../../tests/fixtures/README.md)に記載した。

既存テストではAction/Delete・Trashの承認と制約、削除一意性、50操作ごとの照合、途中終了、応答喪失・再開、SDKのmultipart/一送信契約、Issue #3の時間・試行数表示を回帰確認した。

## 検証していない範囲

実Gmailへの配備・検索・新着メールの振分け、利用者のMasterでの差分削減率、実配備時間は測定していない。今回の一致は合意した限定規則とAPI表現の契約に基づき、任意のGmail検索式の同等性を保証しない。マージ・本番運用の完了をこの記録から推定しない。
