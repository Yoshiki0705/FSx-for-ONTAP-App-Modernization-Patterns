# 検証環境: Amazon FSx for NetApp ONTAP の段階的モダナイゼーション基盤

> 証拠区分: 節ごとに書く。2026-10-07 に ap-northeast-1 で 1 回作って削除した環境の実測は
> `verified`、そのほかは手順の説明か未確認の事項である。

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

## 実際に作った環境の構成

> 証拠区分: `verified`（2026-10-07、ap-northeast-1）。承認した見積りのパラメータで `appmod-base` を
> 1 回作成した。パラメータは既定の全新規（5 種別の `Create<X>` がすべて `true`）、`EgressMode=endpoints`。

| 要素 | 構成 |
|---|---|
| ネットワーク | 専用 VPC を新規作成、2 AZ のサブネット、インターフェイスエンドポイント 6 サービス × 1 AZ、S3 のゲートウェイエンドポイント |
| FSx for ONTAP | `SINGLE_AZ_1`、1,024 GiB、128 MBps（最小構成、固定値） |
| SVM | AD 参加、NetBIOS 名 `APPMODSVM01` |
| 対象ボリューム | `appdata`（NTFS、`SnapshotPolicy: none`、`DeletionPolicy: Retain`） |
| ディレクトリ | AWS Managed Microsoft AD（Standard、DC 2 台）を新規作成 |
| クライアント | Windows EC2（`t3.large`）1 台、Linux EC2（`t3.medium`）1 台 |

エンドポイントを 1 AZ にした構成で、SVM のドメイン参加（発見済みドメインコントローラーで確認）と
Systems Manager の Run Command はどちらも動いた。VPC にはインターネットゲートウェイもあり、
Systems Manager の通信がエンドポイントを通ったかは確かめていない。

## 作成の手順と所要時間の実測

> 証拠区分: `verified`（2026-10-07、ap-northeast-1）。承認時刻（06:07:17 UTC）は承認記録による。
> `appmod-base` の作成と削除の時刻は、スタック削除後にスタック ID で取得した CloudFormation の
> スタックイベントによる（非公開の実行記録として保存）。

1. 見積りの提示と承認（構成・所要時間・費用）。見積りの単価は 2026-10-07 に AWS Price List API から取り直した
2. シークレット 4 つの作成（パスワードは生成し、コマンドライン引数に残さない）
3. ネットワークとシークレットの前提確認（読み取りのみ）
4. 基盤スタック `appmod-base` の作成。06:09:58 UTC に始まり 06:42:39 UTC に完了し、約 33 分かかった。時間の大半はディレクトリ（AWS Managed Microsoft AD）の 29.1 分で、ファイルシステムの 15.1 分はその間に並行して進んだ。SVM（2.0 分）とボリューム `appdata`（1.0 分）はディレクトリの完了後に作られた。1 回の作成での値で、所要時間の目安であって保証ではない
5. SVM のドメイン参加の確認（発見済みドメインコントローラーで判定）
6. 全ボリュームのロック無効の確認

所要時間は 1 回の作成の値で、作成のたびに同じになるとは言えない。設計時の参考値（Hub の実測 43 分）より短かった。

## 作成後に判明した前提の不足

> 証拠区分: `verified`（2026-10-07、ap-northeast-1、Windows Server 2022 の EC2 から確認）。

段階 0 の準備で、テンプレートと手順に次の 3 つの不足が見つかった。どれも直したうえで段階 0 を続けた。

| 不足 | 観測 | 対応 |
|---|---|---|
| Windows のインスタンスロールの権限 | AD ユーザーのパスワードを持つシークレット `appmod/app-users` と成果物バケットを、Windows のロールが読めなかった | 稼働中のスタックは更新せず、Windows のロールにインラインポリシー 2 つ（シークレット 1 つの ARN 限定、成果物バケットの ARN 限定）を帯域外で付けた。同じ付与は `templates/base.yaml` に入れたので、次に作る環境では帯域外の手順は要らない。削除時は `teardown.sh` が base スタックの削除前にこの 2 つを消す |
| AD ユーザーの置き場所 | ドメインルートの `CN=Users` には、AWS Managed Microsoft AD の委任管理者が書き込めなかった（Access is denied） | `appsvc` と `appreader` を委任ツリーの `OU=Users,OU=APPMOD` に作った（`scripts/create-ad-users.ps1`） |
| AD の管理経路 | Windows ホストから Active Directory Web Services（TCP 9389）に届かなかった。LDAP（389）と LDAPS（636）には届いた | ユーザー作成を ActiveDirectory モジュールではなく、`System.DirectoryServices` による LDAP で行うようにした |

## 削除の記録

> 証拠区分: `verified`（2026-10-07、ap-northeast-1）。`teardown.sh --apply` の実行記録による。

