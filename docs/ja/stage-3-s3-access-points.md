# 段階 3: S3 Access Points による一部サーバーレス化

> 証拠区分: `hypothesis`（未測定）。手順は計画で、予想は実測前の仮説である。実測記録で置き換える。

対象ボリュームに FSx for ONTAP S3 Access Points を付け、Worker の処理の一部を AWS Lambda から
動かす。ボリュームの複製やデータの移動はしない。

## 手順（予定）

1. Lambda のサブネットのルートテーブルに S3 のプレフィックスリストがあることを確かめる
2. 見積りの提示と承認（`NetworkOrigin` と file system identity の値を含む）
3. 追加スタックの作成（S3 Access Points の付与を含む）
4. `GetObject` などのデータ操作で疎通を確かめる（`HeadBucket` は証拠にしない）
5. 目録と境界記録（`b3`）、Probe（S3 版）、比較

## 境界で確認すること

- 対象ボリュームの UUID と `seed/` の目録が段階 0 と同一であること
- セキュリティスタイルが `ntfs` のままであること
- S3 Access Points の対象が `appdata` であること

## 主張の出典

S3 Access Points の制約は、主張ごとに Hub のノートを出典として引く（内容は写さない）。

- S3 Access Points の制約（非対応 API、単一 ID での認可、ACL の非継承、イベント通知の不在）: [s3-access-point-constraints](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/data-utilization/notes/s3-access-point-constraints.md)
- 認可の二層構造: [access-point-authorization-layers](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/security-governance/notes/access-point-authorization-layers.md)

## 予想（未検証）

- この構成（`SINGLE_AZ_1`、AD 参加 SVM、NTFS ボリューム）で S3 Access Points を付けられる見込み（未確認）。
- S3 Access Points にはイベント通知がないので、起動はスケジュールによるポーリングにする。
