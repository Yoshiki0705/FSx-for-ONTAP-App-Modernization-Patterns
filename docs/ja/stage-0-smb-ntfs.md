# 段階 0: SMB と NTFS の出発点

> 証拠区分: 節ごとに示す。「実測結果」「再構築環境での再計測」「撤回した主張」「この段階で判明した所見」は
> `verified`（run id と日付を本文に添えた。1 回だけの結果は「1 回の観測」と書いた）。「手順」と
> 「境界で確認すること」は計画の記述である。この段階で未測定の範囲は末尾の「未測定の範囲」に書く。

Windows EC2 上の .NET Framework 4.8 アプリが、NTFS セキュリティスタイルの対象ボリュームを SMB で
読み書きする出発点の構成を作り、Probe の 5 つの挙動を段階 0 の基準値として記録する。

## 手順

2026-10-07（ONTAP 9.19.1P2）と 2026-10-11（9.19.1P3）の 2 回の構築で、次の順に実施した。

1. 基盤スタックの作成（別文書「検証環境」）
2. ONTAP 側の設定（SMB 共有、NTFS ACL、`seed/`・`probe/`・`out/` の作成）
3. `seed/` に合成データを配置する
4. Linux EC2 に SMB をマウントする（2 台目の計測クライアント）
5. 移行元アプリをビルドして配置する
6. 目録と境界記録（`b0`）を取る
7. Probe を実行する（Windows と Linux の 2 台）

## 境界で確認すること

- SVM の CIFS ドメインに発見済みのドメインコントローラーがあること（`Lifecycle` だけで判定しない）
- 対象ボリュームのセキュリティスタイルが `ntfs` であること
- Probe の 5 件の `outcome` が揃い、2 台の組が `cross-host` であること（判定は `probe_merge.py` による。下の「撤回した主張」を参照）
- `seed/` の目録（相対パス、サイズ、SHA-256）が境界記録に含まれること

## 実測結果（verified）

> 証拠区分: 以下の箇条書きは `verified`。実測日 2026-10-07（run id `s0-20261007T104712Z`）、
> リージョン ap-northeast-1、ONTAP 9.19.1P2、Amazon FSx for NetApp ONTAP（以下 FSx for ONTAP）
> Single-AZ（1,024 GiB / 128 MBps）、NTFS ボリューム `appdata`、AWS Managed Microsoft AD（Standard）に
> 参加した SVM で実測した。ID と IP はマスクしてある（`fs-0123456789abcdef0` / `123456789012` /
> `10.0.x.x`）。各項目は 1 回の観測である。2026-10-11 の再構築環境での再計測は次の節に分けて書く。

- SVM の CIFS ドメインに発見済みのドメインコントローラーがある（`ms_dc` の `state=ok`）ことを
  ONTAP REST の `GET /api/protocols/cifs/domains/{svm-uuid}?fields=discovered_servers` で確認した。
  `active-directory` コレクション単独では 0 件を返し、判定に使えない。
- 対象ボリューム `appdata` のセキュリティスタイルは `ntfs`、`snaplock.type` は `non_snaplock`、
  `snapshot_locking_enabled` は false。SVM ルートボリュームも `non_snaplock` で、不可逆機能は
  いずれも無効。
- Windows の .NET Framework 4.8 アプリ（DocIntake）は Windows Server 同梱の MSBuild v4.8 で
  インターネットに出ずにビルドできた（SDK 形式へのフォールバックは不要だった）。
- Linux EC2 からの 2 台目の SMB マウントは `sec=ntlmssp` で接続できた（`sec=krb5` は不要）。
- 合成データ `seed/`（6 ファイル）を Windows から SMB で配置し、Windows の目録（相対パス・サイズ・
  SHA-256）を `b0` に採取した。`appsvc` は SMB 経由で全ファイルを読め、SHA-256 は配置元と一致した。
  NTFS ACL は `appsvc` に modify、`appreader` に read と書き込み拒否の ACE を与えた。
- 段階 0 では NFS の経路がないので、`b0` の目録は Windows 側の 1 つになった（`b0.json` の
  `inventories.linux` は空、name mapping は 0 件。2026-10-11 の再構築環境の `b0` で確認、ONTAP 9.19.1P3）。
