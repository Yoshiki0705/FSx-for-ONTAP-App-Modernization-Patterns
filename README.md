# FSx-for-ONTAP-App-Modernization-Patterns

**日本語** | [English](README.en.md)

Amazon FSx for NetApp ONTAP にデータを置いたまま、`DocIntake` という 1 つの自作コンソールアプリを、.NET Framework 4.8 / Windows / SMB から .NET 10 / Linux / NFS、さらに .NET 10 managed-runtime Lambda / FSx for ONTAP S3 Access Points へ移す検証済みパターン集です。
入力と出力の正本は FSx for ONTAP の `appdata` ボリュームに残し、アプリケーション側とプロトコル側を 1 段ずつ変えます。

> **状態**: 実行記録の区分は `verified`、単発の再現未確認の観測は `field-observation`、テンプレートやテストの宣言は `documented`、未実行または適用範囲外は `open/not measured` として分けています。記載する数値や挙動は、この検証構成のサンプル実行であり、本番性能、SLO、サービス上限、請求額の見積りではありません。

## 状態と証拠の境界

| 対象 | 証拠区分 | 記載範囲 |
|---|---|---|
| 段階 0〜3 の実行記録 | `verified` | `DocIntake` を対象にした実行記録。各段階の構成と未実行の操作は分けて扱います。 |
| ONTAP 9.19.1P2 の記録 | `verified` | 2026-10-07 の初回構築記録。 |
| ONTAP 9.19.1P3 の記録 | `verified` | 2026-10-11〜12 の再構築記録。P2 の記録とは別の履歴です。 |
| 単発の補助的な観測 | `field-observation` | 再現を確認していない観測。サービス上限や一般的な性能値へ一般化しません。 |
| テンプレートの宣言 | `documented` | `provided.al2023` はテンプレートに記載された選択肢ですが、実行していません。EventBridge Scheduler は無効化された宣言です。 |
| fake S3 テスト | `documented` | インメモリ fake を使うテストの事実です。実際の FSx for ONTAP S3 Access Points の挙動を証明しません。 |
| 未実行または適用範囲外 | `open/not measured` | 下の未検証範囲に記載します。 |

## 現在のカバレッジ

| 対象 | 証拠区分 | 現在の範囲 |
|---|---|---|
| 対象アプリケーション | `verified` | `DocIntake` という 1 つの自作コンソールアプリです。Web 層や別ランタイムの検証を含みません。 |
| 開始スライス | `verified` | .NET Framework 4.8 / Windows / SMB です。 |
| 段階 1 | `verified` | 同じ `appdata` ボリュームへ NFS を加えた SMB + NFS の構成です。セキュリティスタイルは `ntfs` のままです。 |
| 段階 2 | `verified` | .NET 10 / Linux / NFS です。比較は旧 Windows ビルドと近代化後の Linux ビルドであり、Windows .NET 10 の検証ではありません。 |
| 段階 3 | `verified` | .NET 10 managed runtime `dotnet10` の Lambda / FSx for ONTAP S3 Access Points です。呼び出しは手動で、EventBridge Scheduler は無効化され、その有効化時の実行は `open/not measured` です。 |
| 段階 3 の file-locking | `open/not measured` | 現在の `DocIntake` S3 アダプターのスライスではスキップしました。S3 全般の file-locking を主張しません。 |

### データ保存先

| データ | 証拠区分 | 保存先 |
|---|---|---|
| アプリケーションの入力と出力 | `verified` | FSx for ONTAP の `appdata` ボリュームです。 |
| 集約した Probe JSON | `verified` | 別の artifacts バケットです。`appdata` の入力・出力とは分けています。 |
| Lambda のログ | `verified` | CloudWatch Logs です。 |

## このリポジトリの範囲

| 扱うこと | 扱わないこと |
|---|---|
| 1 つの `DocIntake` コンソールアプリを、.NET Framework 4.8 / Windows / SMB から .NET 10 / Linux / NFS、さらに .NET 10 managed-runtime Lambda / FSx for ONTAP S3 Access Points へ移す手順 | オンプレミスや VMware からのデータ移行そのもの、Java、Python アプリケーション、Python Lambda、その他の未検証の Lambda ランタイム、Node.js、Go、ASP.NET、ASP.NET Core、Web API、Web UI の挙動 |
| FSx for ONTAP の `appdata` ボリュームを共有したまま、アプリケーションの実行形とアクセスプロトコルを段階的に変える検証 | ECS / EKS、Multi-AZ、`unix` / `mixed` セキュリティスタイル、その他の未検証ランタイムまたはトポロジー |
| 段階ごとの CloudFormation テンプレート、検証スクリプト、自作のサンプルアプリケーション | コンテナ runtime の検証。コンテナ化は将来の分岐として隣の Spoke の責務に置きます。 |
| AI Modernization Flow（AIMF）で .NET Framework を新しい .NET へ移すときの、FSx for ONTAP 固有の補足 | AIMF 本体の手順の複製や、AIMF が公開する別ランタイムのこの Spoke での検証 |

## 段階の構成

各段階は前の段階の構成を前提にし、ボリュームのデータは移動しません。
Windows のクライアントが残っている間は、ボリュームのセキュリティスタイルを NTFS のまま維持します。

