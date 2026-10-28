# FSx-for-ONTAP-App-Modernization-Patterns

**日本語** | [English](README.en.md)

このリポジトリは、データを Amazon FSx for NetApp ONTAP に残したまま、レガシーなアプリケーションやシステムを近代化するパターンを扱います。現在検証済みなのは、`DocIntake` という 1 つの自作コンソールアプリを .NET Framework 4.8 / Windows / SMB から .NET 10 / Linux / NFS、さらに .NET 10 managed-runtime Lambda / FSx for ONTAP S3 Access Points へモダナイズする記録です。広いスコープはプロジェクトの境界であり、すべてのランタイムやトポロジーの検証済みサポートを示しません。

入力と出力の正本は FSx for ONTAP の `appdata` ボリュームに残し、アプリケーション側とプロトコル側を 1 段ずつ変えます。

## リポジトリのスコープ

このリポジトリの持続的なスコープは、データを FSx for ONTAP に残したまま、レガシーなアプリケーションやシステムを近代化するパターンです。パターンごとの requirements、design、実装、検証記録、エビデンス境界、関連資料へのナビゲーションを扱います。現在の `DocIntake` の記録はこの広いスコープの一例であり、別のランタイム、アプリケーション形、実行トポロジー、アクセスパスの互換性を示すものではありません。

## レガシーなアプリケーションの例と置き換えの導線

このリポジトリの検証対象は 1 つの `DocIntake` ですが、同じ考え方は、データをファイル共有に置いたまま実行環境を移したい多くのレガシーなアプリケーションに置き換えて読めます。下の表は、モダナイゼーションの対象になりやすいスタックの例です。自分のアプリケーションに近い行から、このリポジトリの検証内容（段階 0〜3 とコンテナ分岐）と、モダナイゼーションの手段（AIMF、AWS Transform）へたどってください。

各リンクは外部の `documented` な例であり、このリポジトリの検証済みカバレッジを拡張しません。特定の製品やベンダーの優劣を示すものでもなく、読者が自分のスタックを識別するための入口です。

