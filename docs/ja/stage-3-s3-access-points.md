# 段階 3: S3 Access Points による一部サーバーレス化

> 証拠区分: `hypothesis`（未測定）。手順は計画で、予想は実測前の仮説である。実測記録で置き換える。
> 下の AWS の仕様（対応する操作、ランタイムの廃止日、課金の項目）は文書に書かれた事実で、出典を添える。
> この構成で動くかどうかは、デプロイしていないので確認していない。

Amazon FSx for NetApp ONTAP の対象ボリューム `appdata` に FSx for ONTAP S3 Access Points を付け、
Worker と Probe（S3 版）を AWS Lambda から動かす。ボリュームの複製やデータの移動はしない。

## 構成（予定）

| 要素 | 内容 |
|---|---|
| Lambda 関数 | .NET 10 の `DocIntake.Lambda`。ハンドラは `DocIntake.Lambda::DocIntake.Lambda.Function::Handler`、メモリ 512 MiB、タイムアウト 120 秒 |
| ランタイム | 既定は `dotnet10`（マネージドランタイム、Amazon Linux 2023）。もう 1 つの選択肢は `provided.al2023` で、自己完結のブートストラップを別にビルドする必要がある（いまの zip では動かない） |
| `dotnet8` を外した理由 | Lambda のランタイム一覧で `dotnet8` の廃止日は 2026-11-10 で、1 か月を切っている。`dotnet10` は 2028-11-14（2026-10-11 に確認）。新しいリージョンでは、6 か月以内に廃止されるランタイムを Lambda が提供しない |
| ネットワーク | VPC とプライマリサブネットは `appmod-base` の出力（`Fn::ImportValue`）から取る。セキュリティグループは Lambda 専用に作り、受信は 0 件、送信は HTTPS（443）だけにする。S3 Access Points と成果物バケットへは、`appmod-base` の S3 ゲートウェイエンドポイントを通る |
| 環境変数 | `S3_ACCESS_POINT_ARN`（`S3AccessPoint.ResourceARN`。SDK のバケット引数に渡す）、`ARTIFACTS_BUCKET`（成果物バケット）、`STORE=s3`、`FS_IDENTITY` |
| IAM | `s3:ListBucket` を S3 Access Points の ARN に、`s3:GetObject`・`s3:PutObject` をその `/object/*` に、成果物バケットへの読み書きをそのバケットに限る。`s3:DeleteObject` は付けない（段階 3 のコードは削除しない） |
| スケジュール | Amazon EventBridge Scheduler の `appmod-stage3-worker-poll`（5 分ごと）を `DISABLED` で作る。測定は手動で起動する |

## 手順（予定）

1. Lambda のサブネットのルートテーブルに S3 のプレフィックスリストがあることを確かめる
2. `DocIntake.Lambda` の zip を作り（`dotnet publish -r linux-x64 --self-contained false`）、成果物バケットの `lambda/worker.zip` に置く
3. `estimate.py --target stage3` で見積りを作り、承認を得る（`NetworkOrigin` と file system identity の値を含む）
4. `deploy.sh stage3` で追加スタックを作る（S3 Access Points の付与を含む）。見積りに記録した `parameters` をそのまま `create-stack` に渡す
5. `GetObject` などのデータ操作で疎通を確かめる（`HeadBucket` は証拠にしない）
6. 目録と境界記録（`b3`）、Probe（S3 版）、比較

## Lambda の起動方法

ペイロードは `{"mode":"worker"|"probe","run_id":"...","stage":"s3"}` の形にする。`stage` は `s3` 以外を受け付けない。

| `mode` | 動作 | 結果の置き場所 |
|---|---|---|
| `worker` | `seed/inbox/` を読み、`out/s3/reports/` と `out/s3/index.json` を書く | `appdata` の `out/s3/` |
| `probe` | 5 つの挙動を S3 版で測り、`appmod-probe/1` の JSON を作る | 成果物バケットの `probe/<run-id>/lambda-s3.json`。同じ JSON を呼び出しの結果としても返す |

```bash
aws lambda invoke --region ap-northeast-1 --function-name appmod-stage3-worker \
  --cli-binary-format raw-in-base64-out \
  --payload '{"mode":"probe","run_id":"s3-20261011T000000Z","stage":"s3"}' result.json
```

スケジュールを動かすときは、`aws scheduler get-schedule` で得た定義のまま `--state ENABLED` を付けて
`aws scheduler update-schedule` を呼ぶ。`update-schedule` は定義全体を置き換えるので、`State` だけを渡すと
他の値が失われる。測定のあとは `DISABLED` に戻す（`teardown.sh` も削除の前に `DISABLED` にする）。

## S3 版での 5 つの挙動の扱い

S3 API で成り立たない挙動は `skipped` にし、理由を `observed.reason` に書く。

