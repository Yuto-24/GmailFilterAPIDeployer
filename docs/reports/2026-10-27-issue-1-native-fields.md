# Issue #1 nativeフィールド保持の検証記録

2026-10-27時点。利用者から委任された「Masterに忠実に反映し、不要な変更を避け、不明な意味を同等と扱わない」方針に沿って、[nativeフィールド保持の仕様](../issue-1-native-fields-proposal.md)を実装した。実装担当はAstra、下記の独立確認は別担当が行った。

XMLのfrom・subjectをAPIの同名フィールドへ保持し、query-v1の限定正規化は維持する。hasTheWordの追加括弧とTrashの括弧差は変更していない。元のnative2例で差分がなくなることと、Issue #1全体の解決は区別する。旧compiler由来のqueryで既に一致していた場合には、native targetとの差分が増える反例も固定した。

## オフラインの結果

| 検証 | 結果 |
| --- | --- |
| 既存テストの回帰 | Hostで189件、14.923秒、成功。SDK未導入の11件はskip。新schema/native subjectに対応する期待値を更新し、旧compilerの直接検証も維持。 |
| 新規テスト込みの全体 | Hostで204件、15.367秒、成功。SDK未導入の11件はskip。 |
| SDK入りDockerの全体 | 独立担当が`--network none`で204件、15.963秒、全件成功・skipなし。 |
| 旧実装との入力比較 | 4,480組合せを独立検証。旧実装と新旧両compilerで受理2,876・拒否1,604が一致。legacyの出力と拒否メッセージも一致し、nativeのactionとフィールド対応を確認。 |
| 旧v5 fixtureの再生成 | `7331e42`の実モジュールから独立再生成し、保存JSONと構造が完全一致。 |
| schema/versionの入口検証 | 96組合せ中、有効なv3/v4/v5/v6の4組を確認。無効92組はrun/applyともXML・OAuth・API・承認の呼出禁止mockを呼ばず終了コード2。 |
| 旧バイナリの新計画拒否 | 実際の`7331e42`へ未選択/選択済みv6をrun/applyで渡し、XML・OAuth・API・承認前に終了コード2。 |
| Docker配布 | 通常build成功。作成イメージの`--help`を`--network none`で実行して成功。 |

SDKテストでは既存SDK入りイメージへ現ソース・tests・scriptsを読み取り専用でmountし、認証情報・実Master・`local/`はmountしていない。実Gmailにはアクセスしていない。配布イメージは`gmail-filter-deployer:issue18-native-fields`、IDは`sha256:a76c6b214ad6a2980a184ad89474a168dc878754ab4f87822f90df9b231f0ade`。依存取得を伴う通常buildと、ネットワーク無効のテスト・起動確認は別の検証である。

## 機能と監査の確認

新規15テストは`tests/test_native_fields.py`に追加した。native from OR・subject中括弧がAPIと一致するケースは承認・作成・削除・ラベル作成を呼出禁止にしても正常完了し、元IDを保つ。独立担当もSDK環境で同じno-opを確認した。旧queryで既に一致する反例、hasTheWordの括弧維持、真の条件・action変更、旧方式と新方式の重複拒否を確認した。

このnative2例では`normalization_matched=0`、`compilation_audit`は2件となる。コンパイル方式比較は一致0→2・追加2→0・削除2→0を専用項目へ記録する。subjectスペース整理も同時にある場合や複数IDの場合は、同一compiler内のCounter一致数増分だけを正規化件数に数える。方式の説明は`-v`と計画へ置き、通常表示は変更不要・差分・承認を中心にする。両監査情報をfilter/payloadから分離し、改変はdigest検証でAPI取得前に拒否する。

v6のグループ選択・最終承認・native payload・読戻しを通して確認し、見送ったAction/Deleteを保持した。v6の削除後checkpointから再開して重複送信なく完了し、完了後の再実行では追加せず、別の条件が外部から加わると停止した。既存のAction/Delete・Trash制約、原則50操作ごとの照合、応答喪失・復旧、時間・試行数表示は全体テストで回帰確認した。

## 旧計画を固定する証拠

v3/v4は従来の凍結fixtureを使い、XMLへのcompiler=legacy伝播とcompilation監査を後付けしないことも確認した。v5はpreimplementation commit `7331e42eb49ec5ea7a9d09cd34e77e4576c1573d`の実モジュールから未選択/選択済み計画と途中journalを生成した。保持ルールと未送信payloadの両方にfrom OR・subject中括弧があり、selected例はAction/Deleteを見送っている。

v5の4経路（未選択/選択済み × apply/run）は、plan/digestを再署名せず、ファイル参照先だけを一時XMLへ置換して再開した。旧compiler＋query-v1、送信payload、既存ID、旧journalイベント、見送りグループを維持し、済んだ削除を再送しない。旧未選択計画を現在の選択関数へ渡した結果も、旧モジュール生成の選択済み計画と完全一致した。生成方法は[fixture説明](../../tests/fixtures/README.md)に記載している。

独立AST比較では、`build_legacy_template`は旧`build_template`と名前・docstring以外が一致する。`split_or_atoms`、`query_atom`、`check_xml_rules`、`normalize_criteria`、`canonical_template`、`to_api`、`filter_snapshot`、`batch_progress`、`perform_batched`も同一ASTだった。新しい意味はcompiler選択とnative欄の構成へ限定されている。差分に関係する49関数には全てdocstringを確認した。

## 残る範囲

実Gmailへの配備・検索・新着分類、実Masterでの差分削減率、配備時間は未検証である。旧XML/GDATAの公式移行表をUI exportにも適用する点は仕様記載の推論であり、任意のnative/query条件の同等性を実証したものではない。フィールド跨ぎ同等判定・Trash括弧の統一は含めない。この記録はマージや実運用の完了を示さない。
