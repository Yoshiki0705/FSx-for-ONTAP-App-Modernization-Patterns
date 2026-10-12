# 段階 2: Linux 上の新しい .NET への移行

> 証拠区分: 節ごとに示す。実行した結果の節（AIMF セッション、AWS Transform custom の 2 つの変換、近代化後アプリの計測、
> 実装の落とし穴）は `verified` で、run id と日付を本文に添えた。1 回だけの結果は「1 回の観測」と書いた。
> ADR の内容はセッション記録の要約である。「予想」の節は `hypothesis` を含み、項目ごとに区分を書いた。
> 環境は特記がなければ ap-northeast-1、ONTAP 9.19.1P3、Single-AZ、1 ボリューム `appdata`（security style `ntfs`）。

AI Modernization Flow（以下 AIMF、v0.11.0）の `dotnetfw-to-modern-dotnet` playbook で、
移行元アプリを新しい .NET に移し、Linux EC2 上で NFS 経由に動かす。
Windows クライアントを段階 3 まで残すので、セキュリティスタイルは NTFS のまま維持する。

## 手順と実施状況

| # | 手順 | 実施状況 |
|---|---|---|
| 1 | フックの配線を再検査し、`fsxadmin` を段階 2 の間だけ Linux のインスタンスロールから読めなくする | 2026-10-12 に kiro-cli 2.29.1 で canary を実行した（次の節）。`fsxadmin` の遮断は、境界記録を取るときだけ解除した |
| 2 | AWS Transform custom の 2 つの変換（分析と、比較のための .NET の変換）を、唯一の入口（`run-atx.sh`）から別々の承認で 1 回ずつ実行する | 2026-10-11 に実行した（「AWS Transform custom の 2 つの変換の結果」） |
| 3 | AIMF の Phase 0a〜4 を進め、合流点を組み込む | 2026-10-12 に 1 セッションで実施した（「AIMF セッションの実績」） |
| 4 | 移行後のコードを配置し、Probe を実行する | 2026-10-12 に実施した（「近代化後アプリの計測」） |
| 5 | セキュリティスタイルの ADR を用意し、人の決定を待つ（この検証では切り替えない） | `ntfs` を維持すると利用者が 2026-10-12 に決めた（「セキュリティスタイルの決定」） |

## AIMF セッションの実行エンジン

AIMF のセッションは kiro-cli の既定エンジン（V2）を対話モードで使った。

- 2026-10-10: macOS 上の kiro-cli 2.28.0 で、フックの発火を確かめる canary を走らせた。
- 2026-10-12: kiro-cli 2.29.1 で同じ canary をセッション開始前に走らせた。

2.28.0 の canary で次を確認した。

- V2 は agent-config に書いたフックだけを読む。matcher は正確なツール名なら発火し、
  正規表現（`^(execute_bash|shell|use_aws|aws)$`）と `execute_bash|use_aws` では一度も発火しなかった。
  matcher を JSON のリストにすると、エージェント自体を読み込めなかった。
  `setup-workspace.sh` はツール名を 1 つずつ書いた matcher を配線する
- フックが exit 2 を返すと、V2・V3 のどちらでもツール呼び出しが止まった
- V2 の非対話モードは `--trust-tools=execute_bash,use_aws` を付けても shell の実行を拒否した。
  原因は確認できていないため、非対話モードは使わない。この観測は 2.28.0 のもので、2.29.1 では再確認していない
- V3（`--v3`）は agent-config・ワークスペース・グローバルの 3 か所のフックを読み、正規表現の matcher でも発火したが、
  `use_aws` ツールを持たない。AWS 呼び出しを止めるフックの対象が V2 と変わるので、この検証では使わない

2.29.1 の canary（2026-10-12、1 回の観測）の結果は次のとおり。

| 呼び出し | フックの判定 |
|---|---|
| 無害な shell コマンド | allow |
| ブロック対象の shell コマンド | block（コマンドは実行されず、合言葉は出力されなかった） |
| `use_aws` | allow |

再現したい読者のための手順の要点は次のとおり。

