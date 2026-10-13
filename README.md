# FSx-for-ONTAP-App-Modernization-Patterns

**日本語** | [English](README.en.md)

Amazon FSx for NetApp ONTAP にデータを置いたまま、Windows 上の .NET Framework アプリケーションを段階的に Linux 上の .NET、さらに一部サーバーレスへ移すための検証済みパターン集です。
データの置き場所は FSx for ONTAP のボリュームから動かさず、アプリケーション側とプロトコル側を 1 段ずつ変えます。

> **状態**: 段階 0 から 3 を計測済みです。ONTAP 9.19.1P2（2026-10-07 の初回構築）と 9.19.1P3（2026-10-11〜12 の再構築）で、Single-AZ、1 ボリューム、セキュリティスタイル ntfs の構成を対象にしました。
> 計測値は各 1 回の観測が中心で、各文書に run id、日付、環境、証拠区分を書いています。
> 未計測の範囲は次のとおりです: セキュリティスタイル unix と mixed、他の ONTAP の版、Multi-AZ、コンテナ分岐、請求書（費用は見積りと、タグとリソース ID で帰属できた Cost Explorer の見込み値のみ。請求書は未観測）。
> これらは `hypothesis` として扱います。

## このリポジトリの範囲

| 扱うこと | 扱わないこと |
|---|---|
| FSx for ONTAP のボリュームを共有したまま、アプリケーションの実行環境とアクセスプロトコルを段階的に変える手順 | オンプレミスや VMware からのデータ移行そのもの（隣の Spoke が扱う） |
| 段階ごとの CloudFormation テンプレート、検証スクリプト、自作のサンプルアプリケーション | コンテナ化の詳細（分岐として隣の Spoke へ渡す） |
| AI Modernization Flow（AIMF）で .NET Framework を新しい .NET へ移すときの、FSx for ONTAP 固有の補足 | AIMF 本体の手順の複製（AIMF のリポジトリを参照する） |

## 段階の構成

各段階は前の段階の構成を前提にし、ボリュームのデータは移動しません。
Windows のクライアントが残っている間は、ボリュームのセキュリティスタイルを NTFS のまま維持します。

| 段階 | アプリケーションの実行環境 | アクセスプロトコル | 主な変更点 |
|---|---|---|---|
| 0 | .NET Framework on Amazon EC2（Windows） | SMB（NTFS セキュリティスタイル、SVM は AWS Managed Microsoft AD に参加） | 出発点の構成を再現する |
| 1 | 段階 0 と同じ | SMB に加えて NFS（マルチプロトコル） | Windows と UNIX のユーザーの対応付けを設定し、同じボリュームを NFS からも読み書きする |
| 2 | 新しい .NET on Amazon EC2（Linux） | NFS | AIMF の `dotnetfw-to-modern-dotnet` playbook でアプリケーションを移し、Linux の EC2 インスタンスへ置き換える |
| 3 | 一部の処理をサーバーレスへ | FSx for ONTAP S3 Access Points | ファイルを読む処理の一部を、S3 Access Points 経由で AWS Lambda などから扱う |
| 分岐 | コンテナ（Amazon ECS / Amazon EKS） | NFS / SMB / S3 Access Points | 段階 1 以降のどこからでも分岐できる。詳細は隣の Spoke を参照する |

## 文書の索引

| 読みたいこと | 参照先 |
|---|---|
| 検証を自分の環境で再現する順序（前提、見積りと承認、段階 0 から 3、後片付け、再現できない範囲） | [再現手順](docs/ja/reproduce.md) |
| 段階 0: SMB と NTFS の出発点 | [段階 0](docs/ja/stage-0-smb-ntfs.md) |
| 段階 1: 同じボリュームへの NFS の追加 | [段階 1](docs/ja/stage-1-multiprotocol.md) |
| 段階 2: Linux 上の新しい .NET への移行（AIMF のセッション記録を含む） | [段階 2](docs/ja/stage-2-aimf-modern-dotnet.md) |
| 段階 3: S3 Access Points による一部サーバーレス化 | [段階 3](docs/ja/stage-3-s3-access-points.md) |
| 検証方法から得た教訓（対照のない計測は結果にならない、など） | [検証方法の教訓](docs/ja/verification-method-lessons.md) |
| 検証環境の構成と費用の見積り | [検証環境](docs/ja/environment.md) |
| サンプルアプリケーションの構成と 5 つの挙動 | [サンプルアプリ](docs/ja/sample-app.md) |
| コンテナ分岐（未計測） | [分岐: コンテナ化](docs/ja/branch-containerization.md) |

