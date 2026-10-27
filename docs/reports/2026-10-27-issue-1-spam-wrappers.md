# Issue #1 限定spam括弧正規化の検証記録

PR #18のnativeフィールド保持実装`76e5407`に追加したquery-v2/v7の記録。
実装担当Astraと別担当による独立検証を分ける。入力・アカウント・API通信はすべて合成で、
実Gmail・利用者のMaster・認証情報を使用していない。

## 実装と恒常テスト

query-v2はquery-v1を引き継ぎ、全文が限定した`in:spam {ASCII英数字の語}`の形である場合だけ、
外側丸括弧を1組へ揃える。XML compilerはxml-fields-v1のまま。新規計画はv7、旧v6はquery-v1固定で再開する。
詳細な対象・対象外・公式資料からの限定推論は[仕様](../issue-1-spam-wrappers.md)を参照。

- `python3 -m unittest discover -s tests`：214件、成功（SDK未導入により11件skip）。
- `python3 -m unittest discover -s tests -p 'test_spam_wrappers.py'`：追加10件、全件成功。
- `python3 -m py_compile gmail_filter_deployer.py tests/test_spam_wrappers.py tests/fixtures/generate_spam_wrappers_v6.py`：成功。
- `git diff --check`：成功。

追加テストは複数の入力・経路をsubTestで検証する。限定grammar、括弧数0〜4、冪等性、入力非破壊、
語順・大小文字・重複保持、未知構文・予約演算子・negatedQuery保持、action差を確認した。
XML裸/API単一、XML単一/API単一、XML裸/API二重の3例はすべて変更0、元ID/TRASH actionを保持し、
承認・filter create/delete・label createを禁止mockにしてAPI変更がないことを確認した。
正規化一致増分は順に0/1/1で、XML/APIの監査原criteria・理由も確認した。

重複IDのCounterと削除skip、Masterの正規化後重複によるOAuth前停止、v7の単一括弧create payload、
削除後の部分再開・再作成なし・表記変化の読戻し、語の実変更による停止、見送りTrashの元ID/原条件保持、
監査改変と未知v7解釈のXML/OAuth前停止を確認した。既存v1のTrash非同等テストはmodeを明示して保持した。

## 凍結旧v6の再開

実際の`76e5407bfa86d6a89cad947cbf665a695f272a78`モジュールから3ケースを生成した。
二重括弧Trashの保持、新規作成、変更見送りを、削除確認済み・追加未送信の状態で凍結した。
各ケースを`apply`とpending `run`で再開する6経路を恒常テストに含めた。

承認digestをそのまま検証し、ファイル参照先だけを一時XMLへ写像した。全経路がxml-fields-v1/query-v1を使い、
旧payload（Trashの二重括弧を含む）・残存ID・旧journalイベント・見送り対象を維持し、削除を再送しなかった。
完了後の再実行でもcreateは増えなかった。v3/v4/v5の従来の凍結fixtureも全件テストで継続した。
生成方式は[fixture説明](../../tests/fixtures/README.md)に記載。

## 独立担当から受領した検証

実装担当とは別の担当者が、実モジュール`76e5407`との比較で1,400 criteriaケースを調べた。
legacy/query-v1の出力が旧実装と一致し、query-v2は限定spam式の釣り合った外側括弧だけを1組へ揃えること、
未知式・negatedQuery・未知の入れ子キーを保持し、入力非破壊・冪等であることを確認した。
v6 fixtureの独立再生成では3ケースのJSON構造が完全一致した。

SDKを使った独立の3ケースrun probeでも、前記0/1/1の一致増分、変更0、承認や変更APIの呼出しなし、
元ID/TRASH action保持を確認した。これらは合成データによる検証であり、実Gmail分類の証拠ではない。

独立担当のSDK入りネットワーク無効コンテナでは全214件が成功（skipなし、16.219秒）。
通常のDocker buildも成功し、imageは
`sha256:8ce1c6163a29ed66e25427d634c067efdfcb65a757f75e393ab45ead0bcba03b`。
このイメージを認証情報なし・ネットワーク無効で起動し、`--help`の正常終了も確認した。

version組合せ140通りでは、規定の5通りを受理し、残る135通りを拒否した。
拒否対象は各々pending `run`と`apply`の計270経路で、XML処理・OAuth・API・承認を呼ぶ前に停止した。
実際の旧`76e5407`バイナリでも、選択前・選択後を模したv7を`run`/`apply`の4経路すべてで同様に拒否した。

旧モジュールとのAST比較では、XML変換本体、API payload生成、ID snapshot、canonical境界、
配備executorが不変で、既存関数の変更は意図したversion解釈・正規化・説明表示の7関数だけだった。
新規helperを含む変更runtime関数8個すべてにdocstringがあることも確認した。

## 残る検証境界

PR更新後のCIとCodeRabbitレビューは別途記録する。先行commitの承認だけで今回の挙動変更を承認済みとは扱わない。
実Gmailへの配備、実メール分類、実Masterでの差分数は未実施である。
