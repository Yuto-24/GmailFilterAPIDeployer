# Issue #1：条件式の表現差を扱う仕様案

**状態：PR #16で合意・実装・マージした初版正規化の記録。Trash追加案は保留。** PR #15マージ後の`f2240c9`を調査し、利用者が「推奨範囲で実装を進める」と承認した。実Gmail・認証情報・利用者のMaster XMLにはアクセスせず、公式資料と合成入力だけを使用した。初版の検証範囲は[検証記録](reports/2026-10-27-issue-1-condition-equivalence.md)に記載する。後続のXML変換・v6計画は[nativeフィールド保持の仕様](issue-1-native-fields-proposal.md)を参照し、本書のv5計画は元の方式で再開する。

## 推奨する進め方と解消範囲

初版は、**同じフィールド内で根拠がある変換だけを採用し、残る差分には「同等性未確認」と表示する**案を推奨する。対象は次の二つとする。

- `query`全体が単純な送信者ORである場合の、`from:a OR from:b`と`{from:a from:b}`の統一。その式だけを包む外側丸括弧も対象にする。
- `subject`フィールド内の連続したASCIIスペースを一つにする。`subject`から`query`へは移動しない。

**元のIssueで挙がった「fromフィールドとqueryの差」「subjectフィールドとqueryの差」は、この案では解消しない。Trashの外側括弧も初版では残す。** それらを同等とみなす根拠が足りないためである。初版だけで元の3例が解決した、あるいはIssue #1全体をCloseできるとは扱わない。解消できるのは、下表の別の限定例である。

| 合成例 | 旧方式の追加/削除 | 初版の追加/削除 |
| --- | --- | --- |
| `query`の`from:a@example.invalid OR from:b@example.invalid`対`{from:a@example.invalid from:b@example.invalid}` | 1 / 1 | 0 / 0 |
| `query`の`(({from:a@example.invalid from:b@example.invalid}))`対`{from:a@example.invalid from:b@example.invalid}` | 1 / 1 | 0 / 0 |
| `subject`の`meeting  reminder`対`meeting reminder` | 1 / 1 | 0 / 0 |
| APIの`from: "a@example.invalid OR b@example.invalid"`対XML由来の`query: "{from:a@example.invalid from:b@example.invalid}"` | 1 / 1 | 1 / 1を維持 |
| APIの`subject: "{スカウトが届きました}"`対XML由来の`query: "subject:{スカウトが届きました}"` | 1 / 1 | 1 / 1を維持 |
| Trashの`query: "((in:spam {phishing}))"`対`query: "(in:spam {phishing})"` | 1 / 1 | 1 / 1を維持 |

旧方式は仕様検討時に`build_template`→`canonical_template`と`live_as_template`を通して`delta`で確認した結果。初版は合成入力の回帰テストで確認した値であり、各例のactionは同一とした。件数削減率や配備時間への効果は実アカウントで測っていない。

## 公式資料が示すこと・示さないこと

