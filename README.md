# FSx-for-ONTAP-App-Modernization-Patterns

**日本語** | [English](README.en.md)

Amazon FSx for NetApp ONTAP にデータを置いたまま、Windows 上の .NET Framework アプリケーションを段階的に Linux 上の .NET、さらに一部サーバーレスへ移すための検証済みパターン集です。
データの置き場所は FSx for ONTAP のボリュームから動かさず、アプリケーション側とプロトコル側を 1 段ずつ変えます。

> **状態**: 雛形の段階です。各段階の手順・テンプレート・検証結果は、検証が済んだものから追加します。
> 現時点で検証済みの段階はありません。

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

## 検証環境の前提

| 項目 | 値 |
|---|---|
| リージョン | ap-northeast-1（FSx for ONTAP を含むインフラ） |
| FSx for ONTAP | 検証専用に新しく作るファイルシステム。Single-AZ、ストレージ容量とスループット容量は最小構成 |
| ディレクトリ | AWS Managed Microsoft AD |
| IaC | AWS CloudFormation（YAML）。`cfn-lint` と `cfn-guard` を通す |
| AIMF | v0.11.0 に固定 |

**SnapLock と snapshot locking（Tamperproof Snapshot）は使いません。** どちらも保持期間が満了するまでボリュームを削除できなくする機能で、検証用のファイルシステムに置くと削除できない請求が残ります。
不可逆操作の扱いは [AGENTS.md](AGENTS.md) にあります。

AWS へのデプロイは、構成・所要時間・費用の見積りを提示し、承認を得てから毎回実行します。

## AIMF との関係

[AI Modernization Flow](https://github.com/aws-samples/sample-ai-modernization-flow)（[紹介ブログ](https://aws.amazon.com/jp/blogs/news/aidm-introducing-ai-modernization-flow/)）は、AI エージェントに決められた段階でアプリケーションの移行を進めさせるワークフローです。
対象はアプリケーション自体の移行で、クラウド環境の設計と構築は対象外とされています。
このリポジトリは段階 2 で AIMF を使い、AIMF が扱わないストレージ側の前提（共有パス、権限の対応付け、プロトコルの切り替え）を補足として持ちます。
AIMF が作る作業記録のリポジトリは公開しない作業領域に置き、公開するのは結果の抜粋だけです。

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