- 起動は V2 の対話モードで、AWS の資格情報を環境変数で隔離する。`AWS_PROFILE` を外し、`AWS_CONFIG_FILE=/dev/null`、
  `AWS_SHARED_CREDENTIALS_FILE=/dev/null`、`AWS_EC2_METADATA_DISABLED=true` を渡した。隔離後の
  `use_aws sts get-caller-identity` は NoCredentials で失敗した（1 回の観測）。
- 承認プロンプトは 3 択（単発の許可 / セッション中の常時許可 / 拒否）で、常時許可は選ばず単発だけを選んだ。許可リストにある
  読み取りと記録の操作は承認用のスクリプトが単発で承認し、それ以外は止めて利用者が判断した。承認用スクリプトの
  内容と承認の件数はこの文書に書かない。

## AIMF セッションの実績

2026-10-12 の 1 セッションの結果を記録に基づいて示す（1 回の実行）。

- Phase 0a は、2026-10-11 に実行済みの AWS Transform custom の分析（`AWS/comprehensive-codebase-analysis`）の取り込みで、
  セッション内で `atx` は実行していない。
- 設計判断は ADR-1〜5 に残った。

| ADR | 決定の要旨 |
|---|---|
| ADR-1 | 移行先は .NET 10（LTS、`net10.0`）。ストレージは NFS に加え S3 Access Points の 2 経路を対象にする。Lambda のハンドラと配置は AIMF の範囲外 |
| ADR-2 | 非機能の 4 項目のうち、性能とリソースリークは対象外、障害耐性とセキュリティは限定して対象にする |
| ADR-3 | `CanWrite` の NTFS ACL 事前判定は、Windows では現行の評価を維持し、Linux では POSIX の mode ビットではなく実 I/O の成否で判定する |
| ADR-4 | `index.json` はコンパクトな JSON（`System.Text.Json` の既定）にする。成功基準の検証は byte-for-byte から JSON 同値（キー・値・配列順）に改める |
| ADR-5 | S3 での `OpenExclusive` は `NotSupportedException` のままにして、台帳 AP-14 で追跡する。S3 に排他オープンの操作が無いため |

- 独立検証は 1 回。機能欠落のレンズは REFUTED、スコープのレンズは UPHELD（finding 1 件）。finding は ADR-5 と AP-14 で処理し、繰り越しは 0 件だった。
- 否定テスト: 検出器に一時的な違反ファイルを注入して検出されることを AP-1〜13 で確かめた（13 件が検出、検出できなかったものは 0 件、2026-10-12）。
- Kiro のクレジットは、このセッションで約 284 を使った。上限は利用者が 5,000 に設定した。クレジットと金額の換算は
  この文書では行わない。

## ハーネスが機械的に止める範囲と止めない範囲

| 区分 | 内容 |
|---|---|
| 機械的に止める | WORM 系の不可逆操作の変更（`guard_irreversible_ops.py`）。`run-atx.sh` を通らない `atx` の直接実行（`block_direct_atx.py`）。canary でフックの発火を確認した（allow / block / allow） |
| 機械的には止めない | 不可逆操作以外の AWS の変更全般。`git push`（フックは無い） |
| 運用で止めている | 上記 2 行目の範囲は、利用者の指示文、資格情報の隔離、承認用スクリプトの許可リスト、記録リポジトリに remote を設定しないこと、で制約している。フックが一般の `aws` コマンドを止めるわけではない |

隔離が効いていることを確かめたのは、上の 1 回の canary の範囲である。他のツールや他の呼び出し方に隔離が効くかは確かめていない。

## AWS Transform custom の 2 つの変換と比較の位置づけ

段階 2 の移行そのものは AIMF の playbook で進める。
AWS Transform custom の AWS 管理の変換は 2 つを使い、`AWS/dotnet-modernization` の結果は比較のために記録する。
本線の移行は AIMF で、`AWS/dotnet-modernization` の出力を移行に使ってはいない。

