# 段階 3: S3 Access Points による一部サーバーレス化

> 証拠区分: 節ごとに示す。AWS の仕様（対応する操作、ランタイムの廃止日、課金の項目）は `documented` で、出典を添える。
> デプロイと Lambda の呼び出しの結果は `verified`、1 回の観測として書いたものは `field-observation`、
> 実行していないものは `hypothesis`、公開された一次情報も再現記録も無いものは `open` とした。
> 環境は特記がなければ ap-northeast-1、ONTAP 9.19.1P3、Single-AZ、1 ボリューム `appdata`（security style `ntfs`）。
> 計測の run id は `s3-20261011T212200Z`（2026-10-11〜12）、`s3b-20261012T022900Z`（2026-10-12）、`s3m-20261012T104800Z`（2026-10-12）。

Amazon FSx for NetApp ONTAP の対象ボリューム `appdata` に FSx for ONTAP S3 Access Points を付け、
Worker と Probe（S3 版）を AWS Lambda から動かす。ボリュームの複製やデータの移動はしない。

## 構成

テンプレート `templates/stage3-serverless.yaml` の設計は次のとおり。

| 要素 | 内容 |
|---|---|
| Lambda 関数 | .NET 10 の `DocIntake.Lambda`。ハンドラは `DocIntake.Lambda::DocIntake.Lambda.Function::Handler`、メモリ 512 MiB、タイムアウト 120 秒 |
| ランタイム | 既定は `dotnet10`（マネージドランタイム、Amazon Linux 2023）。もう 1 つの選択肢は `provided.al2023` で、自己完結のブートストラップを別にビルドする必要がある（いまの zip では動かない） |
| `dotnet8` を外した理由 | Lambda のランタイム一覧で `dotnet8` の廃止日は 2026-11-10 で、1 か月を切っている。`dotnet10` は 2028-11-14（2026-10-11 に確認）。新しいリージョンでは、6 か月以内に廃止されるランタイムを Lambda が提供しない |
| ネットワーク | VPC とプライマリサブネットは `appmod-base` の出力（`Fn::ImportValue`）から取る。セキュリティグループは Lambda 専用に作り、受信は 0 件、送信は HTTPS（443）だけにする。S3 Access Points と成果物バケットへは、`appmod-base` の S3 ゲートウェイエンドポイントを通る |
| 環境変数 | `S3_ACCESS_POINT_ARN`（`S3AccessPoint.ResourceARN`。SDK のバケット引数に渡す）、`ARTIFACTS_BUCKET`（成果物バケット）、`STORE=s3`、`FS_IDENTITY` |
| IAM | `s3:ListBucket` を S3 Access Points の ARN に、`s3:GetObject`・`s3:PutObject` をその `/object/*` に、成果物バケットへの読み書きをそのバケットに限る。`s3:DeleteObject` は付けない（段階 3 のコードは削除しない） |
| スケジュール | Amazon EventBridge Scheduler の `appmod-stage3-worker-poll`（5 分ごと）を `DISABLED` で作る。測定は手動で起動する |

デプロイの結果（`verified`）: このスタックは 3 回作成して削除した（`s3-20261011T212200Z`、`s3b-20261012T022900Z`、`s3m-20261012T104800Z`）。
`AWS::FSx::S3AccessPointAttachment` は `CREATE_COMPLETE`、Lifecycle は `AVAILABLE` になった。ファイルシステムの ID の種別は
WINDOWS（`APPMOD\appsvc`）、ネットワーク起点は VPC。削除は `delete-stack` で完了し、`s3-20261011T212200Z` の削除の待機は 20 分を超えた
（1 回の観測）。削除後に名前が `appmod` で始まる付与は残っていなかった。

## 手順

デプロイは次の順に実施した。作成の所要時間は記録に無い（未記録）。

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

### Lambda の実行結果

> `verified`、各 1 回の観測。ランタイムは `dotnet10`、ONTAP 9.19.1P3。時間とメモリは Lambda の記録で、記録に無い項目は「未記録」と書いた。

| run id | 呼び出し | 結果 | 実行時間 | 最大メモリ |
|---|---|---|---|---|
| `s3-20261011T212200Z`（先行の実装） | worker | `exit_code` 0。`out/s3/reports/doc-001〜005.txt.txt` と `index.json` を NFS 側で確認 | 2,505 ms（初回、Init 361 ms） | 115 MB |
| `s3-20261011T212200Z` | probe | 5 挙動のうち `measured` 4、`skipped` 1（file-locking） | 821 ms | 120 MB |
| `s3m-20261012T104800Z`（AIMF 由来のコード） | worker | `exit_code` 0。`out/s3/` を書き直し、`index.json` は 181 バイト（コンパクト JSON、ADR-4）、レポート 5 件 | 未記録 | 未記録 |
| `s3m-20261012T104800Z` | probe | `measured` 4、`skipped` 1（file-locking） | 未記録 | 未記録 |

