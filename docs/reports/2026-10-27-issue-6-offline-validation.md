# Issue #6 オフライン検証記録

2026-10-27。実装・PRレビュー対象の検証であり、実Gmailへの適用記録ではない。

## 結果

| 検証 | 結果 |
|---|---|
| Google SDK入りPython 3.12 Dockerでの全unittest | 257件成功、skipなし（既存214件＋メール関連43件） |
| Dockerビルドと追加CLIの起動 | 成功。追加モジュールをイメージへ同梱 |
| ローカルMasterの構文対応 | 930ルールのうちユーザーラベル用929件が検索変換を通過。ラベルなしTrash 1件は対象外 |
| 実Gmailでの計画・検索結果確認 | 未実施 |
| 実Gmailの更新・復元 | 未実施 |

## 再現コマンド

```bash
docker build --tag gmail-filter-issue6:test .
docker run --rm --network none --user 1000:1000 \
  --mount type=bind,src="$PWD",dst=/source,readonly \
  --workdir /source --entrypoint python gmail-filter-issue6:test \
  -m unittest discover -s tests -q
docker run --rm --network none gmail-filter-issue6:test reclassify --help
docker run --rm --network none gmail-filter-issue6:test restore-labels --help
```

テストは合成メタデータ・検索結果と一時ディレクトリを使い、OAuth取得・Gmail通信を行わない。実SDKのdiscovery定義による要求構築は、模擬HTTPへ送って検査する。

## 確認した境界

- 検索: 送信者OR、表示名、To/Cc/Bcc、件名OR・引用句・日本語・複数語、条件欄の積集合、除外検索、ラベル依存検索の固定、重複検索キャッシュ、未対応構文の拒否。
- 対象: 全ページ、1003メール、同じスレッドの送信済み／対象メール混在、新着除外、既存メール・ラベル定義の外部変更、履歴期限切れ、取得不能メール。
- 保全: 固定ファイル破損（承認待ち中を含む）、アカウント・pending digest・送信予定の不整合、共通ラベル保持、システムラベルを送らない要求。
- 更新: 承認辞退、非対話で認証・準備を開始しないこと、共通ローカル排他、バッチ上限停止、429/500、応答消失、部分反映、読戻し失敗、送信前と送信成功後の中断、未反映分だけの再開。
- 復元: 完了後・部分適用後の復元、システム状態保持、外部ラベル変更・対象外移動の拒否、復元中断・再開、復元拒否が以前の完了記録を変更しないこと。
- 認証: 権限不足時の再認証、拒否されたスコープ、別アカウント、非対話認証拒否、検査成功時だけの専用トークン原子的保存。
- 互換性: 既存フィルタ計画v3〜v7、配備の単独送信・読戻し・中断再開、計測、CLIを含む従来テスト。

実Masterの構文通過は、検索とnativeフィルタの実メール上の意味的一致を証明しない。初回の計画件数・保存検索結果を確認する工程と、独立承認による少量更新・復元の受入れを残す。PRのCI・レビュー結果も、このオフライン実行結果と分けて報告する。