| 段階 | アプリケーションの実行環境 | アクセスプロトコル | 主な変更点 |
|---|---|---|---|
| 0 | .NET Framework 4.8 on Amazon EC2（Windows） | SMB（`ntfs` セキュリティスタイル、SVM は AWS Managed Microsoft AD に参加） | 出発点の構成を再現する |
| 1 | 段階 0 と同じ `DocIntake` | SMB に加えて NFS（マルチプロトコル） | 同じ `appdata` ボリュームを NFS からも読み書きする |
| 2 | .NET 10 on Amazon EC2（Linux） | NFS | 旧 Windows ビルドと近代化後の Linux ビルドを比較する。Windows .NET 10 の検証ではない |
| 3 | .NET 10 managed-runtime Lambda（`dotnet10`） | FSx for ONTAP S3 Access Points | 手動 invocation で一部の処理を扱う。EventBridge Scheduler は無効化され、file-locking は現行スライスでスキップする |
| 分岐 | コンテナ | NFS / SMB / S3 Access Points | 将来のナビゲーションと責務分担を示す分岐。コンテナ runtime は `open/not measured` で、詳細は隣の Spoke を参照する |

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
| リージョン | `ap-northeast-1`（FSx for ONTAP を含むインフラ） |
| FSx for ONTAP の構成 | 検証専用の Single-AZ ファイルシステム、1 つの `appdata` ボリューム、`ntfs` セキュリティスタイル |
| ONTAP 9.19.1P2 の履歴 | `verified`。2026-10-07 の初回構築記録。 |
| ONTAP 9.19.1P3 の履歴 | `verified`。2026-10-11〜12 の再構築記録。P2 とは別の歴史的記録。 |
| セキュリティスタイル | `ntfs`。段階 2 の判断で維持しています。`unix` と `mixed` は `open/not measured` です。 |
| ディレクトリ | AWS Managed Microsoft AD |
| IaC | AWS CloudFormation（YAML）。`cfn-lint` と `cfn-guard` を通す構成です。 |
| AIMF | v0.11.0 に固定。2026-10-12 のセッションは kiro-cli 2.29.1 の V2 対話モードで実施。 |

**SnapLock と snapshot locking（Tamperproof Snapshot）は使いません。** どちらも保持期間が満了するまでボリュームを削除できなくする機能で、検証用のファイルシステムに置くと削除できない請求が残ります。
不可逆操作の扱いは [AGENTS.md](AGENTS.md) にあります。

AWS へのデプロイは、構成・所要時間・費用の見積りを提示し、承認を得てから毎回実行します。

## AIMF との関係

[AI Modernization Flow](https://github.com/aws-samples/sample-ai-modernization-flow)（[紹介ブログ](https://aws.amazon.com/jp/blogs/news/aidm-introducing-ai-modernization-flow/)）は、AI エージェントに決められた段階でアプリケーションの移行を進めさせるワークフローです。
この Spoke における AIMF は、アプリケーション・モダナイゼーションのワークフロー境界に限ります。AIMF が扱わない FSx for ONTAP 側のストレージ、プロトコル、権限、証拠の境界を補足します。AIMF が公開する別ランタイムや別トポロジーを、この Spoke の検証結果へ一般化しません。
AIMF が作る作業記録のリポジトリは公開しない作業領域に置き、公開するのは結果の抜粋だけです。
2026-10-12 に実施したセッションの手順、ADR の要旨、ハーネスが機械的に止める範囲と止めない範囲は[段階 2](docs/ja/stage-2-aimf-modern-dotnet.md)にあります。

## 未検証の範囲

| 範囲 | 証拠区分 | 境界 |
|---|---|---|
| 別ランタイム | `open/not measured` | Java、Python アプリケーション、Python Lambda、その他の未検証の Lambda ランタイム、Node.js、Go は対象外です。 |
| 別のアプリケーション形 | `open/not measured` | ASP.NET、ASP.NET Core、Web API、Web UI、HTTP ingress、認証ミドルウェア、長時間稼働サービスは対象外です。 |
| コンテナとトポロジー | `open/not measured` | ECS、EKS、Multi-AZ は対象外です。コンテナ分岐は runtime 挙動を検証していません。 |
| セキュリティスタイルと版 | `open/not measured` | `unix`、`mixed`、その他の ONTAP 版、複数ボリュームの挙動は対象外です。 |
| Stage 3 の未実行経路 | `open/not measured` | EventBridge Scheduler の有効化と `provided.al2023` は未実行です。`provided.al2023` はテンプレートに記載された選択肢に限ります。 |
| S3 の排他制御 | `open/not measured` | 現行の DocIntake S3 アダプターでは exclusive-open / file-locking をスキップしました。S3 全般に universal な file-locking がある、またはないという主張はしません。 |

この範囲の外へ、現在の .NET の実測結果を他のランタイム、アプリケーション形、セキュリティスタイル、トポロジーの証拠として一般化しません。

## 今後の候補

候補 A〜D は将来の検証候補であり、すべて `open/not measured` です。ここでは候補名と責務の境界だけを示し、暫定的なランタイム定数、構成値、実行条件、候補固有の設計は定めません。

| 候補 | 将来の検証単位 | 状態 |
|---|---|---|
| A | .NET の ASP.NET Core Web API またはバックグラウンドサービス | `open/not measured` |
| B | Java / Spring の別ランタイム検証 | `open/not measured` |
| C | Python Worker / Lambda と FSx for ONTAP S3 Access Points | `open/not measured` |
| D | ECS / EKS のコンテナ化 | `open/not measured`。コンテナ runtime の責務は Container-Datastore Spoke と調整します。 |

### 候補ごとの証拠要件

各候補は、現在の `verified` 記録を再利用せず、独立した evidence ledger と独立した verification conditions を持つ別パターンとして扱います。候補の実測、採用判断、公開範囲は、その証拠がそろった後に別の作業で決めます。
## 関連リポジトリ

旅程全体の中での位置づけと、各リポジトリの関係図は Hub の旅程マップだけが持ちます。
ここにある隣接 Spoke へのリンクは、ナビゲーションと責務の所有範囲を示す `documented` なコンテキストです。リンク先の記載は、この Spoke の実行記録や検証済み範囲を拡張しません。

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