1 回目の `teardown.sh --apply` は手順 4 で止まった。`--svm` に ONTAP の SVM 名（`appmodsvm`）ではなく
FSx for ONTAP の API が返す SVM ID（`svm-` で始まる値）を渡したため、`integration-clone.sh sweep` が SVM の UUID を
解決できずに失敗した。手順 0〜3（Linux EC2 の起動、ロックが無いことの確認、`appmod-stage3` が無いことの
確認、S3 Access Points の関連付けが 0 件であることの確認）は通っていて、どれも何も削除しない手順である。
手順 4 の失敗で `teardown.sh` は終了コード 1 で止まり、`appdata` もほかのリソースも消していない。
失敗したら先へ進まない作りが、この誤入力に対して意図どおり働いた。`teardown.sh` と `--svm` を受け取る
各スクリプトは、いまは `svm-<16 進>` の形の値を何も呼ばずに終了コード 2 で拒否する。

2 回目は正しい SVM 名で実行し、手順 0〜10 を通って約 17:41 UTC に終了コード 0 で終わった。

- 手順 1 で、走査した 2 本のボリューム（`appdata` と SVM のルートボリューム）に snapshot locking も SnapLock も無いことを確かめた
- 手順 4・5 で、残っている FlexClone も `it_*` の Snapshot も無く、回復キューが空であることを確かめた
- 手順 6 で `appdata` を `SkipFinalBackup=true` 付きで削除した
- 手順 7b で、Windows のロールに帯域外で付けたインラインポリシー 2 つを消してから、手順 8 で base スタックを削除した。スタックの削除は 17:24:53 UTC に始まり 17:40:34 UTC に完了した（約 16 分、スタックイベントによる）
- 手順 9 でシークレット 4 つを回復期間なしで削除した
- 手順 10 の API の列挙は、2 回目の確認で残存なし（ファイルシステム、SVM、ボリューム、バックアップ、S3 Access Points の関連付け、ディレクトリ、タグ付きの EC2・ENI・エンドポイント、シークレット）になった。1 回目の確認ではシークレット 4 つが一覧にまだ出ていた

手順 11 では、2026-10-09 に Cost Explorer をリソース単位で確認した。値はいずれも確定前の見込み（`Estimated`）で、クレジットと返金を除いた利用分（`RECORD_TYPE=Usage`）である。

| リソース | 2026-10-07 | 2026-10-08 |
|---|---|---|
| ファイルシステム | $4.01 | $0.00 |
| Windows EC2 | $0.98 | $0.00 |
| Linux EC2 | $0.39 | $0.00 |
| 対照: 同じアカウントで稼働を続けた別のファイルシステム 2 つ | 各 $8.70 | 各 $5.07〜$5.08 |

- 10-08 のデータは途中まで入っており、対照のファイルシステムには通常の約 6 割の額が付いている。その時点で、削除したリソースには 0 件だった。削除後に課金が止まったと判断する根拠はこの対比で、10-08 の値が確定するまでは見込みの値による判断である
- EC2 は、作業の合間に止めた時間が課金されていないので、全時間で数えた算術見積りより少ない
- AWS Managed Microsoft AD とインターフェイスエンドポイントは、リソース単位の金額が Cost Explorer に出ない。同じアカウントの別のディレクトリが稼働を続けているため、サービスの合計からは切り分けられない

## 課金時間の算術見積り

> 証拠区分: 時刻は `verified`（2026-10-07、ap-northeast-1）。始点は承認記録、終点は削除の実行記録に
> よる。金額は算術値で、請求書の値ではない。

始点は承認記録の `approved_at`（06:07:17 UTC）に置く。課金されるリソースの作成は承認の後に始まるので、
この始点は実際の作成開始より早い側にある。終点は削除の完了（約 17:41 UTC）で、これは下限である。
シークレット 4 つの削除が 17:40:58 UTC で、その後に手順 10 の 60 秒間隔の確認が少なくとも 1 回あったので、
実際の完了はこれより後になる。
この間は約 11.6 時間（11 時間 34 分）である。
承認した見積りの単価（AWS Price List API、2026-10-07、ap-northeast-1）の時間あたり合計は $0.8015 で、
11.6 時間を掛けると約 $9.3 になる。

- 内訳は FSx for ONTAP の SSD $0.2104、スループット $0.1589、AWS Managed Microsoft AD $0.146、Windows EC2 $0.1364、
  Linux EC2 $0.0544、EBS gp3 $0.0092、インターフェイスエンドポイント $0.084、Secrets Manager $0.0022（いずれも 1 時間あたり）
- EC2 2 台も全時間を数えた。データ転送、Systems Manager、S3 の要求などは含めていない
- リソース単位で Cost Explorer に出た分（ファイルシステムと EC2 2 台）の 10-07 の合計は $5.38 で、見込みの値である（削除の記録の表を参照）。AD とエンドポイントの請求額は、リソース単位では確かめられない

## 見積りの出し方

課金を伴う操作の前に、単価を AWS Price List API から取り直して見積りを作る。
単価 × 時間は算術値であり、請求書の値ではない。取得できなかった項目は「取得できなかった」と書く。

## 残る未確認事項

- Cost Explorer の 10-07 と 10-08 の値の確定（2026-10-09 の時点では見込みの値）
- インターフェイスエンドポイントを 1 AZ に置いた構成で、Systems Manager の通信がエンドポイントを通ったか