| 挙動 | S3 版での扱い | `outcome` |
|---|---|---|
| `case-sensitivity` | `GetObject` で `Docs/Index.json` と `docs/index.json` を引き、それぞれ引けたかを記録する | `measured` |
| `path-separator` | キーに `\` を含む `PutObject`（`probe\sep-check-s3.txt`）を行い、読み戻せるかを記録する。SMB と NFS 側でできたファイル名は、各ホストの目録で確かめる | `measured` |
| `file-locking` | S3 API に排他的に開く操作がないので測らない | `skipped` |
| `acl-evaluation` | 事前判定は S3 API にないので `skipped` と記録し、`probe/<run-id>/deny-appsvc.txt` への実際の `PutObject` の成否を記録する | `measured` |
| `write-visibility` | 目印のオブジェクトを `PutObject` し、そのキーを記録する | `measured` |

`DeleteObject` は S3 Access Points で使える操作に含まれる。`S3FileStore` の書き込み可否の判定（`CanWrite`）は
`PutObject` で一意のキーを書いてから `DeleteObject` で消す形にした。ただし段階 3 の Probe はこの判定を呼ばない。
Windows が `FileShare.None` で開いている間の `PutObject` と `GetObject` の成否（`observed.under_smb_lock`）は
2 台の組で測るもので、この単独の起動には含まれない。

## 段階 3 の見積りの範囲

`estimate.py --target stage3` は、`appmod-base` の時間を見積り直さず、追加スタックの分だけを数える。
基盤の時間は `base`（と `extend`）の見積りに含まれている。

| 項目 | 数え方 | 単価の出所（ap-northeast-1） |
|---|---|---|
| Lambda の要求 | 手動起動の回数（既定 20 回、`--invocations` で指定） | Price List `AWSLambda` の `APN1-Request` |
| Lambda の実行時間 | 回数 × 0.5 GB × 120 秒の上限 | Price List `AWSLambda` の `APN1-Lambda-GB-Second`（第 1 段の単価） |
| S3 Access Points の要求 | 1 回あたり 100 件と仮定した上限。すべて PUT/COPY/POST/LIST の単価で計算する | Amazon S3 の Price List `AmazonS3` の `APN1-Requests-Tier1`（SKU XAKHUSC46NWC9PPM）。1,000 件あたり $0.0047、ap-northeast-1、2026-10-12 に確認。適用されるものと仮定している（表の下の補足を参照） |
| S3 Access Points の要求（GET の単価） | 同じ件数。参考として示し、合計には含めない | Amazon S3 の Price List `AmazonS3` の `APN1-Requests-Tier2`（SKU 9C7873KU3ZQBD6BJ）。10,000 件あたり $0.0037、ap-northeast-1、2026-10-12 に確認 |
| CloudWatch Logs の取り込み | 1 回あたり 1 MiB と仮定した上限 | Price List `AmazonCloudWatch` の `APN1-DataProcessing-Bytes` |
| EventBridge Scheduler | `DISABLED` なので 0 回、$0 | なし |
| S3 のデータ転送 | 数えない。転送量はオブジェクトの大きさに依存し、見積りは仮定していない | 料金ページによれば、S3 Access Points を通るデータ転送は S3 バケットと同様に課金される |

要求の行は上限であり、仮定の上に立っている。英語版の
[Amazon FSx for NetApp ONTAP の料金ページ](https://aws.amazon.com/fsx/netapp-ontap/pricing/)（2026-10-12 に確認）は、
S3 Access Points 経由でデータにアクセスすると S3 の要求に課金されると書いており、課金そのものはある。
一方でこのページは要求あたりの単価を載せておらず、これらの要求に Amazon S3 Standard の要求単価が適用されるとも
書いていない。そのため見積りは、上の S3 の単価（ap-northeast-1、オンデマンド、2026-10-12 に確認）を適用すると
仮定しており、課金が確認されたものではない。Worker と Probe は読み取りと書き込みが混ざり、内訳を測っていないので、
仮定した要求はすべて単価の高い PUT/COPY/POST/LIST で計算する。GET の単価（1 件 0.00000037 USD）なら同じ
2,000 件で 0.00074 USD、PUT/LIST の単価では 0.0094 USD になる（20 回 × 100 件。算術であり請求額ではない）。
`AmazonFSx` の Price List には S3 Access Points の要求の usagetype がない（2026-10-11 に確認）ので、
単価の出所は S3 の Price List だけである。価格ファイルにキーが欠けている場合、その行は既定値で埋めず、
小計なしの `not retrieved` として記録する。

見積りは、承認する `parameters`（`BaseStackName`、`LambdaPackageKey`、`LambdaHandler`、`LambdaRuntime`、
`FileSystemIdentityUser`、指定したときだけ `ArtifactsBucketName`）も記録する。`deploy.sh stage3` はこれを
そのまま渡し、`parameters` が無いか空の見積りは `base` と同じく拒否する（終了コード 2）。

## 境界で確認すること

- 対象ボリュームの UUID と `seed/` の目録が段階 0 と同一であること
- セキュリティスタイルが `ntfs` のままであること
- S3 Access Points の対象が `appdata` であること

## 主張の出典

S3 Access Points の制約は、主張ごとに Hub のノートを出典として引く（内容は写さない）。

- S3 Access Points の制約（非対応 API、単一 ID での認可、ACL の非継承、イベント通知の不在）: [s3-access-point-constraints](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/data-utilization/notes/s3-access-point-constraints.md)
- 認可の二層構造: [access-point-authorization-layers](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/security-governance/notes/access-point-authorization-layers.md)
- S3 Access Points で使える操作（`DeleteObject` を含む。Object Lock と条件付き書き込みは使えない）: [Access point compatibility](https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/access-points-for-fsxn-object-api-support.html)（2026-10-11 に確認）
- Lambda のランタイムと廃止日: [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html)（2026-10-11 に確認）
- S3 Access Points 経由の S3 の要求とデータ転送に課金されること: [Amazon FSx for NetApp ONTAP pricing](https://aws.amazon.com/fsx/netapp-ontap/pricing/) の英語ページ（2026-10-12 に確認）。要求の単価は Amazon S3 の Price List から取る（上の見積りの節を参照）

## 予想（未検証）

- この構成（`SINGLE_AZ_1`、AD 参加 SVM、NTFS ボリューム）で S3 Access Points を付けられる見込み（未確認）。
- S3 Access Points にはイベント通知がないので、起動はスケジュールによるポーリングにする。段階 3 の測定では、スケジュールを `DISABLED` のまま手動で起動する。
- 上の IAM の範囲（`DeleteObject` なし）で Worker と Probe の読み書きが通る見込み（未確認）。