## S3 版での 5 つの挙動の扱い

S3 API で成り立たない挙動は `skipped` にし、理由を `observed.reason` に書く。

| 挙動 | S3 版での扱い | `outcome` |
|---|---|---|
| `case-sensitivity` | `GetObject` で `seed/Docs/Index.json`（綴り違い）と実在の `seed/docs/index.json`（陽性対照）を引き、それぞれ引けたかを記録する。陽性対照が引けたときだけ `measured` | `measured` |
| `path-separator` | キーに `\` を含む `PutObject` を行い、読み戻せるかを記録する。キーは `probe/sep\check-s3.txt`（先行の実装は `probe\sep-check-s3.txt`。下の「ルート直下に書いた `\` 入りキーが SMB 側に残す影響」を避けるため `probe/` 配下に移した）。SMB と NFS 側でできたファイル名は、各ホストの目録で確かめる | `measured` |
| `file-locking` | S3 API に排他的に開く操作がないので測らない | `skipped` |
| `acl-evaluation` | 事前判定は S3 API にないので `skipped` と記録し、`probe/<run-id>/deny-appsvc.txt` への実際の `PutObject` の成否を記録する | `measured` |
| `write-visibility` | 目印のオブジェクトを `PutObject` し、そのキーを記録する | `measured` |

`DeleteObject` は S3 Access Points で使える操作に含まれる。`S3FileStore` の書き込み可否の判定（`CanWrite`）は
`PutObject` で一意のキーを書いてから `DeleteObject` で消す形にした。ただし段階 3 の Probe はこの判定を呼ばない。
Windows が `FileShare.None` で開いている間の `PutObject` と `GetObject` の成否（`observed.under_smb_lock`）は
2 台の組で測るもので、この単独の起動には含まれない。

## S3 版の 5 つの挙動の実測結果

> `verified`、各 1 回の観測。run id と日付を各行に書いた。結果は上の表の扱いと同じ区分で `measured` または `skipped`。

| 挙動 | 結果 | run id |
|---|---|---|
| `case-sensitivity` | 参照 `seed/Docs/Index.json` は解決せず（`resolved=false`）、陽性対照 `seed/docs/index.json` は解決した（`real_resolved=true`）。S3 Access Points 経由ではキーの大文字小文字を区別する | `s3b-20261012T022900Z`、`s3m-20261012T104800Z`（ともに 2026-10-12。同じ結果） |
| `path-separator` | キーに `\` を含む `PutObject` が成功し、読み戻せた（`read_back=true`）。`\` は普通の文字として扱われる。ボリューム上の置き場所はキーの書き方で決まる（次の節） | `s3b-20261012T022900Z`（キー `probe\sep-check-s3.txt`）、`s3m-20261012T104800Z`（キー `probe/sep\check-s3.txt`） |
| `file-locking` | `skipped`（S3 API に排他的に開く操作が無い） | 3 run とも同じ |
| `acl-evaluation` | `precheck` は `skipped`、`probe/<run-id>/deny-appsvc.txt` への `PutObject` は許可された（`io_put_allowed=true`）。許可されなかった場合（false 側）は未観測 | `s3-20261011T212200Z`、`s3b-20261012T022900Z`、`s3m-20261012T104800Z` |
| `write-visibility` | 目印のオブジェクトを書き、そのキーを記録した。時間差は測っていない（2 台の組が要る） | 3 run とも同じ |

- 先行の `s3-20261011T212200Z` の `case-sensitivity` は、陽性対照が解決せず（`real_resolved=false`）、結論に使わない。
  参照を `seed/` 配下に直した `s3b-20261012T022900Z` で測り直した。陽性対照の無い計測は、結果が偽でも「区別する」「見つからない」の
  どちらとも読めるため結論にならない。
- この Probe の出力は単独の Lambda 呼び出しでも `topology: cross-host` を出力する。これは固定のラベルで、2 台の協調を測った
  証拠ではない。2 台の協調は `probe_merge.py` が `cross-host`（`reasons` が空）と判定した組だけを数える。
- 読者が取る行動: S3 Access Points 経由のキーは大文字小文字を区別する。SMB で動いたコードが、同じ名前を綴り違いで S3 から引くと見つからない。
  S3 には排他的に開く操作が無いので、排他制御が要る処理は NFS か SMB の経路に置く。

## ルート直下に書いた `\` 入りキーが SMB 側に残す影響

> `verified`（検査の失敗と合格）。短縮名と長い名前の対応は推測（`field-observation`）。ONTAP 9.19.1P3、Single-AZ、1 ボリューム。

S3 のキーでは `\` は普通の文字である。`probe\sep-check-s3.txt` のように `\` を含むキーをバケット直下に書くと、ボリューム直下に
名前に `\` を含む 1 ファイル（1 バイト）ができた。この名前は SMB 側の最上位の一覧に 8.3 短縮名 `PROBES~1.TXT` として現れ、最上位に許す名前を
`out`・`probe`・`seed` に限る `check-invariant.py` が失敗した（`s3-20261011T212200Z` の `b3`、`s2m-20261012T090934Z` の `b2`、
`s3m-20261012T104800Z` の `b3`。2026-10-12）。

- 短縮名と長い名前の対応は推測である。SMB 側で長い名前を引いて照合してはおらず、NFS 側の長い名前のファイルを削除した後に `PROBES~1.TXT` も一覧から消えたことからの読みである。
- 対処の 1: Probe のキーを `probe/` 配下（`probe/sep\check-s3.txt`）に移した。`s3m-20261012T104800Z` では新しいキーはボリューム直下に現れず、
  `probe/` の中のファイルになった。
- 対処の 2: すでにボリューム直下にあったファイルは、NFS で UNIX ユーザー uid 10001 から `rm` を 1 回実行して削除した。削除後に `b3` を取り直した
  （`s3m-20261012T104800Z-clean`）と、`top_level_paths` は `out`・`probe`・`seed` の 3 件になり、`check-invariant.py` は合格した（終了コード 0）。
  削除は書き込みなので別途承認を得て行った。snapshot は 0 件で、この削除を戻す手段は無い。
- 読者が取る行動: S3 で `\` を含むキーを書く検証は `probe/` のような専用の接頭辞の下に限り、ボリューム直下に書かない。
  書いてしまうと SMB 側の最上位が汚れ、戻すには削除（書き込み）が要る。

## S3 Access Points の付与中だけ現れる name mapping

> `field-observation`。`s3-20261011T212200Z` の 1 回の観測で、他の構成での再現は未確認。

付与中の `b3` では、ONTAP の name mapping が 5 件で、うち 1 件が `s3_win`（replacement は `APPMOD\appsvc`）だった。`appmod-stage3` の削除後に
読み直すと、`s3_win` は無く、`b2` と同じ 4 件に戻っていた。付与で現れ、削除で消えた。アクセスポイントのファイルシステムの ID
（WINDOWS の `APPMOD\appsvc`）と replacement が一致する。AIMF 由来のコードの `s3m-20261012T104800Z` は、スタックの削除後に境界を取ったので、
この mapping は観測していない（4 件、`s3_win` なし）。

- この mapping を作った主体が FSx for ONTAP の付与処理かどうかは、ONTAP と AWS の文書で確認していない。
- 読者が取る行動: name mapping の件数を境界記録で比べるときは、アクセスポイントが付与中かどうかを併記する。
  付与中に取った記録と削除後に取った記録は、件数が 1 件違いうる。

## AD 参加 SVM での到達性と `HeadBucket`

> `open`。この Spoke では AD ドメインコントローラー（DC）への到達不能を作る試験をしていない。

「AD 参加 SVM では S3 Access Points の全データ操作が AD DC への到達性を要する」「DC に届かないとき `HeadBucket` だけが成功する」の 2 点は、
公開された一次情報も再現記録も無く、Hub の
[端末から S3 Access Points に届く条件](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/client-access/notes/what-an-endpoint-needs-to-reach-s3-access-points.md)も
`open` としている。この文書でも `open` として扱い、`verified` や `documented` とは書かない。

確かに言えるのは次の範囲である。`HeadBucket` はバケット（アクセスポイント）の存在を確かめる操作で、ファイルの読み書きの可否までは示さない。
読者が取る行動は、疎通を `HeadBucket` の成功だけで判断せず、実際に使うデータ操作（`GetObject`、`ListObjectsV2`、`PutObject`）の成功で確かめること。
この Spoke の段階 3 は、データ操作で疎通を確かめた（`s3b-20261012T022900Z`、`s3m-20261012T104800Z`）。

## 段階 3 の見積りの範囲

`estimate.py --target stage3` は、`appmod-base` の時間を見積り直さず、追加スタックの分だけを数える。
基盤の時間は `base`（と `extend`）の見積りに含まれている。

| 項目 | 数え方 | 単価の出所（ap-northeast-1） |
|---|---|---|
| Lambda の要求 | 手動起動の回数（既定 20 回、`--invocations` で指定） | Price List `AWSLambda` の `APN1-Request` |
| Lambda の実行時間 | 回数 × 0.5 GB × 120 秒の上限 | Price List `AWSLambda` の `APN1-Lambda-GB-Second`（第 1 段の単価） |
| S3 Access Points の要求 | 1 回あたり 100 件と仮定した上限。すべて PUT/COPY/POST/LIST の単価で計算する | Amazon FSx for NetApp ONTAP の料金ページ（英語版）の S3 Access Points 用のタブ。Asia Pacific (Tokyo) の PUT/COPY/POST/LIST は 1,000 件あたり $0.00108、料金ページのタブと AWS Price List API（サービスコード `AmazonS3`）の両方で 2026-10-12 に確認（表の下の補足を参照） |
| S3 Access Points の要求（GET の単価） | 同じ件数。参考として示し、合計には含めない | 同じタブ。GET とその他の要求は 1,000 件あたり $0.000029、2026-10-12 に確認 |
| CloudWatch Logs の取り込み | 1 回あたり 1 MiB と仮定した上限 | Price List `AmazonCloudWatch` の `APN1-DataProcessing-Bytes` |
| EventBridge Scheduler | `DISABLED` なので 0 回、$0 | なし |
| S3 のデータ転送 | 数えない。転送量はオブジェクトの大きさに依存し、見積りは仮定していない | 料金ページによれば、S3 Access Points を通るデータ転送は S3 バケットと同様に課金される |

要求の行は上限である。英語版の
[Amazon FSx for NetApp ONTAP の料金ページ](https://aws.amazon.com/fsx/netapp-ontap/pricing/)には
「S3 Access Points for FSx」のタブがあり、S3 Access Points に対する S3 の要求に課金されること、課金は要求の種別 <!-- allow:naming -->
（PUT/COPY/POST/LIST と、GET およびその他）と件数による 1,000 件あたりの料金であること、API/SDK の要求と同じ料率で
発生することが書かれている。「FSx for NetApp ONTAP」の行の Asia Pacific (Tokyo) の値は、PUT/COPY/POST/LIST が
1,000 件あたり $0.00108、GET とその他の要求が 1,000 件あたり $0.000029 である（2026-10-12 に確認。区分は「文書化」で、
出典はベンダーの 2 つ）。このタブはブラウザ側で描画されるため、静的な HTML の取得では見えない。
2026-10-12 時点の日本語ページには該当タブが見当たらなかった。見積りはこの値を使う。Worker と Probe は読み取りと
書き込みが混ざり、内訳を測っていないので、仮定した要求はすべて単価の高い PUT/COPY/POST/LIST で計算する。
GET とその他の単価（1 件 0.000000029 USD）なら同じ 2,000 件で 0.000058 USD、PUT/LIST の単価では 0.00216 USD になる
（20 回 × 100 件。算術であり請求額ではない）。Amazon S3 の標準の価格はこれより高いが、この見積りには使っていない。

S3 のデータ転送は、この見積りの合計に入っていない。転送量はオブジェクトの大きさに依存し、見積りは仮定していない。
同じ単価を AWS Price List API でも 2026-10-12 に確認した（Asia Pacific (Tokyo)）。SKU は `AmazonFSx` ではなくサービスコード
`AmazonS3` にあり、`AmazonFSx` には、このリージョンに S3 Access Points の要求の SKU が無い。
`APN1-Requests-FSXONTAP-Tier1`（SKU `ZE45RAAH9EX5HNT4`）が PUT/COPY/POST/LIST の 1,000 件あたり $0.00108、
`APN1-Requests-FSXONTAP-Tier2`（SKU `W647VRU94Q4WFUS7`）が GET とその他の要求の 1,000 件あたり $0.000029 で、
どちらも料金ページのタブと一致する。兄弟の usagetype `APN1-Requests-FSXOPENZFS-Tier1` と `-Tier2` は別の製品のもので、使っていない。
区分は `documented` のまま据え置く。出典はベンダーの 2 つで、請求額は観測していないので、
見積りは請求額が一致することを予測するものではない。要求の件数（1 回あたり 100 件）は仮定である。
価格ファイルにキーが欠けている場合、その行は既定値で埋めず、小計なしの `not retrieved` として記録する。
その行は合計に入らないので、合計は実際より低い値になる。見積りの出力で、`not retrieved` の行が無いことを確かめてから
合計を使う。

見積りは、承認する `parameters`（`BaseStackName`、`LambdaPackageKey`、`LambdaHandler`、`LambdaRuntime`、
`FileSystemIdentityUser`、指定したときだけ `ArtifactsBucketName`）も記録する。`deploy.sh stage3` はこれを
そのまま渡し、`parameters` が無いか空の見積りは `base` と同じく拒否する（終了コード 2）。

## 実行に使った見積り

段階 3 の 3 回の実行で承認した見積りの合計は、順に $0.04（2026-10-11）、$0.01（2026-10-12 02:19Z）、$0.01（2026-10-12 10:35Z）だった。
これらは上限の試算（単価 × 数量）であり、請求額ではない。請求額は未観測で、Cost Explorer での確認は 2026-10-13 に行う。

## 境界で確認すること

- 対象ボリュームの UUID と `seed/` の目録が段階 0 と同一であること
- セキュリティスタイルが `ntfs` のままであること
- S3 Access Points の対象が `appdata` であること

結果（`verified`）: UUID、security style、`seed/` の目録、export policy、snapshot、snapshot locking は `b0` から変わらず、
`check-no-locking.sh` は合格した。`check-invariant.py` は、ルート直下の `PROBES~1.TXT` が原因で失敗し、そのファイルを削除して
`b3` を取り直した後（`s3m-20261012T104800Z-clean`）に合格した。詳細は上の「ルート直下に書いた `\` 入りキーが SMB 側に残す影響」。

## 主張の出典

S3 Access Points の制約は、主張ごとに Hub のノートを出典として引く（内容は写さない）。

- S3 Access Points の制約（非対応 API、単一 ID での認可、ACL の非継承、イベント通知の不在）: [s3-access-point-constraints](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/data-utilization/notes/s3-access-point-constraints.md)
- 認可の二層構造: [access-point-authorization-layers](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/security-governance/notes/access-point-authorization-layers.md)
- S3 Access Points で使える操作（`DeleteObject` を含む。Object Lock と条件付き書き込みは使えない）: [Access point compatibility](https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/access-points-for-fsxn-object-api-support.html)（2026-10-11 に確認）
- Lambda のランタイムと廃止日: [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html)（2026-10-11 に確認）
- S3 Access Points 経由の S3 の要求の単価と、データ転送に課金されること: [Amazon FSx for NetApp ONTAP pricing](https://aws.amazon.com/fsx/netapp-ontap/pricing/) の英語ページ、タブ「S3 Access Points for FSx」（2026-10-12 に確認） <!-- allow:naming -->
- 同じ単価の AWS Price List API の SKU: サービスコード `AmazonS3`、usagetype `APN1-Requests-FSXONTAP-Tier1`（SKU `ZE45RAAH9EX5HNT4`）と `APN1-Requests-FSXONTAP-Tier2`（SKU `W647VRU94Q4WFUS7`）、Asia Pacific (Tokyo)、2026-10-12 に確認
- AD 参加 SVM での DC 到達性と `HeadBucket`（`open`）: [端末から S3 Access Points に届く条件](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/client-access/notes/what-an-endpoint-needs-to-reach-s3-access-points.md)

## 結果と予想の区分

結果（`verified`）に移したもの:

- この構成（`SINGLE_AZ_1`、AD 参加 SVM、NTFS ボリューム）で S3 Access Points を付けられた（3 回、上の「構成」）。
- 上の IAM の範囲（`DeleteObject` なし）で、Worker と Probe の読み書きが通った。Probe は `CanWrite` を呼ばず、Worker は削除しない
  （`s3-20261011T212200Z`、`s3m-20261012T104800Z`）。

`hypothesis` のまま残すもの:

- S3 Access Points にはイベント通知がないので、起動はスケジュールによるポーリングにする。段階 3 の測定では、スケジュールを `DISABLED` のまま手動で起動した。
  ポーリング起動は実行していない。
- マネージドランタイム `dotnet10` ではなく `provided.al2023` で動かす構成。試していない。

`open` のもの: AD 参加 SVM の全データ操作が DC 到達性を要するか、到達不能時に `HeadBucket` だけが成功するか（上の「AD 参加 SVM での到達性と `HeadBucket`」）。

未測定: security style `unix` と `mixed`、ONTAP 9.19.1P3 以外の版、Multi-AZ、他のボリューム構成、請求額、Lambda の作成の所要時間。
