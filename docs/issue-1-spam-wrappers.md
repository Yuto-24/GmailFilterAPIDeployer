# Issue #1：限定したspam条件の外側括弧を揃える仕様

**状態：同一PR #18への追加実装・合成データでの検証。実Gmailの配備・分類挙動は未検証。** 利用者の「Masterに忠実・不要な変更を避ける・不明な意味を勝手に同等扱いしない」という実装委任と、同じPRで進める指示に基づく。先行の[nativeフィールド保持](issue-1-native-fields-proposal.md)はそのまま使用し、正規化を`query-v2`、新規計画をv7へ進める。

## 対象と根拠

query全文から、その全体を包む丸括弧を候補として除いた結果が、次の限定形へ全文一致する場合だけ適用する。

```text
in:spam {[A-Za-z0-9]+(?: [A-Za-z0-9]+)*}
```

これは受理形を示す正規表現であり、波括弧は実際の文字である。`in:spam`は小文字、区切りはASCIIスペース1文字、中括弧内は1語以上のASCII英数字に限る。独立した大文字の`AND`・`OR`・`AROUND`・`NOT`は演算子として解釈され得るため除外する。認識後は外側丸括弧を**1組**とし、語そのもの・大小文字・順序・重複・中括弧を保つ。actionに依存しないcriteria正規化なので、同じqueryを使う通常ラベルのルールにも適用する。actionの違いは保持する。

- [Gmail公式検索演算子](https://support.google.com/mail/answer/7190?hl=en)は、丸括弧で検索語をまとめること、中括弧でいずれかの語に一致させることを説明している。
- [Gmail API Criteria](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters#Criteria)は、queryがGmail検索欄と同じ構文を使うと説明している。
- **本実装の推論**は、このグループ化の説明を上記の閉じた形に限定して適用することである。Googleがこの式の全括弧数の組合せを個別に保証したという主張ではない。合成の比較・送信・再開を検証し、実メールの分類を確認したとは扱わない。

引用、エスケープ、否定、内部丸括弧、別の演算子や条件との混在、日本語、ワイルドカード、タブ・全角空白・連続スペース、括弧不均衡は対象外である。`in:spam(phishing)`など隣接表記も変換しない。未知式の一部分だけを置換せず、query全体を維持する。`negatedQuery`は変更しない。query-v1で既に採用した単純送信者ORとnative subjectの連続ASCIIスペース規則は継続する。

## 比較例と保持する契約

同一actionの場合の比較を示す。XML compilerの`hasTheWord`への括弧1組追加は変更しない。その後の正規化がXML/API双方を同じ形にする。

| XML hasTheWord | API query | v6追加/削除 | v7追加/削除 | 正規化一致の増分 |
| --- | --- | --- | --- | --- |
| `in:spam {phishing}` | `(in:spam {phishing})` | 0 / 0 | 0 / 0 | 0 |
| `(in:spam {phishing})` | `(in:spam {phishing})` | 1 / 1 | 0 / 0 | 1 |
| `in:spam {phishing}` | `((in:spam {phishing}))` | 1 / 1 | 0 / 0 | 1 |
| `(in:spam {phishing})` | `(in:spam {other})` | 1 / 1 | 1 / 1 | 0 |
| `in:spam(phishing)` | `(in:spam {phishing})` | 1 / 1 | 1 / 1 | 0 |

最初の行は以前から一致する例であり、無条件にXML側の追加括弧を除く案ではこの一致を壊してしまう。今回の方式では既存一致を保ち、丸括弧が複数組の限定例も変更不要にする。意味不明な表現を推測して一致扱いしない。Issue #1の任意の検索式やすべての表現差を解消したとは扱わない。

`normalization_matched`は引き続き同じcompilerの下で正規化前後を比べたCounter一致数の増分である。action差を無視せず、重複IDをまとめず、既存一致の分を二重計上しない。複数IDへ一致する削除を飛ばす保護、Masterの正規化後重複検出、同等ルールのID保持も維持する。計画の`normalization_audit`へ原criteria・正規化後・`query_spam_outer_parentheses`の理由を残す。監査は承認digestの対象とし、API payloadには混ぜない。

Action/Delete・Trashの制約、グループ承認→最終承認、見送りグループ保持、削除先行、通常50操作ごとの全件照合、送信不確定時の復旧は既存契約を使用する。変更不要なら承認・変更APIは発生しない。

## 計画と途中再開

| 計画schema | compilation_version | normalization_version |
| --- | --- | --- |
| v3/v4 | 欄なし、legacyで解釈 | 欄なし、legacyで解釈 |
| v5 | 欄なし、legacyで解釈 | query-v1 |
| v6 | xml-fields-v1 | query-v1 |
| v7（新規） | xml-fields-v1 | query-v2 |

この5つのschemaと正確な組合せだけを受理する。v6へのquery-v2注入、v7のversion欠落や旧方式混入などは`apply`とpending `run`のXML処理・OAuth・承認より前に停止する。グループ選択でschemaと解釈方式を変えず、旧計画を移行・再署名しない。旧journalは承認時の方式で読み、通常の進捗だけ更新する。送信payload・API読取り・checkpoint・最後の照合まで計画に固定した方式を通す。

`validate`・新規`plan`・独立`verify`はv7の方式を使用する。旧pendingを再開・照合するにはその`run`または`apply`を使用する。新schemaは旧バイナリに新計画を受け入れさせないための境界でもある。

## 検証

[検証記録](reports/2026-10-27-issue-1-spam-wrappers.md)に結果と未実施範囲を残す。恒常テストは限定grammarの成功・拒否、API/XML両向き監査、Counter、ID、承認不要、通常ルールとTrashの実差分、部分再開、見送り、外部変更、未知version拒否を確認する。

旧v6の実モジュール`76e5407`から、二重括弧のTrashを既存のまま残す・新規作成する・変更見送りで残す3ケースの途中journalを生成して凍結する。各々を`apply`/`run`で再開し、旧digest・旧payload・既存ID・確定済みイベントをそのまま確認する。詳細は[fixture説明](../tests/fixtures/README.md)を参照。v3/v4/v5も以前の凍結fixtureによる再開検証を継続する。
