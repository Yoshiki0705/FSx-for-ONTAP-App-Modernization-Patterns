# 検証環境: Amazon FSx for NetApp ONTAP の段階的モダナイゼーション基盤

> 証拠区分: `hypothesis`（未測定）。この文書の手順と予想は実施前の計画であり、
> 実測記録が揃った段階で該当箇所を `verified` に上げる（環境・日付・ONTAP の版を本文に書く）。

Amazon FSx for NetApp ONTAP（以下 FSx for ONTAP）にデータを置いたまま、
Windows 上の .NET Framework アプリを 4 段階で移す検証の、環境の構成と作成・削除の手順をまとめる。
環境は ap-northeast-1 に 1 つだけ作り、段階 0 から 3 を順に実施してから削除する。

## 不可逆機能の不使用

この検証では次を使わない。環境の作成前にテンプレートの静的検査（cfn-guard）とコマンド実行前の
hook の 2 層で機械的に止める。

- SnapLock（Compliance / Enterprise、監査ログボリューム）
- snapshot locking（Tamperproof Snapshot）。retention 付きの Snapshot ポリシーと SnapMirror ポリシーも作らない
- S3 Object Lock、AWS Backup Vault Lock、EBS snapshot lock

snapshot locking には AWS API のパラメータがないので、cfn-guard では止められない。
段階 2 の間は `fsxadmin` の到達経路を塞ぎ、各段階の境界と削除前に全ボリュームを列挙して
ロックが無効であることを確かめる。

## 実測した ONTAP の版

> 証拠区分: `verified`（2026-10-07、ap-northeast-1、`SINGLE_AZ_1`、1,024 GiB / 128 MBps）。

この検証のファイルシステムは、CloudFormation で版を指定せずに作成し、`NetApp Release 9.19.1P2`
（ビルド日 2026-08-19）で動いていた。段階 0 と段階 1 の境界記録（`b0`、`b1`）に加え、削除直前の
2026-10-07 17:06 UTC にも `GET /api/cluster?fields=version` で読み、同じ値だった。

- 段階 0〜1 の結果は、すべてこの版での値である。Hub が引き継いでいる知見の基準は 9.17.1P7D1 で、
  この検証の版はそれより新しい。
- 次の 2 点は 9.19.1P2 で確かめた。SnapLock でないボリュームの `snaplock.type` は `non_snaplock`
  を返した。ドメインコントローラーの発見は `/api/protocols/cifs/domains/{svm.uuid}?fields=discovered_servers`
  で確認でき、`/api/protocols/active-directory` は 0 件を返した。
- 1 回の作成で観測した版であり、新しく作るファイルシステムが常に 9.19.1P2 になるとは言えない。
- 版は AWS 管理面の API からは読めなかった。`describe-file-systems` の `OntapConfiguration` が返したキーは
  `DeploymentType`、`DiskIopsConfiguration`、`Endpoints`、`HAPairs`、`PreferredSubnetId`、
  `ThroughputCapacity`、`ThroughputCapacityPerHAPair`、`WeeklyMaintenanceStartTime` で、版を示す
  キーはなかった（2026-10-07）。版を確かめるには、VPC 内から管理エンドポイントへ届くホストで、
  `fsxadmin` の資格情報を使い、ONTAP REST の `GET /api/cluster?fields=version` か ONTAP CLI の
  `version` を実行する。

## 環境の構成（予定）

| 要素 | 構成 |
|---|---|
| FSx for ONTAP | `SINGLE_AZ_1`、1,024 GiB、128 MBps（最小構成、固定値） |
| SVM | AD 参加、NetBIOS 名 `APPMODSVM01` |
| 対象ボリューム | `appdata`（NTFS、`SnapshotPolicy: none`、`DeletionPolicy: Retain`） |
| ディレクトリ | AWS Managed Microsoft AD（Standard、DC 2 台） |
| クライアント | Windows EC2 1 台、Linux EC2 1 台 |

## 作成の手順（予定）

1. 見積りの提示と承認（構成・所要時間・費用）
2. シークレット 4 つの作成（パスワードは生成し、コマンドライン引数に残さない）
3. ネットワークとシークレットの前提確認（読み取りのみ）
4. 基盤スタックの作成
5. SVM のドメイン参加の確認（発見済みドメインコントローラーで判定）
6. 全ボリュームのロック無効の確認

## 削除の手順（予定）

削除は報告のみを既定とし、削除一覧の確認を得てから実行する。
削除の順序と削除後の存在確認は、実施時に本文へ記録する。

## 見積りの出し方（予定）

課金を伴う操作の前に、単価を AWS Price List API から取り直して見積りを作る。
単価 × 時間は算術値であり、請求書の値ではない。取得できなかった項目は「取得できなかった」と書く。

## 予想（未検証）

- 基盤スタックの作成に約 45 分かかる見込み（Hub の実測を参考値とする。この Spoke では未測定）。
- エンドポイントを 1 AZ に置いてもドメイン参加と Systems Manager が動く見込み（未確認）。
