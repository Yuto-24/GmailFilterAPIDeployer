# Issue #1：XMLのfrom・subjectをnativeフィールドに保持する仕様

**状態：実装・オフライン検証済み。実Gmailでは未検証。** 利用者は「Masterに忠実・不要な変更を避ける・不明な意味を勝手に同等と扱わない」という方針で実装を委任した。以下はその範囲で採用した技術仕様である。実装はAstra、独立レビューは別担当で行う。

## 採用範囲

新規計画のXML変換で、`from`と`subject`をそれぞれAPIの同名フィールドへ保持する。従来の「fromのOR式をqueryへ移す」「subjectの中括弧式をqueryへ移す」処理をやめ、元の2例でツール自身が作るフィールド差を解消する。**異なるフィールドの条件を同等と判定するものではない。**

| 対象 | 今回の仕様 |
| --- | --- |
| XMLのfrom・subject | 現在受理する入力に限り、nativeフィールドへ保持する。 |
| XMLのhasTheWord | 現在どおり外側丸括弧を追加してqueryへ置く。変更しない。 |
| query内の単純送信者OR・subjectのASCIIスペース | PR #16の`query-v1`正規化を継続する。 |
| フィールド跨ぎの同等判定・Trashの括弧正規化 | 採用しない。残る差分は承認対象として表示する。 |

`hasTheWord`の追加括弧除去は今回から外す。XML原文が`in:spam {phishing}`でAPIが`(in:spam {phishing})`なら、除去によって現在の一致が不一致になるためである。今回の案ではこの挙動を変えない。

## 根拠：確認した事実と推論