- Probe の 1 台で完結する挙動のうち `path-separator` と `acl-evaluation` は、Windows（SMB）と
  Linux（SMB）の両方で `outcome=measured` だった。`path-separator` は Linux の SMB で
  `\` を含む名前が拒否され（`rejected=true`、`errno=22`）、その拒否自体を観測として記録した。
  これは「Linux では例外なく別名ファイルができる」という事前の予想とは異なる所見で、NFS を足す
  段階 1 との比較が主眼になる。
- 同じ Probe の `case-sensitivity` の記録は結論に使わない。参照 `Docs\Index.json` をボリュームの
  ルートに対して引いたが、実ファイルは `seed/docs/index.json` にあり、実在するファイルを同じ
  経路で引く陽性対照（`real`）が記録に無かった。大文字小文字を区別しない Windows（SMB）で
  `resolved=false` と記録されたのは、対照の不在で説明できる。陽性対照つきの再計測は次の節に書く。
  陽性対照の無い計測は、結果が偽でも「区別する」「見つからない」のどちらとも読めるため結論にならない。
  今後 Probe を取るときは、`real_resolved` が true の記録だけを `case-sensitivity` の結果として扱う。

## 再構築環境での再計測（verified、1 回ずつ）

> 証拠区分: `verified`。環境はすべて ap-northeast-1、ONTAP 9.19.1P3、Single-AZ、1 ファイルシステム、
> 1 SVM、1 ボリューム `appdata`（security style `ntfs`）。NFS の export を設定する前の状態で、
> 結果はいずれも 1 回の観測である。再現は未確認。

### SMB 同士の 2 台協調（run id `s0-20261011T065443Z`、2026-10-11）

Windows と Linux（cifs）の 2 台が同じ同期 ID（`s0-20261011T065443Z-p1`〜`p4`）を持ち、時刻の重なりが
両側の記録で確認できた組だけを `cross-host` とした。4 組すべてを `probe_merge.py` が `cross-host`
（`reasons` が空）と判定した。

| 組 | 保持側 / 競合側 | 保持の方法 | 保持中の競合側 | 解放後 |
|---|---|---|---|---|
| 1 | Windows / Linux（cifs） | `FileShare.None` | 読み取りと書き込みの open が拒否（`errno=16`） | 取得できた |
| 2 | Linux（cifs） / Windows | `fcntl.lockf LOCK_EX` | open は成功、読み取りはロック違反（`0x80070021`）、排他 open は共有違反（`0x80070020`） | 取得できた |

| 組 | 書き手 / 読み手 | 結果 | 保存完了から読めるまで |
|---|---|---|---|
| 3 | Windows / Linux（cifs） | 読めた | 81 ms |
| 4 | Linux（cifs） / Windows | 読めた | 8,122 ms |

- 読み手は 100 ms 間隔で一覧と読み取りを繰り返したので、値の分解能は約 100 ms である。性能の主張ではない。
- Linux が書き Windows が読む方向で約 8 秒、逆方向で 100 ms 未満という差は、段階 1 の 2026-10-07 の run でも現れた
  （[段階 1](stage-1-multiprotocol.md)）。原因は調べていない。
- 読者が取る行動: Windows のクライアントが Linux の書いたファイルを読む設計では、書き込み直後に読める
  ことを前提にせず、読めるまでの遅れを許す作りにする（待機やリトライを置く）。ここで測った遅れは 1 つの構成での
  値で、遅れの長さを他の構成に当てはめない。

### 大文字小文字の扱い（Windows SMB、陽性対照つき。run id `s4c-20261012T034403Z`、2026-10-12）

参照を `seed/Docs/Index.json` の綴り違い、陽性対照を実在の `seed/docs/index.json` にして Windows（SMB、
UNC 共有）から引いた。誤った綴りの参照も解決し（`resolved=true`）、陽性対照も解決した
（`real_resolved=true`）。SMB では大文字小文字を区別しない。1 回の観測。Linux の cifs と NFS を含む経路別の
結果は[段階 1](stage-1-multiprotocol.md)に書く。

### この環境で SMB の結果から言えない範囲

- 他の ONTAP の版、Multi-AZ、他のボリューム構成、複数 SVM は測っていない。
- 2026-10-07 の run（9.19.1P2）と 2026-10-11 の run（9.19.1P3）は別の構築で、版が違う。
  この文書は、版の違いが結果を変えたかどうかを調べていない。

## 撤回した主張

- 2026-10-07 の段階 0 の記録は、2 台のクライアントを要する `file-locking` と `write-visibility` についても
  `observed.topology=cross-host` と書いていた。この主張は撤回する。その run の Probe では各ホストが
  自分のファイルを独立にロック・書き込みしただけで、2 台の間の相互作用（一方がロック中の他方の
  アクセス、一方の書き込みが他方に見えるまでの時間）は測っていない。`cross-host` は結果の統合時に
  付けたラベルで、観測ではなかった。
- `topology` は、1 台だけで動かした Probe でも固定で出力される自己申告のラベルで、2 台が協調した証拠ではない。
  2 台の協調と読めるのは、`probe_merge.py` が同期 ID の一致と時刻の重なりを確かめ、`cross-host`（`reasons` が空）と
  判定した組だけである。読者が取る行動は、`topology` の値ではなく統合後の `reasons` が空かどうかで判定すること。
- 2 台の組を同期させた計測は、2026-10-07 の段階 1 で初めて行った（[段階 1](stage-1-multiprotocol.md)）。その環境には
  NFS の export があったので、その run の SMB 同士の組は段階 0 の基準値には読み替えない。
  SMB だけの構成での 2 台協調の基準は、NFS の export を設定する前の 2026-10-11 の run
  `s0-20261011T065443Z`（上の「SMB 同士の 2 台協調」）で得た。この基準は 1 回ずつの観測である。

## この段階で判明した所見（手順への反映）

- ONTAP REST の共有 ACL・file-security・`files` の各エンドポイントは、パスに SVM 名・ボリューム名
  ではなく UUID を要求する。NTFS ACL は `apply_to` に `this_folder`/`sub_folders`/`files` を
  指定しないと、子ファイルへ継承された ACE がフォルダ限定になり、一覧はできるが内容の読み取りが
  拒否される。
- FSx for ONTAP の管理エンドポイントの証明書 CN は管理 DNS 名で、スクリプトは管理 IP で到達する
  ため、ONTAP REST の `curl` には TLS 検証の調整が要る。
- SSM Run Command は SYSTEM で動くため、共有へは `appsvc` の SMB セッションを張ってから読み書き
  する。
- Probe の統合結果で 2 台の協調を読むときは、`topology` ではなく `probe_merge.py` の判定を使う（上の「撤回した主張」）。

## 未測定の範囲

次は、この段階では測っていない。結論に使わない。

- security style `unix` と `mixed` での挙動
- ONTAP 9.19.1P2・9.19.1P3 以外の版
- Multi-AZ、複数 SVM、1 ボリュームを超える構成
- 請求額（費用の見積りは[検証環境](environment.md)にあり、請求額ではない）