| レガシーの例 | 典型的なモダナイゼーション | 参考リンク（外部） | このリポジトリでの対応する検証 |
|---|---|---|---|
| Java: Struts / JSP の Web アプリ | Spring MVC / Spring Boot へ再アーキテクチャ | [Struts から Spring MVC への再設計](https://learn.microsoft.com/ja-jp/azure/developer/java/migration/github-copilot-app-modernization-for-java-rearchitecture)、[移行先に Spring を選ぶ背景](https://www.stylez.co.jp/legacy_columns/struts_to_spring/why_spring_is_the_best_choice_for_migration_from_struts_framework/) | [段階 2（Linux の新しいランタイムへ）](docs/ja/stage-2-aimf-modern-dotnet.md) |
| Java: J2EE / EJB / WebSphere / WebLogic のモノリス | サービス分解、コンテナ化 | [Java EE アプリのコンテナ化（AWS 指針）](https://docs.aws.amazon.com/prescriptive-guidance/latest/modernization-containerize-javaee/introduction.html)、[EJB モノリスの分解事例](https://bhavansh.hashnode.dev/decomposing-a-legacy-ejb-monolith-a-system-design-deep-dive) | [コンテナ分岐](docs/ja/branch-containerization.md) |
| .NET Framework / ASP.NET のモノリス（Windows 依存） | .NET（Linux）へ、またはコンテナへ | [.NET Framework モノリスのモダナイゼーション経路](https://aws.amazon.com/jp/blogs/architecture/modernization-pathways-for-a-legacy-net-framework-monolithic-application-on-aws/)、[ASP.NET のコンテナ化](https://aws.amazon.com/jp/blogs/modernizing-with-aws/containerize-asp-net-application-using-aws-migration-hub-orchestrator/)、[.NET モダナイゼーションの指針](https://docs.aws.amazon.com/wellarchitected/latest/microsoft-workloads-lens/scenario-2-.net-application-modernization-on-aws.html) | [段階 0〜3 の記録](docs/ja/stage-0-smb-ntfs.md) |
| COBOL / メインフレーム / 保守期限切れプラットフォーム | 再プラットフォーム、モノリス分解 | [レガシーシステムのモダナイゼーション](https://www.lastingdynamics.com/blog/legacy-system-modernization-guide/)、[レガシーコードの移行（IBM）](https://www.ibm.com/think/topics/legacy-code-migration) | [文書の索引](#文書の索引) |
| ローカルファイルシステム / ファイル共有に依存するアプリ | コンテナ化し、共有ストレージを永続ボリュームに | [レガシーアプリのコンテナ化の勘所](https://oneuptime.com/blog/post/2026-02-12-containerize-legacy-applications-for-aws/view)、[コンテナは再起動で状態を失う（永続ストレージが要る）](https://kubernetes.io/docs/concepts/storage/volumes/)、[NAS を Kubernetes に繋ぐ 3 経路（NFS / SMB / iSCSI）](https://oneuptime.com/blog/post/2025-12-15-how-to-use-nas-storage-with-kubernetes/view) | [コンテナ分岐](docs/ja/branch-containerization.md) |

ファイル共有に依存するレガシーなアプリケーションは、コンテナ化すると自身のファイルシステムの状態を再起動で失うため、永続ボリュームが要ります。このとき FSx for ONTAP を Kubernetes の永続ボリュームにすると、Pod の再スケジュール後もデータが追従します（[Kubernetes の永続ボリューム](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)）。この経路の構成とテンプレートは [コンテナ分岐](docs/ja/branch-containerization.md) と隣の Spoke にあります。

モダナイゼーションの手段は、下の「関連する責務」にある AI Modernization Flow（AIMF）と AWS Transform です。どのレガシーの例でも、データを FSx for ONTAP に残したまま、アプリケーション側と到達プロトコルを 1 段ずつ変える、という本リポジトリの型に当てはめて読めます。

### ブロックストレージ起点の検討（documented）

上の例はファイルアクセス前提ですが、入口はファイル共有だけではありません。オンプレミスで FC / iSCSI のブロックストレージ上で動いているワークロード（データベース、SQL Server FCI など）も、FSx for ONTAP へ移すことでモダナイゼーションと最適化を検討できます。

- モダナイゼーション（データ活用）: ブロックからファイルアクセスへ変えると、同じボリュームに対してマルチプロトコル（iSCSI / NFS / SMB / S3 Access Points）が成立します。S3 API のユーザーマッピングで S3 Access Points 経由のデータ活用（分析・AI 連携）へ広げられます。
- 最適化（データ保護・可用性・疎結合）: アクセスパターンに応じた階層化（FabricPool）、Snapshot / SnapMirror / FlexClone によるデータ保護、Multi-AZ の iSCSI マルチパスによる高可用性、FlexClone による非破壊テストでの疎結合・可搬性を検討できます。

これは `documented` な検討であり、本リポジトリで実機 verified にした経路ではありません（検証済みはファイル起点の `DocIntake` に限ります）。前提となる機能は、AWS Transform のストレージ Assessment が File & Block に対応し FSx for ONTAP を推奨すること（[ストレージの評価と移行](https://aws.amazon.com/jp/blogs/migration-and-modernization/assess-and-migrate-storage-to-the-cloud-with-aws-transform/)）、ブロックストレージ移行が EC2 と FSx for ONTAP をサポートすること（[AWS Transform の FSx for ONTAP サポート（プレビュー）](https://aws.amazon.com/jp/about-aws/whats-new/2026/06/aws-transform-vmware-fsx-for-ontap-preview/)）です。

ブロックストレージ移行の手順、データ保護・不変性、分析・AI 活用の詳細は、それぞれの責務を持つ隣の Spoke にあります（下の「関連する責務」を参照）。本リポジトリはアプリケーション側の移行を扱い、ブロック移行の実装は置きません。

## 現在の検証済みカバレッジ

現在の検証済みカバレッジは、1 つの `DocIntake` コンソールアプリと、その記録された実行経路に限ります。.NET Framework 4.8 / Windows / SMB を開始点とし、段階 2 では旧 Windows ビルドと近代化後の Linux ビルドを比較します。段階 2 は Windows .NET 10 の検証ではありません。段階 3 では .NET 10 managed-runtime Lambda と FSx for ONTAP S3 Access Points の経路を記録しています。次の表を、このリポジトリの唯一の完全な段階詳細表とします。

各段階は前の段階の構成を前提にし、ボリュームのデータは移動しません。Windows のクライアントが残っている間は、ボリュームのセキュリティスタイルを NTFS のまま維持します。

| 段階 | アプリケーションの実行環境 | アクセスプロトコル | 主な変更点 |
|---|---|---|---|
| 0 | .NET Framework 4.8 on Amazon EC2（Windows） | SMB（`ntfs` セキュリティスタイル、SVM は AWS Managed Microsoft AD に参加） | 出発点の構成を再現する |
| 1 | 段階 0 と同じ `DocIntake` | SMB に加えて NFS（マルチプロトコル） | 同じ `appdata` ボリュームを NFS からも読み書きする |
| 2 | .NET 10 on Amazon EC2（Linux） | NFS | 旧 Windows ビルドと近代化後の Linux ビルドを比較する。Windows .NET 10 の検証ではない |
| 3 | .NET 10 managed-runtime Lambda（`dotnet10`） | FSx for ONTAP S3 Access Points | 手動 invocation で一部の処理を扱う。EventBridge Scheduler は無効化され、file-locking は現行スライスでスキップする |
| 分岐 | コンテナ | NFS / SMB / S3 Access Points | 将来のナビゲーションと責務分担を示す分岐。コンテナ runtime は `open/not measured` で、詳細は隣の Spoke を参照する |

## エビデンス境界

現在のエビデンスは、上記の `DocIntake` と、`ap-northeast-1` の検証専用 Single-AZ ファイルシステム、1 つの `appdata` ボリューム、`ntfs` セキュリティスタイル、ONTAP 9.19.1P2 と 9.19.1P3 の記録、および記録された実行経路にだけ適用します。`verified` は名前付きのスライスと環境で再現した実行、`field-observation` は再現を確認していない単発の観測、`documented` はリポジトリ内の成果物または引用した文書にある宣言でありランタイム実行ではなく、`open/not measured` は未実行、現在の記録で裏付けられていないもの、または検証済みスライスの外側を表します。

| 対象 | 証拠区分 | 記載範囲 |
|---|---|---|
| 段階 0〜3 の実行記録 | `verified` | `DocIntake` を対象にした実行記録。各段階の構成と未実行の操作は分けて扱います。 |
| ONTAP 9.19.1P2 の記録 | `verified` | 2026-10-07 の初回構築記録。 |
| ONTAP 9.19.1P3 の記録 | `verified` | 2026-10-11〜12 の再構築記録。P2 の記録とは別の履歴です。 |
| 単発の補助的な観測 | `field-observation` | 再現を確認していない観測。サービス上限や一般的な性能値へ一般化しません。 |
| テンプレートの宣言 | `documented` | EventBridge Scheduler はテンプレートで無効化された宣言ですが、実行していません。 |
| fake S3 テスト | `documented` | インメモリ fake を使うテストの事実です。実際の FSx for ONTAP S3 Access Points の挙動を証明しません。 |
| 未実行または適用範囲外 | `open/not measured` | この節の未一般化範囲に記載します。 |

現在の .NET エビデンスは、Java、Python、Node.js、Go、ASP.NET、ASP.NET Core、Web API、Web UI、ECS、EKS、Multi-AZ、`unix` または `mixed` セキュリティスタイル、その他の未検証ランタイム、トポロジー、アクセスパスには一般化しません。別のランタイムや構成を扱う場合は、そのパターン固有のエビデンスが必要です。

現在の記録は Windows .NET 10 の検証、S3 全般の file-locking の有無、production performance、SLO、service limit、production cost estimate を確立しません。サンプル実行と本番見積り、この検証環境と一般的なサービス上限、設計上の考慮事項と法務・コンプライアンス判断を分けて扱います。

| データ | 証拠区分 | 保存先 |
|---|---|---|
| アプリケーションの入力と出力 | `verified` | FSx for ONTAP の `appdata` ボリュームです。 |
| 集約した Probe JSON | `verified` | 別の artifacts バケットです。`appdata` の入力・出力とは分けています。 |
| Lambda のログ | `verified` | CloudWatch Logs です。 |

## 文書の索引

| 読みたいこと | 参照先 |
|---|---|
| 検証を自分の環境で再現する順序（前提、見積りと承認、段階 0 から 3、後片付け、再現できない範囲） | [再現手順](docs/ja/reproduce.md) |
| 段階 0: SMB と NTFS の出発点 | [段階 0](docs/ja/stage-0-smb-ntfs.md) |
| 段階 1: 同じボリュームへの NFS の追加 | [段階 1](docs/ja/stage-1-multiprotocol.md) |
| 段階 2: Linux 上の新しい .NET へのモダナイゼーション（AIMF のセッション記録を含む） | [段階 2](docs/ja/stage-2-aimf-modern-dotnet.md) |
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

## 今後のパターンロードマップ

将来のパターンは、独立して仕様化され、エビデンスがそろうまで `open/not measured` とします。各パターンは、固有の requirements、design、evidence ledger、verification conditions、publication criteria を持つ必要があります。現在の `DocIntake` の記録を別のパターンの証拠として再利用せず、サンプル実行と本番見積り、個別環境と一般的なサービス上限を分けて記録します。

## 関連する責務

旅程全体とリポジトリの関係図は Hub の旅程マップだけが持ちます。以下のリンクはナビゲーションと責務の所有範囲を示す `documented` なコンテキストであり、リンク先の記載はこの Spoke の検証済みカバレッジを拡張しません。AIMF はアプリケーション・モダナイゼーションのワークフロー境界です。この Spoke は、AIMF が扱わない FSx for ONTAP 側のストレージ、プロトコル、権限、エビデンス境界を補足します。

[AI Modernization Flow](https://github.com/aws-samples/sample-ai-modernization-flow)（[日本語版ドキュメント（原本）](https://github.com/aws-samples/sample-ai-modernization-flow/tree/main/ja)、[紹介ブログ](https://aws.amazon.com/jp/blogs/news/aidm-introducing-ai-modernization-flow/)）は、AI エージェントに決められた段階でアプリケーションの移行を進めさせるワークフローです。Method（移行方法論）・Playbook（ドメイン固有の知見）・Harness（AI 実行規律）の 3 階層で、レガシーなアプリケーションのアセスメントから移行までを進めます。AIMF が作る作業記録のリポジトリは公開しない作業領域に置き、公開するのは結果の抜粋だけです。2026-10-12 に実施したセッションの手順、ADR の要旨、ハーネスが機械的に止める範囲と止めない範囲は[段階 2](docs/ja/stage-2-aimf-modern-dotnet.md)にあります。

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