- **公式の事実**：旧Email Settings APIはXML/GDATAを使い、移行表は`from → criteria.from`、`subject → criteria.subject`、`hasTheWord → criteria.query`を明示する。[公式移行表](https://developers.google.com/workspace/gmail/api/guides/migrate-from-email-settings#manage_filters)
- **公式の事実**：Workspaceのフィルタ例には、UIのFrom欄へ複数アドレスを大文字ORで指定する例がある。[公式From OR例](https://support.google.com/a/users/answer/9300019?hl=en)
- **公式の事実**：Gmailのフィルタexportは編集可能なXMLである。ただし、この説明はXML各値とREST APIの全構文の対応を保証していない。[公式export説明](https://support.google.com/mail/answer/6579?hl=en)
- **公式の事実**：Criteria仕様はfromを送信者名・アドレス、subjectを件名内のフレーズとして説明し、queryにGmail検索構文を認める。native欄のOR・中括弧をqueryへ移す同等性は明記していない。[Criteria仕様](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters#Criteria)
- **本案の推論**：UI exportの同名プロパティにも公式移行表の対応を適用し、フィールドを保持する方が、独自にqueryへ移すより根拠が直接的である。ただし、任意の構文の意味や新着メールの分類を実証したものではない。

## 合成例の結果と反例

以下はmain `7331e42`の現行変換と保持案を、実装担当とは別の担当者が合成入力で再現・確認した結果である。**実Gmail・実Masterの測定ではない。** 各行のactionは同一とする。

| XML原文とAPI側の表現 | 従来の追加/削除 | 今回の追加/削除 |
| --- | --- | --- |
| XML from=`a@example.invalid OR b@example.invalid`、API native fromが同文 | 1 / 1 | 0 / 0 |
| XML subject=`{スカウトが届きました}`、API native subjectが同文 | 1 / 1 | 0 / 0 |
| XML hasTheWord=`(in:spam {phishing})`、API query=`(in:spam {phishing})` | 1 / 1 | 1 / 1を維持 |
| XML from=`a@example.invalid OR b@example.invalid`、API query=`{from:a@example.invalid from:b@example.invalid}` | 0 / 0 | 1 / 1へ増える |
| XML subject=`{スカウトが届きました}`、API query=`subject:{スカウトが届きました}` | 0 / 0 | 1 / 1へ増える |

後ろの2行は、旧compiler由来のqueryで既に一致している場合の反例である。本案は差分が必ず減る変更ではなく、差分が増えた場合も自動で同等とみなさない。元の2例の解消とIssue #1全体の解決を区別する。

## 入力と変換の契約

1. 旧compilerと同じ受理・拒否条件を先に検証する。現在拒否する複雑なfrom、subjectのOR式などを新しく受理しない。action・空条件・XML重複等の検証も維持する。旧方式でコンパイル後重複として拒否したMasterも、新方式で別フィールドになったことを理由に受理しない。
2. 検証後のfrom・subjectだけをnative欄へ保持する。既存の前後空白処理と`query-v1`のsubjectスペース整理は維持し、引用・中括弧・語順を独自に解釈しない。
3. `hasTheWord`は従来の括弧付きquery、`doesNotHaveTheWord`は従来のnegatedQueryとする。複数criteriaをANDとして扱う契約を維持する。[公式フィルタ管理](https://developers.google.com/workspace/gmail/api/guides/filter_settings#match_criteria)
4. `compile_xml`から`build_template`へコンパイル方式を明示的に渡す。コンパイル方式と正規化方式を分離し、API読取り・payloadの正規化は計画に固定された方式を使用する。

## 計画と再開の互換性

新規計画は`schema: gmail-filters-deploy-plan/v6`、`compilation_version: xml-fields-v1`、`normalization_version: query-v1`とする。両versionと監査情報はapproval digestの対象にし、グループ選択後もv6を維持する。

| 保存済み計画 | XMLコンパイル | 正規化 |
| --- | --- | --- |
| v3/v4、両versionなし | 従来のquery移動・括弧追加 | legacy |
| v5、normalization_version=query-v1、compilation_versionなし | 従来のquery移動・括弧追加 | query-v1 |
| v6、compilation_version=xml-fields-v1、normalization_version=query-v1 | from・subject保持、hasTheWordは従来どおり | query-v1 |

上表以外の組合せ、v6のversion欠落、v5へのcompilation_version追加等は、`run`/`apply`で最初のXMLコンパイル・OAuthより前に停止する。旧計画の自動移行・digestの再署名・新方式での旧journal再解釈は行わない。旧方式でも通常の再開進捗は更新する。独立した`validate`/`plan`/`verify`は新方式を明示使用し、旧pendingの再開はその計画の方式で最終照合まで通す。

## 監査・件数・検証契約

コンパイル変更による旧v5 targetと新targetのcriteria差を、**「正規化で同等」と記録しない**。XML項番号・旧方式のcriteria・新方式のcriteria・コンパイル方式をローカル計画の`compilation_audit`へ残す。原文を公開レポートへ転記せず、監査を意味比較用filterやAPI payloadへ混ぜない。方式の説明は`-v`の詳細表示と計画に置く。

`normalization_matched`は、**同一compilerの出力**に対するquery-v1適用前後のCounter一致数増分だけとする。コンパイル方式変更に起因する一致数・追加/削除の変化は`compilation_comparison`へ別記し、未確認の同等ペアや「安全に削減した変更件数」へ読み替えない。これは計画作成時のMaster全体対liveの比較であり、グループ選択後の実行予定件数とは区別する。

実装の検証契約は次のとおり。結果は[検証記録](reports/2026-10-27-issue-1-native-fields.md)に記載する。

- 上表の2例0/0、Trash例1/1、旧queryで一致済みの反例1/1を固定する。複数criteria、query併存、否定、action差、件数重複、拒否入力も確認する。
- v5の未選択・選択済み計画と途中journalを実際の旧モジュールから生成して凍結する。v3/v4と併せ、digest・payload・既存ID・承認範囲を保ち、重複送信なく再開することを確認する。
- 新v6のplan→選択→事前検証→送信→読戻しと、旧バイナリによるv6拒否、未知組合せの入口停止、監査改変検出を確認する。
- Action/Delete・Trash制約、グループ承認と最終承認、削除先行、原則50操作ごとの照合、応答喪失・途中復旧を維持する。合成fixtureとネットワーク無効のSDK環境で検証し、実Gmailの配備や分類確認と区別する。

**今回の実装は、上記のnativeフィールド保持と計画互換性の範囲である。** hasTheWordの括弧除去・フィールド跨ぎ同等判定・実Gmail操作は含めない。