Googleの検索演算子説明には、ORと中括弧の両方でいずれかの条件を満たす検索の例があり、丸括弧は条件のグループ化に使うとある。これを根拠に、**同じquery内の、式全体を認識できる送信者OR**に限定する。全体を包む括弧の除去は、このグループ化の説明からの限定的な推論であり、任意のGmail式を変形できるという保証には広げない。[Gmail検索演算子](https://support.google.com/mail/answer/7190?hl=en)

Criteria仕様は、`subject`の前後空白を除去し、連続スペースをまとめることを明記している。このため、同じsubjectフィールドのASCIIスペースだけを対象にできる。一方、`from`は送信者名またはアドレス、`subject`は件名のフレーズとして説明されている。フィールド内部のORや中括弧をqueryへ移してよいという記述は確認できなかった。なお`to`はto/cc/bccの各ヘッダーを対象にするため、一般的な`query`の`to:`への移動も採用しない。[Criteria仕様](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters#Criteria)

複数のcriteriaを指定したフィルタでは、すべてを満たす必要がある。新仕様はこのフィールド間の結合を変えない。フィルタは個別メッセージに適用される。[フィルタ管理](https://developers.google.com/workspace/gmail/api/guides/filter_settings)

有限個のメールで検索結果が同じでも、将来の全メールでの同等性は証明できない。また、メッセージ検索APIとGmail UIには別名展開やスレッド検索の差があるため、UIの検索件数一致だけをフィルタの同等性の根拠にしない。[API検索とUIの相違](https://developers.google.com/workspace/gmail/api/guides/filtering#differences_from_the_gmail_ui)

## 初版で許可する変換の契約

### 1. query内の単純な送信者OR

フィールドを移動せず、query文字列の**全体**が次のいずれかに一致した場合だけ、`{from:アドレス from:アドレス ...}`へ揃える。項は2個以上とし、順序・重複・アドレスの大文字小文字を保つ。

```text
SenderOr := FromAtom (" OR " FromAtom)+
          | "{" FromAtom (" " FromAtom)+ "}"
Query    := SenderOr | "(" Query ")"
FromAtom := "from:" Address
```

スペースはASCIIの1文字、ORは大文字に限定する。構文外の空白整理は新しく追加しない。既存の文字列両端`strip()`は現行仕様として維持する。AddressはRFCメールアドレス全体の解析を目指さず、初版では次のASCII部分集合だけを許可する。

```regex
[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,63}
```

許可外のアドレスは不正とは判定せず、正規化せずに保持する。一般的な検索式パーサは作らず、この文法への全文一致を確認する。`(a) (b)`、`from:(a OR b)`、末尾に`subject:x`を足した式、OR項の並べ替えは対象外。`strip('()')`や全括弧の削除は禁止する。

### 2. subject内のASCIIスペース

既存の前後空白処理の後、`subject`値の連続したU+0020だけを一つへ置換する。タブ・改行・全角空白・NBSP・Unicode正規化・大小文字変換は新しく行わない。日本語の文字や引用符はそのまま保持し、query内の`subject:`や引用句には適用しない。たとえば`subject: "日本語  通知"`と`subject: "日本語 通知"`は対象になるが、`query: '"日本語  通知"'`は対象外である。

### 3. 対象外の条件と真の差分

`from`/`to`/`subject`とqueryの間の移動、`negatedQuery`とquery内の`-`の交換、AND/ORの並べ替え、De Morgan変換、引用句の再解釈、日本語の分かち書きは行わない。`query`に未知の構文があっても削除したり変換失敗で空条件にしたりせず、そのフィールドの値を保持する。対象外のqueryと、変換可能なsubjectが同じcriteriaにある場合は、subjectだけを独立に処理できる。

条件が同等でもactionが異なるルールは統合しない。複数ルールが同じ内部表現になっても勝手に1件へ減らさず、既存Counterの件数を維持する。Master内の正規化後重複は、既存のXMLコンパイル後重複検知と同様に変更API前に停止する。Gmail側の複数IDが同じルールへ対応する場合も、既存の削除一意性検証・スキップを維持する。

## Trashの外側括弧を扱う追加案

追加候補は、`in:spam {phishing}`のように**認識した形を包む外側括弧だけ**を揃えることである。採用するなら、本文を`in:spam`、ASCIIスペース1文字、中括弧内のASCII英数字の語という形に限定し、語順・語そのもの・中括弧を変えない。引用・エスケープ・否定・内部丸括弧・日本語・`in:spam(...)`のような隣接表記は対象外にする。

この案を追加すれば元のTrash合成例は期待0/0になるが、任意のTrash条件を解消するわけではない。公式の括弧説明をフィルタのこの形へ適用する推論であり、該当式そのものの同等性をGoogleが列挙した資料は確認できていない。**推奨初版には含めず、採用は別途明示的に合意する。** Action/DeleteやTrashであっても、採用した同等性だけを判定し、それ以外のaction制約を緩めない。

## 表示と承認

通常表示は現在の6フェーズとグループ別差分を維持する。採用済みの変換で差分がなくなった件数は、`表記を統一して一致したルール: N件（変更不要）`として簡潔に示す。重複がある場合はCounterの一致数で数え、IDの異なるルールを勝手に対応付けない。

残る差分には、該当する場合に`同等性未確認: from/subjectとqueryの表現差は自動判定対象外`のような注記を付ける。**未確認だから同等、と推測した削除/追加ペアは作らない。** 追加と削除の現在の内容をそれぞれ表示し、差分として承認対象に残す。詳細表示とローカル計画には、変換前・変換後・理由を残す。

Action/Delete・Trashのグループ承認→最終承認、承認前の読取り専用検証、未承認グループの保持、削除先行、50操作ごとの照合を変更しない。同等と判定したルールへの変更APIは送らない。実際に残る変更については従来どおり承認を得る。

## 実装時の境界と旧計画の扱い

### 二つの正規化方式だけを持つ

既存方式を`legacy`、合意した初版を`query-v1`とする。汎用的なversion管理機構や利用者向け切替CLIは作らない。正規化方式は実行中の計画に固定し、下位関数へ明示的に渡す。グローバルな既定値の途中変更や、hash計算時だけの正規化は避ける。

新規計画には`schema: gmail-filters-deploy-plan/v5`と`normalization_version: query-v1`を記録する。v5では`selected_groups`未設定が従来のv3相当、設定済みが従来のv4相当であり、グループ選択してもv5を維持する。新schemaは旧バイナリに新計画を拒否させるためのもので、journal/checkpoint形式は増やさない。

| 入力計画 | 正規化方式・扱い |
| --- | --- |
| 既存のv3/v4、normalization_versionなし | `legacy`で再開。XML再コンパイル・API読戻し・payloadも従来規則を使う。digestを再署名せず、journalに新方式の診断を後付けしない。通常の進捗更新は行う。 |
| v5、normalization_versionがquery-v1 | 初版規則を全経路へ適用。version情報もapproval_digestの対象にする。 |
| 不明なschema/version、v5のversion欠落、v3/v4に新versionを付加 | 承認・変更API前に停止。旧計画を自動移行しない。 |

`run`ではロック取得後、**最初のXMLコンパイルとAPI取得より先に**pending計画の方式を選ぶ。従来は第4フェーズでpendingを読んでいたが、この入口の位置を変更した。新規ならquery-v1、pendingがあればその方式を最終結果比較まで使う。`apply`も計画読込み直後に選び、`plan`/`validate`/`verify`など新規の独立操作はquery-v1を明示使用する。独立`verify`は新規基準の検証であり、旧pendingの再開照合を代行しない。

### 関数の責務

実装の核は純粋関数`normalize_criteria(criteria, *, normalization)`とし、正規化済みcriteriaと変更理由を返す。入力を変更せず、未知構文を保持し、同じ方式で何回処理しても結果が変わらないことを契約にする。`canonical_template`はこの関数と既存action整理をまとめる。`json_hash`や`counter`には意味解釈を入れない。

| 境界 | 現行の関数と実装時の変更 |
| --- | --- |
| XML・APIからの入口 | `compile_xml`、`live_as_template`、`api_fetch`、`filters_with_known_labels`、`fetch_for_deploy`へ方式を渡し、同じ`canonical_template`へ到達させる。現在の`build_template`のXML→API変換はlegacyを含め維持し、その後の限定正規化として追加する。 |
| 計画作成・選択 | `make_plan`、`select_plan_groups`で新規方式を固定。`deletion_preview`、`classify_deletion_conflicts`へ渡すルールも同じ方式の値にする。 |
| 再開・送信 | `planned_states`で計画方式によるXML再コンパイルとbaseline検証。`validate_before_final_approval`、`perform`、`perform_batched`、`to_api`まで固定した方式を使用。途中の再照合・再試行のAPI読取りにも伝える。 |
| 比較・保存 | `filter_snapshot`、`batch_progress`、`prefix_counts`、`delta`、`counter`、`tokens_digest`には入口で揃えた値を渡す。直接dict比較、ID照合、snapshot/journalも同じ値を使う。 |
| 最終照合・診断 | `verify_partial`、`cmd_verify`、`cmd_run`末尾のMaster再コンパイルも同じ方式。正規化済み値のほか、変更前のcriteriaと固定の変換理由をローカル診断へ保存する。 |

変換履歴は`criteria`/`action`の外へ置き、意味比較するテンプレートには混ぜない。新計画には変換されたXML項番号/APIフィルタID・変換前criteria・変換後criteria・理由を診断情報として保持し、approval_digestでは計画の一部として検証する。API読戻しの行にも必要な原文を補助情報として残し、`filter_snapshot`のID→正規化済みfilter比較やAPI payloadには含めない。旧計画・旧journalにはこの補助情報を後付けしない。秘密を含む可能性があるため、この情報を公開レポートや固定集計キーへ転記しない。

特に`planned_states`は毎回XMLを再コンパイルし、`batch_progress`や削除ID選択はテンプレートのdictを直接比較する。このため`delta`だけの修正では整合しない。新計画・旧計画それぞれで、入口から最終照合まで同じ方式を使うことが実装レビューの重点になる。

## 検証契約

以下を合成fixtureとネットワーク無効のSDK環境で検証した。結果と旧実装を使った互換性検証の根拠は[検証記録](reports/2026-10-27-issue-1-condition-equivalence.md)を参照する。

- **許可範囲**：query OR/中括弧/全体括弧、subjectのASCII連続スペース、冪等性、入力非変更、XMLとAPIの同じ最終表現、payload往復。
- **対象外の保持**：引用中のOR、日本語query、全角空白・NBSP・タブ、negatedQuery、否定、AND混在、内部グループ、不釣合い括弧、`in:spam(...)`、from/subject/toとqueryの移動をしない。
- **真の差分**：異なるアドレス・単語・actionは消えない。OR項の順序入替えは初版では統一しない。正規化後重複、複数ID、IDなしスキップの扱いを保持する。
- **一貫性**：新方式のplan→preview→グループ選択→最終承認→送信→読戻し→最終結果で判定が一致する。表記だけの差分では変更APIが0件である。正規化外の外部変更を次回照合で検出する。
- **旧計画**：v3/v4と既存journalを合成fixtureとして固定し、legacyでdigest不変・payload不変・同じIDの重複送信なしで再開する。未知versionは変更前に停止する。v5は旧バイナリで拒否されることも確認する。
- **承認・復旧**：Action/Delete/Trashの制約と承認順、50操作境界、末尾、途中終了、応答喪失、rate-limit後の再開、Issue #3の計測表示を既存テストで回帰確認する。

## 合意結果（2026-10-27）

1. **初版の範囲**：query内単純OR・その全体括弧・subjectのASCIIスペースと、未確認差分の表示を実装する。診断表示のみの案は採用しない。
2. **Trashの追加候補**：初版から外す。from/subject跨ぎも保留する。
3. **旧計画との互換性**：v3/v4はlegacyのまま再開し、新計画だけv5/query-v1とする。旧計画の自動移行や承認済みdigestの更新は行わない。

合意した限定初版を実装した。限定初版の完了と、元のIssue例すべての解消は別々に報告する。
