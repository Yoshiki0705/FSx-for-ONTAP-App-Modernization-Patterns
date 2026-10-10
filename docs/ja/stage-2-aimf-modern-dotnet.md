# 段階 2: Linux 上の新しい .NET への移行

> 証拠区分: `hypothesis`（未測定）。手順は計画で、予想は実測前の仮説である。実測記録で置き換える。

AI Modernization Flow（以下 AIMF、v0.11.0）の `dotnetfw-to-modern-dotnet` playbook で、
移行元アプリを新しい .NET に移し、Linux EC2 上で NFS 経由に動かす。
Windows クライアントを段階 3 まで残すので、セキュリティスタイルは NTFS のまま維持する。

## 手順（予定）

1. フックの配線を再検査し、`fsxadmin` を段階 2 の間だけ Linux のインスタンスロールから読めなくする
2. AWS Transform custom の 2 つの変換（分析と、比較のための .NET の変換）を、唯一の入口（`run-atx.sh`）から別々の承認で 1 回ずつ実行する
3. AIMF の Phase 0a〜4 を進め、合流点を組み込む
4. 移行後のコードを配置し、Probe を実行する
5. セキュリティスタイルの ADR を用意し、人の決定を待つ（この検証では切り替えない）

## AIMF セッションの実行エンジン

AIMF のセッションは kiro-cli 2.28.0 の既定エンジン（V2）を対話モードで使う。
2026-10-10 に macOS 上の kiro-cli 2.28.0 で、フックの発火を確かめる canary を走らせて次を確認した。

- V2 は agent-config に書いたフックだけを読む。matcher は正確なツール名なら発火し、
  正規表現（`^(execute_bash|shell|use_aws|aws)$`）と `execute_bash|use_aws` では一度も発火しなかった。
  matcher を JSON のリストにすると、エージェント自体を読み込めなかった。
  `setup-workspace.sh` はツール名を 1 つずつ書いた matcher を配線する
- フックが exit 2 を返すと、V2・V3 のどちらでもツール呼び出しが止まった
- V2 の非対話モードは `--trust-tools=execute_bash,use_aws` を付けても shell の実行を拒否した。
  原因は確認できていないため、非対話モードは使わない
- V3（`--v3`）は agent-config・ワークスペース・グローバルの 3 か所のフックを読み、正規表現の matcher でも発火したが、
  `use_aws` ツールを持たない。AWS 呼び出しを止めるフックの対象が V2 と変わるので、この検証では使わない

## AWS Transform custom の 2 つの変換と比較の計画

> 証拠区分: `hypothesis`（未測定）。どちらの変換もまだ実行していない。

段階 2 の移行そのものは AIMF の playbook で進める。
AWS Transform custom の AWS 管理の変換は 2 つを使い、`AWS/dotnet-modernization` の結果は比較のために記録する。