英語版は [docs/en/](docs/en/stage-0-smb-ntfs.md) にあり、節構造は日本語版と一致させています。

## 検証環境の前提

| 項目 | 値 |
|---|---|
| リージョン | ap-northeast-1（FSx for ONTAP を含むインフラ） |
| FSx for ONTAP | 検証専用に新しく作るファイルシステム。Single-AZ、ストレージ容量とスループット容量は最小構成。計測した ONTAP の版は 9.19.1P2（2026-10-07）と 9.19.1P3（2026-10-11〜12） |
| セキュリティスタイル | ntfs（段階 2 の決定で維持。決定者は利用者） |
| ディレクトリ | AWS Managed Microsoft AD |
| IaC | AWS CloudFormation（YAML）。`cfn-lint` と `cfn-guard` を通す |
| AIMF | v0.11.0 に固定。2026-10-12 のセッションは kiro-cli 2.29.1 の V2 対話モードで実施 |

**SnapLock と snapshot locking（Tamperproof Snapshot）は使いません。** どちらも保持期間が満了するまでボリュームを削除できなくする機能で、検証用のファイルシステムに置くと削除できない請求が残ります。
不可逆操作の扱いは [AGENTS.md](AGENTS.md) にあります。

AWS へのデプロイは、構成・所要時間・費用の見積りを提示し、承認を得てから毎回実行します。

## AIMF との関係

[AI Modernization Flow](https://github.com/aws-samples/sample-ai-modernization-flow)（[紹介ブログ](https://aws.amazon.com/jp/blogs/news/aidm-introducing-ai-modernization-flow/)）は、AI エージェントに決められた段階でアプリケーションの移行を進めさせるワークフローです。
対象はアプリケーション自体の移行で、クラウド環境の設計と構築は対象外とされています。
このリポジトリは段階 2 で AIMF を使い、AIMF が扱わないストレージ側の前提（共有パス、権限の対応付け、プロトコルの切り替え）を補足として持ちます。
AIMF が作る作業記録のリポジトリは公開しない作業領域に置き、公開するのは結果の抜粋だけです。
2026-10-12 に実施したセッションの手順、ADR の要旨、ハーネスが機械的に止める範囲と止めない範囲は[段階 2](docs/ja/stage-2-aimf-modern-dotnet.md)にあります。

## 関連リポジトリ

旅程全体の中での位置づけと、各リポジトリの関係図は Hub の旅程マップだけが持ちます。

| 読みたいこと | 参照先 |
|---|---|
| 旅程全体と、このリポジトリの位置づけ | [モダナイゼーション旅程マップ](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/reference/modernization-journey-map.md)（Hub） |
| AWS Transform でデータとサーバーを移す手順 | [AWS Transform による移行手順](https://github.com/Yoshiki0705/VMware-Migration-EC2-ONTAP/blob/main/docs/ja/aws-transform-migration-procedure.md) |
| 分岐: コンテナ化と FSx for ONTAP の連携 | [コンテナ化への派生と FSx for ONTAP の連携可否](https://github.com/Yoshiki0705/FSx-for-ONTAP-Container-Datastore-Patterns/blob/main/docs/ja/atx-containerization-fsxn-derivation.md) |
| 段階 3 の先: S3 Access Points による処理パターン | [FSx-for-ONTAP-S3AccessPoints-Serverless-Patterns](https://github.com/Yoshiki0705/FSx-for-ONTAP-S3AccessPoints-Serverless-Patterns/blob/main/README.md) |

## 品質ゲート

```bash
make install   # .venv に固定版のツールを導入
make all       # lint・監査・リンク・テスト一式
```

各ゲートの内容は [docs/agent/quality-gates.md](docs/agent/quality-gates.md) にあります。