| 変換 | 段階 2 での役割 | 送るディレクトリ | コードの変更 |
|---|---|---|---|
| `AWS/comprehensive-codebase-analysis` | AIMF Phase 0a の分析 | `DocIntake/`（AIMF が変更しない分析用の複製） | しない。[Managed Transformations](https://docs.aws.amazon.com/transform/latest/userguide/transform-aws-customs.html) はこの変換を報告を作る分類に置く |
| `AWS/dotnet-modernization` | 比較 | `DocIntake-atx-dotnet/`（`app/legacy/` のコミット済みの状態から作った別の複製） | する。[How to work with the .NET agent](https://docs.aws.amazon.com/transform/latest/userguide/dotnet-work-with-agent.html) によれば、CLI は元のコードを置き換える |

2026-10-10 に macOS の作業端末で `atx` 3.18.0 の `atx custom def list --json` を実行し、ap-northeast-1 のレジストリに 2 つの変換が載っていることを確かめた。

`AWS/dotnet-modernization` の CLI の形は、.NET の文書にある `atx custom def exec -n AWS/dotnet-modernization -p <path-to-solution> [-q] [-x] [-t]` に従う。
この形にビルドコマンド（`-c`）と設定ファイル（`-g`）はなく、既定の移行先は net10.0 である。
`run-atx.sh` はこの変換を、コミットが 1 つ以上あり未コミットの変更がない別の複製でだけ実行する。
分析用の複製と同じ場所を指す指定は拒否し、送ったコミットを実行記録に残す。

## AWS Transform custom の 2 つの変換の結果

> 証拠区分: `verified`。2026-10-11、ap-northeast-1、atx 3.18.0。どちらの変換も 1 回ずつの実行で、
> 同じサンプルアプリ（`.cs` が約 900〜1,089 行）に対する値である。

変換後のコードを Linux で動かした記録の run id は `s2-20261011T125351Z`、`s2f-20261011T152320Z`、`s2g-20261011T154203Z` である。
費用は agent minutes に単価を掛けた見積りで、請求額ではない。

| 項目 | `AWS/comprehensive-codebase-analysis` | `AWS/dotnet-modernization` |
|---|---|---|
| 実行（UTC） | 09:48〜10:03、exit 0 | 11:05〜11:25、exit 0 |
| agent minutes | 92.46 / 120（`--limit` に対して 77%） | 139.00 / 300（46%） |
| 費用（見積り） | 約 $3.24（92.46 × $0.035） | 無料枠内なら $0。枠外なら約 $4.87。枠の残りは観測できない |
| コードの変更 | なし（`ATXDocumentation` に 28 ファイルを生成、`.cs` は変更なし） | 11 ファイル、+55 / −162 |
| 内容 | 技術的負債の報告。推奨変換は `AWS/dotnet-modernization` | `net10.0`、SDK 形式の csproj、`ConfigurationManager` から `IConfiguration`、`App.config` から `appsettings.json`、`AssemblyInfo` の削除 |
| ビルド | 対象外（コードを変更しない） | macOS の作業端末で `dotnet build` が成功（エラー 0、CA1416 の警告 7 件） |

## agent minutes の上限と費用の上限

`run-atx.sh` は、承認した見積りに記録した上限を `atx custom def exec ... --limit <分>` に渡す。
上限と変換の名前は見積りからだけ読み、コマンドラインや環境変数で変える経路はない。
`atx custom def exec --help`（3.18.0、2026-10-10 に確認）によれば、`atx` は上限に達すると終了コード 2 で止まり、上限を上げて再開できる。
[AWS Transform の料金ページ](https://aws.amazon.com/transform/pricing/)は、中断した変換を 24 時間後まで再開できると書く。
`run-atx.sh` は終了コード 2 を受けると、上限までの分が課金されたものとして見積りを使用済みにし、終了コード 3 で止まる。
上限を上げるには、新しい見積り・承認・呼び出しの検証記録が要る。

| 変換 | `--limit` | 単価 | 費用の上限 | 実行前の見込み | 実績（2026-10-11、1 回） |
|---|---|---|---|---|---|
| `AWS/comprehensive-codebase-analysis` | 120 | $0.035 / agent minute | $4.20 | 未測定 | 92.46 分 |
| `AWS/dotnet-modernization` | 300 | 月間の無料枠の外では $0.035 / agent minute | $10.50（無料枠の外で実行された場合） | 月間 50,000 agent minutes の無料枠の中なら $0。枠の残りは見積りのスクリプトからは見えない | 139.00 分 |

単価は料金ページと AWS Price List API（サービス `AWSTransform`、使用タイプ `APN1-AgentMinute`、Asia Pacific (Tokyo)、適用開始 2026-04-01）で 2026-10-10 に確かめた。
課金の最小単位は 1 分で、料金ページは作業端末側のビルドやファイル読み取りを課金の対象外とする。

上限の根拠は実行前の仮説で、実行後に実績と比べた。

- サンプルは C# で約 900 行（`app/legacy/` の `.cs` の合計）。分析変換は約 900〜1,089 行で 92.46 分だった。この値は 1 回の観測で、他のサンプルには当てはめない
- `AWS/dotnet-modernization`: 料金ページの例（50k 行の .NET Framework で約 2,800 agent minutes、1 行あたり約 0.056）をこの規模に当てると約 50 分になる。評価と計画の固定分と、作業端末でビルドが通らない場合の繰り返しを見込んで、その 6 倍の 300 にした。実績は 139.00 分で、見込みの約 50 分を上回り、上限の 300 を下回った
- `AWS/comprehensive-codebase-analysis`: 料金ページにこの変換の例はない。.NET 以外の 3 つの例は 3,000〜17,000 行で 20〜72 agent minutes である（.NET の例は約 500,000 行で約 25,000 agent minutes と桁が違うため、小規模の目安には使わない）。例のない変換なので、小規模の例の最大値を上回る 120 を判断として置いた。実績は 92.46 分

## 変換の選び方

| 観点 | `AWS/comprehensive-codebase-analysis` | `AWS/dotnet-modernization` |
|---|---|---|
| 得られるもの | コードベースの報告 | 書き換えたコードと、評価・計画・変換の報告 |
| 送ったコードへの影響 | 変更しない | 置き換えるので、元のコードを残す別の複製が要る |
| 費用 | 最初の 1 分から $0.035 / agent minute | 月間の無料枠の中なら $0。枠の残りが見えないので、上限は枠の外の単価で見積もる |
| 作業端末の前提 | ビルドコマンドなしの形が文書にある | .NET Framework 4.8 のコードが macOS でビルドされるかは変換前に測っていない。変換後の `net10.0` のコードは macOS でビルドが成功した（2026-10-11）。.NET の文書は、Mac からの変換に Web アプリケーションを、手元でのビルドの確認に Visual Studio IDE を勧める |

現状のコードを変えずに把握したいときは前者、変換後のコードそのものを得たいときは後者が合う。
後者を CLI で使うときは、元のコードを残す複製と、ビルドを確かめられる環境を先に用意する。

## AWS Transform custom と AIMF の比較と選び方

> 同じサンプルアプリでの各 1 回の実行である。AIMF 側は分析・ADR・台帳という追加の入力を使っており、同じ条件の
> 比較ではない。単位の違う費用は金額に換算して並べない。

| 観点 | `AWS/dotnet-modernization`（atx） | AIMF（`dotnetfw-to-modern-dotnet`） |
|---|---|---|
| 費用の単位 | agent minutes（139.00 分、見積りは無料枠内なら $0、枠外なら約 $4.87） | Kiro のクレジット（約 284） |
| 実時間 | 約 20 分（11:05〜11:25 UTC） | 2026-10-12 の 1 セッション。人の承認と質疑の時間を含むので、実時間は並べない |
| 出力 | 11 ファイルの書き換え。5 つの埋め込み挙動が残った（次の節） | 台帳 AP-1〜14、ADR-1〜5、`NfsFileStore` と `S3FileStore`、独立検証の記録 |
| Linux での最初の実行 | 一部の挙動が `error`（`s2-20261011T125351Z`）。手直しののち `measured`（`s2f`、`s2g`） | Probe の記録 19 件がすべて `measured`、`error` は 0（`s2m-20261012T090934Z`） |
| 事前に要るもの | 元のコードを残す別の複製。ビルドを確かめられる作業端末 | セッションの設計、利用者の承認、資格情報の隔離、Kiro のクレジット |
| 制約 | 環境固有のストレージ層（NFS 向けなど）は作らない。課金は agent minutes | 人の判断と承認の時間が要る。クレジットを使う |

選び方は用途で分かれる。変換後のコードを 1 つのコマンドで得て、自分で手直しする前提なら atx の
`AWS/dotnet-modernization` が合う。移行の判断（ADR）と検証の記録を一続きで残したい、またはストレージ層を追加したいなら AIMF が合う。
どちらも、元のコードを残す複製と、結果を実環境で確かめる手順が要る。この文書は 1 つのサンプルでの観測で、
他のアプリの結果や費用を予測するものではない。

## 実装の落とし穴と修正

> `verified`、各 1 回。atx 由来のコードは `s2-20261011T125351Z`、`s2f-20261011T152320Z`、`s2g-20261011T154203Z`（2026-10-11）、
> AIMF 由来のコードは `s2m-20261012T090934Z`（2026-10-12）で確認した。

atx の `AWS/dotnet-modernization` が直さなかった 5 つと、読者が取る行動は次のとおり。

| # | 残ったもの | 読者が取る行動 |
|---|---|---|
| 1 | ACL の評価。`File.GetAccessControl` を `FileInfo.GetAccessControl` に置き換えただけで、Linux で動く形にはなっていない。Linux の実行では `PlatformNotSupportedException` になった | Linux では ACL の事前判定ではなく、実 I/O の成否で判断する |
| 2 | `StorePaths.Combine` が `\` で連結する。Linux では `/mnt/appdata\probe\...` が 1 つのファイル名になり、マウントの外の `/mnt` 直下（ローカルのファイルシステム）を指した | 区切り文字を文字列連結せず `Path.Combine` を使う。`measured` が共有への書き込みの成功を意味するとは限らないので、書き込み先を確かめる |
| 3 | `FileShare.None` | Linux の NFS・cifs 上でも排他が効くことを実測で確かめてから残す。ここでは意図的に維持した（台帳 AP-4） |
| 4 | `Docs\Index.json` への参照 | 大文字小文字と区切り文字の差は経路ごとに出る。参照を実在のファイルに固定し、陽性対照を持たせる |
| 5 | `NfsFileStore` が追加されていない（`SmbFileStore` のまま） | NFS 用のストア実装を別に用意する |

AIMF の側で直した 3 つは次のとおり。

- パスの連結を `Path.Combine` にした（台帳 AP-1）
- `CanWrite` を実 I/O の成否で判定する形にした（台帳 AP-3、ADR-3）
- case-sensitivity の参照を `seed/` 配下に固定し、陽性対照を記録に加えた（台帳 AP-2）

atx 由来のコードの手直し（2026-10-11、`s2f-20261011T152320Z`）では、`Path.Combine` 化、`NfsFileStore`、`CanWrite` の実 I/O 化を行い、
Linux（NFS と cifs）の 4 つの挙動が `measured` になった。cifs は `\` を含む名前を `errno=22`（`EINVAL`）で拒否し、
.NET がこれを `IOException` にするため、Probe の扱いを `s2g-20261011T154203Z` で変更した（`rejected=true` と `hresult` を記録）。

## 近代化後アプリの計測

> `verified`、各 1 回。run id `s2m-20261012T090934Z`、`s2n-20261012T104314Z`、`s0w-20261012T102314Z`（いずれも 2026-10-12）。
> ONTAP 9.19.1P3、Single-AZ、1 ボリューム。NFS の実行は UNIX uid 10001（root は匿名に潰され拒否される）。
> Windows 側の Probe は旧ビルド（.NET Framework 4.8）、Linux 側は近代化したビルド（net10.0）。

- Probe の記録 19 件（standalone 3、ペア 16）はすべて `measured`、`error` は 0 件だった。case-sensitivity の陽性対照
  （`real_resolved`）は Windows、Linux cifs、Linux NFS の 3 経路とも true だった。
- クロスホストのペア 8 組は、`probe_merge.py` がすべて `cross-host`（`reasons` が空）と判定した。file-locking の 4 組は
  contender が拒否され、解放後に成功した。write-visibility の 4 組は `seen: true` で、Windows 書き→Linux 読みが 66 ms と 35 ms、
  Linux 書き→Windows 読みが 7,968 ms と 8,342 ms だった。
- Worker は exit 0 で、レポート 5 件と `out/s2m/index.json` を書いた。旧 Windows Worker を同じボリュームで 1 回動かして作った
  `out/s0/index.json`（`s0w-20261012T102314Z`）と、キー・値・配列順が一致した（JSON 同値）。バイト列は 201 バイトと 181 バイトで、
  差は区切りの空白 20 バイトだけだった（ADR-4 で想定した差）。レポート 5 件の sha256 は一致し、`seed/` の 6 ファイルの個別ハッシュは実行の前後で変わらなかった。
  配列順は 5 ファイル・1 回の観測で、名前の昇順だった。`Directory.GetFiles` や SMB が名前順を保証するかは確かめていない。
- acl-evaluation の記録の形を ADR-3 に合わせた再計測（`s2n-20261012T104314Z`）では、Linux の NFS と cifs の両方で
  `precheck` が `skipped`、`precheck_reason` が非空、`io_can_write` が true になり、`precheck_can_write` は記録から消えた。
- 読者が取る行動: `index.json` の比較はバイト列ではなく JSON 同値で行う。出力が `System.Text.Json` の既定（コンパクト）に
  変わると、バイト列は変わるがキー・値・配列順は変わらない。

この節で測っていないもの: acl-evaluation の false 側（書けない場合の値は未観測。3 経路とも true の 1 回）、近代化 Probe による NFS の
`\` を含む名前の再観測（近代化 Probe は `\` を含む名前を書かないので、`exists_literal` は 2026-10-07 の Python 版の観測を引用するにとどまる）。

## セキュリティスタイルの決定

`ntfs` を維持する。decision: user（利用者が 2026-10-12 に決めた。AI は決めていない）。
この記述は設計判断であり、法務・コンプライアンス・規制の判断ではない。

| 観点 | `ntfs` を維持 | `unix` に切り替え | `mixed` に切り替え |
|---|---|---|---|
| 権限を変更できるクライアント | SMB | NFS | NFS または SMB。最後に変更したクライアントの型が有効になる |
| 向いている場面 | Windows クライアントが残る間。権限の正本を NTFS ACL に置き続けるとき | Windows クライアントを廃止した後。権限を UNIX の道具で管理するとき | 同じデータに両方の管理者が権限を書き込む必要があるとき |
| Linux 側の見え方 | `ls` のモード表示は可否を表さない（この環境で確認） | モードビットが可否を表す（文書上の一般論。この環境では未試験） | ファイルごとに型が混在する（文書上の一般論。この環境では未試験） |
| 制約 | Linux の `chmod`・`chown` は権限の変更手段にならない。Linux 側の可否は name mapping と Windows 主体の ACL に依存する | Windows クライアントが残ると ACL の管理手段が変わる。切り替え前後の権限の同値性は未検証 | 権限の型が混在し、有効な権限の判定がファイル単位になる。この環境での挙動は未試験 |

- 選び方: Windows クライアントが残り、権限の正本を NTFS ACL に置きたい間は `ntfs` が合う。Windows クライアントが無くなり UNIX の道具で
  権限を管理したいなら `unix` が合う。両方のプロトコルから権限を書き換える運用が必要なときに限り `mixed` を検討する。
- 区分: `ntfs` の経路別の挙動（大文字小文字、可視性の遅れ、事前判定、NFS の root の拒否）は `verified`（上の各節）。
  セキュリティスタイルの一般的な効果は `documented`（[Learn about ONTAP NAS security styles](https://docs.netapp.com/us-en/ontap/nfs-admin/security-styles-their-effects-concept.html)、2026-10-12 に開いて確認。
  要約であり、バージョン固有の差は確認していない）。`unix` と `mixed` をこの環境で試した結果は無く、この環境での挙動は `hypothesis`（未試験）。
- 経路別の差（大文字小文字、可視性の遅れ、事前判定）は、セキュリティスタイルを変えても解消するとは主張しない。試していないので言えない。
- 見直しの条件: 対象ボリュームを使う Windows クライアントが 0 台になったとき、権限の管理を Linux 側の道具に一本化したい要望が出たとき、
  マルチ AZ・別の ONTAP バージョン・別のボリュームを扱うとき、name mapping の維持が運用上の負担になったとき。
- 切り替えは、この検証の後の別手順として扱う。ボリュームにデータがある状態での切り替えと、既存の NTFS ACL の扱いは未試験。

## 境界で確認すること

- 対象ボリュームの UUID と `seed/` の目録が段階 0 と同一であること
- セキュリティスタイルが `ntfs` のままであること
- 回復キュー・クローン関係・`it_*` Snapshot が空であること
- Probe の差分を段階 0・1 と比べて記録すること

結果（`verified`）:

- 2026-10-11 の境界 `b2` は合格した（UUID・security style・`seed/` の目録・トップレベルのパスが `b0` と一致、
  `check-no-locking` も合格）。
- 2026-10-12 の `b2`（`s2m-20261012T090934Z`）と `b3` は、最上位に 8.3 短縮名 `PROBES~1.TXT` が現れ、`check-invariant.py` が失敗した。
  原因は[段階 3](stage-3-s3-access-points.md)の S3 Probe がボリューム直下に作ったファイルで、段階 3 で扱う。
  UUID、security style、`seed/` の目録、export policy、snapshot、snapshot locking の値は不変で、`check-no-locking.sh` は合格した。

## 費用の見積り

次は見積り（単価 × 数量）であり、請求額ではない。請求額は未観測で、Cost Explorer での確認は 2026-10-13 に行う。

| 対象 | 見積り | 備考 |
|---|---|---|
| 基盤（72 時間分の承認） | $57.71（承認 2026-10-11T02:07:07Z） | 破棄は約 33 時間後。33 時間分の金額はこの文書では計算しない（[検証環境](environment.md)） |
| atx の分析変換 | 約 $3.24 | 上限の見積りは $4.20 |
| atx の `AWS/dotnet-modernization` | 無料枠内なら $0、枠外なら約 $4.87 | 上限の見積りは $10.50。枠の残りは観測できない |

AIMF セッションの Kiro クレジット（約 284）は金額に換算していない。

## 記録と公開の境界

AIMF の記録リポジトリ全体は非公開の作業領域に置く。公開するのは監査を通した表と要約の抜粋だけとし、
抜粋にはファイルシステム ID・アカウント ID・IP の置き換えと `make audit` を通す。

## 予想と未検証の範囲

- AIMF がセキュリティスタイルとパス形式の変更を自律実行せず、人の判断で止まる見込み。`hypothesis`。セッションでセキュリティスタイルの
  切り替えは提案も実行もされなかったが、「止まった」ことを示す直接の記録は無い。
- 移行後の Linux では `GetAccessControl` 系が使えない見込み。これは `verified`（atx 由来のコードで `PlatformNotSupportedException`、
  `s2-20261011T125351Z`、1 回）。ACL の事前判定と NFS 経由の実際の可否が食い違う見込みは `hypothesis` のまま。
  `io_can_write` が true の 3 経路で NFS の実書き込みと一致したが、書けない場合（false 側）は未観測。
- 未測定: security style `unix` と `mixed`、ONTAP 9.19.1P2・9.19.1P3 以外の版、Multi-AZ、他のボリューム構成、`Directory.GetFiles` の返却順の保証、
  Linux が書き Windows が読む約 8 秒の遅れの原因、請求額。