| 変換 | 段階 2 での役割 | 送るディレクトリ | コードの変更 |
|---|---|---|---|
| `AWS/comprehensive-codebase-analysis` | AIMF Phase 0a の分析 | `DocIntake/`（AIMF が変更しない分析用の複製） | しない。[Managed Transformations](https://docs.aws.amazon.com/transform/latest/userguide/transform-aws-customs.html) はこの変換を報告を作る分類に置く |
| `AWS/dotnet-modernization` | 比較 | `DocIntake-atx-dotnet/`（`app/legacy/` のコミット済みの状態から作った別の複製） | する。[How to work with the .NET agent](https://docs.aws.amazon.com/transform/latest/userguide/dotnet-work-with-agent.html) によれば、CLI は元のコードを置き換える |

2026-10-10 に macOS の作業端末で `atx` 3.18.0 の `atx custom def list --json` を実行し、ap-northeast-1 のレジストリに 2 つの変換が載っていることを確かめた。
変換の実行はしていない。

`AWS/dotnet-modernization` の CLI の形は、.NET の文書にある `atx custom def exec -n AWS/dotnet-modernization -p <path-to-solution> [-q] [-x] [-t]` に従う。
この形にビルドコマンド（`-c`）と設定ファイル（`-g`）はなく、既定の移行先は net10.0 である。
`run-atx.sh` はこの変換を、コミットが 1 つ以上あり未コミットの変更がない別の複製でだけ実行する。
分析用の複製と同じ場所を指す指定は拒否し、送ったコミットを実行記録に残す。

比べる項目は、書き換えた範囲、ビルドの結果、使った agent minutes の 3 つを予定している。

## agent minutes の上限と費用の上限

`run-atx.sh` は、承認した見積りに記録した上限を `atx custom def exec ... --limit <分>` に渡す。
上限と変換の名前は見積りからだけ読み、コマンドラインや環境変数で変える経路はない。
`atx custom def exec --help`（3.18.0、2026-10-10 に確認）によれば、`atx` は上限に達すると終了コード 2 で止まり、上限を上げて再開できる。
[AWS Transform の料金ページ](https://aws.amazon.com/transform/pricing/)は、中断した変換を 24 時間後まで再開できると書く。
`run-atx.sh` は終了コード 2 を受けると、上限までの分が課金されたものとして見積りを使用済みにし、終了コード 3 で止まる。
上限を上げるには、新しい見積り・承認・呼び出しの検証記録が要る。

| 変換 | `--limit` | 単価 | 費用の上限 | 実行前の見込み |
|---|---|---|---|---|
| `AWS/comprehensive-codebase-analysis` | 120 | $0.035 / agent minute | $4.20 | 未測定 |
| `AWS/dotnet-modernization` | 300 | 月間の無料枠の外では $0.035 / agent minute | $10.50（無料枠の外で実行された場合） | 月間 50,000 agent minutes の無料枠の中なら $0。枠の残りは見積りのスクリプトからは見えない |

単価は料金ページと AWS Price List API（サービス `AWSTransform`、使用タイプ `APN1-AgentMinute`、Asia Pacific (Tokyo)、適用開始 2026-04-01）で 2026-10-10 に確かめた。
課金の最小単位は 1 分で、料金ページは作業端末側のビルドやファイル読み取りを課金の対象外とする。

上限の根拠は次のとおり。どちらも実行前の仮説で、実行後に使った分と比べて見直す。

- サンプルは C# で約 900 行（`app/legacy/` の `.cs` の合計）
- `AWS/dotnet-modernization`: 料金ページの例（50k 行の .NET Framework で約 2,800 agent minutes、1 行あたり約 0.056）をこの規模に当てると約 50 分になる。評価と計画の固定分と、作業端末でビルドが通らない場合の繰り返しを見込んで、その 6 倍の 300 にした
- `AWS/comprehensive-codebase-analysis`: 料金ページにこの変換の例はない。同じページの例は 3,000〜17,000 行で 20〜72 agent minutes で、最大は 17,000 行の Java の言語バージョンの更新で約 72 agent minutes である。例のない変換なので、例の最大値を上回る 120 を判断として置いた

## 変換の選び方

| 観点 | `AWS/comprehensive-codebase-analysis` | `AWS/dotnet-modernization` |
|---|---|---|
| 得られるもの | コードベースの報告 | 書き換えたコードと、評価・計画・変換の報告 |
| 送ったコードへの影響 | 変更しない | 置き換えるので、元のコードを残す別の複製が要る |
| 費用 | 最初の 1 分から $0.035 / agent minute | 月間の無料枠の中なら $0。枠の残りが見えないので、上限は枠の外の単価で見積もる |
| 作業端末の前提 | ビルドコマンドなしの形が文書にある | macOS で .NET Framework 4.8 のコードがビルドされるかは未確認。.NET の文書は、Mac からの変換に Web アプリケーションを、手元でのビルドの確認に Visual Studio IDE を勧める |

現状のコードを変えずに把握したいときは前者、変換後のコードそのものを得たいときは後者が合う。
後者を CLI で使うときは、元のコードを残す複製と、ビルドを確かめられる環境を先に用意する。

## 境界で確認すること

- 対象ボリュームの UUID と `seed/` の目録が段階 0 と同一であること
- セキュリティスタイルが `ntfs` のままであること
- 回復キュー・クローン関係・`it_*` Snapshot が空であること
- Probe の差分を段階 0・1 と比べて記録すること

## 記録と公開の境界

AIMF の記録リポジトリ全体は非公開の作業領域に置く。公開するのは監査を通した表と要約の抜粋だけとし、
抜粋にはファイルシステム ID・アカウント ID・IP の置き換えと `make audit` を通す。

## 予想（未検証）

- AIMF がセキュリティスタイルとパス形式の変更を自律実行せず、人の判断で止まる見込み。
- 移行後の Linux では `GetAccessControl` 系が使えず、ACL の事前判定と NFS 経由の可否が食い違う見込み。
