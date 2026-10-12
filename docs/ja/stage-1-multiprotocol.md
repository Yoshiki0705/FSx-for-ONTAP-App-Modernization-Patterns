# 段階 1: 同じボリュームへの NFS の追加
>
> 証拠区分: 「実測結果（verified）」「再現（verified）」「実測で判明した所見」の節は `verified`。
> run id と日付を本文に添えた。1 回だけの結果は「1 回の観測」と書いた。「手順」と「境界で確認すること」は
> 計画の記述、「未検証の仮説」は `hypothesis` である。
段階 0 の対象ボリュームに NFS のアクセスを足す。ボリュームの複製・作り直し・データの移動はしない。
セキュリティスタイルは NTFS のまま維持する。

## 手順

2026-10-07（ONTAP 9.19.1P2）と 2026-10-11（9.19.1P3）の 2 回の構築で、次の順に実施した。

1. ONTAP 側で export policy と name mapping（両方向）を設定する
2. Linux EC2 で NFS をマウントする
3. 目録（Windows と Linux の両側）と境界記録（`b1`）を取る
4. 単一ボリューム不変条件を確認する（`b0` と `b1`）
5. Probe を実行する（SMB と NFS の両方）

## 境界で確認すること

- 対象ボリュームの UUID が段階 0 と同一であること
- `seed/` の目録が Windows（SMB）と Linux（NFS）の両側で一致すること
- セキュリティスタイルが `ntfs` のままであること
- NFS からの拒否時に、Windows 側の有効なアクセス権と name mapping を併せて記録すること

## 実測結果（verified）
>
> 実測日 2026-10-07（run id `s1-20261007T150116Z`、2 台の協調は `s1-20261007T164155Z`）、
> リージョン ap-northeast-1、ONTAP 9.19.1P2。Amazon FSx for NetApp ONTAP
> （以下 FSx for ONTAP）Single-AZ（1,024 GiB / 128 MBps）、NTFS ボリューム `appdata`、AWS Managed
> Microsoft AD（Standard）に参加した SVM。クライアントは Windows Server 2022 の EC2（SMB 3、
> DocIntake.Probe）と Amazon Linux 2023 の EC2（SMB は `cifs`・`sec=ntlmssp`、NFS は `vers=4.1`・
> `sec=sys`。いずれも `probe_peer.py`）の 2 台。ID と IP はマスクしてある（`fs-0123456789abcdef0` /
> `123456789012` / `10.0.x.x`）。時刻はこの 2 台の時計で、Amazon Time Sync Service に対する
> オフセットは Windows で 1.5〜2.1 ms、Linux で 0.01 ms 未満だった。

### ONTAP 側の設定と冪等性

- `stage1-nfs.sh` は 1 回目に export policy `appmod_nfs`（規則 1 本: クライアントはプライマリ
  サブネットの CIDR、`nfs4`、`ro_rule`/`rw_rule` は `sys`、`superuser` は `none`）を作り、ボリューム
  UUID を指定して `appdata` に割り当てた（変更前は `default`）。UNIX ユーザー `appsvc`（uid 10001）と
  `appreader`（uid 10002）、name mapping 4 本（`win_unix` と `unix_win` を 2 ユーザー分）も作った。
- 2 回目の実行は全オブジェクトが「既存のため変更なし」で終了コード 0 だった。
- 変更の前と後でセキュリティスタイルは `ntfs`。ボリュームの複製・作り直し・移動はしていない
  （NFS を足すのに複製が要らないことは Hub の
  [adding-a-protocol-does-not-need-a-clone](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/adding-a-protocol-does-not-need-a-clone.md)
  にある）。
- name mapping の置換文字列 `APPMOD\\appsvc`（REST の JSON では `\\\\`）は、ONTAP の対応付けの
  結果として 1 文字の `\` を持つ Windows 名 `APPMOD\appsvc` になった。UNIX 名から Windows 名、
  Windows 名から UNIX 名のどちらの方向も、`appsvc` と `appreader` で期待どおりに解決した。
- スクリプトは既定ユーザーを設定していない。SVM には既定で CIFS の `default_unix_user` に `pcuser`
  が入っており、NFS 側の既定 Windows ユーザーは未設定だった。

### NFS マウントと単一ボリューム不変条件

- Linux EC2 から NFSv4.1（`sec=sys`）で `/appdata` をマウントできた。段階 0 の SMB マウントも同じ
  ホストに並べて残した。
- `b1` で、対象ボリュームの UUID は `b0` と同一だった。`seed/` の 6 ファイルの目録（相対パス、
  サイズ、SHA-256）は、Windows（SMB）からも Linux（NFS、uid 10001）からも `b0` と一致し、両者も
  互いに一致した。最上位のパスは `seed/`・`probe/`・`out/` だけで、セキュリティスタイルは `ntfs`。
- snapshot locking が有効なボリュームと SnapLock のボリュームはなかった（`appdata` と SVM ルート
  ボリュームを走査）。

### 1 台で完結する挙動（path-separator と acl-evaluation）

| 挙動 | Windows（SMB） | Linux（SMB） | Linux（NFS） |
|---|---|---|---|
| `path-separator`（`\` を含む名前の作成） | 区切りとして扱われる | 拒否（`errno=22`） | 作成できる。`\` は名前の一部になる |
| `acl-evaluation` | 事前判定は書き込み可 | 事前判定の手段なし | 事前判定の手段なし |

- NFS で作った `sep\check.txt` は、Windows からは 8.3 形式の短い名前 `SEPCHE~1.TXT` として見え、
  読めた。段階 0 で Linux の SMB が拒否した名前は、同じボリュームでも NFS からは作成できる。
- Windows と Linux（SMB）の `path-separator` と `acl-evaluation` は段階 0 と同じ観測だった
  （`compare-probe.py` の判定は `ok`）。`case-sensitivity` はこの比較の対象に含めない。比べた 2 件の記録は
  どちらも陽性対照が無く、無効な記録同士の一致は `ok` でも根拠にならない。Linux（NFS）は段階 0 に
  対応する経路がないので比較対象がない。
- `\` の扱いはプロトコルで違う。NFS のファイル名と S3 のキーでは `\` は普通の文字、Linux の cifs
  マウントは `errno=22` で拒否する。S3 のキーに `\` を入れてボリューム直下に書くと SMB 側の最上位に 8.3 短縮名が
  現れる罠は[段階 3](stage-3-s3-access-points.md)に書いた。読者が取る行動は、`\` を含む名前の作成を検証するとき、
  作る場所を `probe/` のような専用ディレクトリの下に限ること。

### 大文字小文字の扱い（経路別、陽性対照つき）

> run id `s4c-20261012T034403Z`、2026-10-12、ONTAP 9.19.1P3、Single-AZ、1 ボリューム（security style `ntfs`）。
> 経路ごとに 1 回の観測。陽性対照（実在する `seed/docs/index.json` を同じ経路で引く）が解決した記録だけを結果とした。

| 経路 | 参照（綴りを変えた `seed/Docs/Index.json`） | 陽性対照（`seed/docs/index.json`） |
|---|---|---|
| Windows（SMB） | 解決した（`resolved=true`） | 解決した（`real_resolved=true`） |
| Linux（cifs、`vers=3.0`、`sec=ntlmssp`） | 解決した（true） | 解決した（true） |
| Linux（NFS 4.1、`sec=sys`、uid 10001） | 解決しなかった（`resolved=false`、エラーによる拒否ではない） | 解決した（true） |

- 同じ ntfs ボリュームでも、SMB（Windows と cifs）は大文字小文字を区別せず、NFS は区別する。
- cifs の mount options に `nocase` のような大文字小文字の指定は無く、他のオプションは試していない。
  どのオプションが cifs の結果を決めたかは言えない。
- 読者が取る行動: アプリはファイル名の大文字小文字に依存しない作りにするか、経路ごとの差を前提にする。
  SMB で動いたコードが NFS で「ファイルが無い」になりうる。
- 2026-10-07 の run（`s1-20261007T150116Z`、ONTAP 9.19.1P2）には、別の設計の観測がある。NFS で作った
  `casecheck.txt` と `CaseCheck.txt` は、Windows からは `casecheck.txt` と `CASECH~2.TXT` の 2 つとして見え、
  読み戻した内容は別だった（それぞれ `l` と `u`）。NFS が大文字小文字の違う 2 つのファイルを作れることの観測で、
  上の表の値とは別の設計の計測である。1 回の観測。
- 段階 0〜2 の旧 Probe の `case-sensitivity` 記録（`s0`・`s1` 系）は陽性対照が無く、結論に使わない。
  S3 Access Points での結果は[段階 3](stage-3-s3-access-points.md)に書く。

### 2 台のクライアントによる挙動（2026-10-07）

run id `s1-20261007T164155Z`、ONTAP 9.19.1P2。2 台は同じファイルに対して同時に操作し、同期は検証対象のボリュームではなく成果物の S3 バケットを
経由させた。2 台が同じ同期 ID を持ち、時刻の重なり（競合側の試行が保持側のロック中、読み手の観測
が書き手の保存後）が両側の記録で確認できた組だけを `cross-host` とした。8 組すべてがこの条件を
満たした。SMB 同士の組も段階 1 の構成（NFS の export がある状態）での値で、段階 0 の基準値ではない。

| 保持側 / 競合側 | 保持の方法 | 保持中の競合側 | 解放後 |
|---|---|---|---|
| Windows（SMB）/ Linux（SMB） | `FileShare.None` | 読み取りと書き込みの open が拒否（`errno=16`） | 取得できた |
| Linux（SMB）/ Windows（SMB） | `fcntl` の排他ロック | open は成功、読み取りはロック違反、排他 open は共有違反 | 取得できた |
| Windows（SMB）/ Linux（NFS） | `FileShare.None` | 読み取りと書き込みの open が拒否（`errno=13`） | 取得できた |
| Linux（NFS）/ Windows（SMB） | `fcntl` の排他ロック | open は成功、読み取りはロック違反、排他 open は共有違反 | 取得できた |

| 書き手 / 読み手 | 結果 | 保存完了から読めるまで |
|---|---|---|
| Windows（SMB）/ Linux（SMB） | 読めた | 99 ms |
| Linux（SMB）/ Windows（SMB） | 読めた | 7,849 ms |
| Windows（SMB）/ Linux（NFS） | 読めた | 88 ms |
| Linux（NFS）/ Windows（SMB） | 読めた | 8,085 ms |

- 読み手は 100 ms 間隔で一覧と読み取りを繰り返したので、値の分解能は約 100 ms である。この run は各組で
  1 回ずつの計測で、再現は次の「再現」の節に書く。性能の主張ではない。
- NFS で書いたファイルを Windows が SMB で読めるまでの時間（8,085 ms）は、SMB で書いた場合
  （7,849 ms）とほぼ同じだった。この構成での約 8 秒の遅れは、書き手のプロトコルではなく Windows
  が読み手であることに伴って現れた。逆向き（Windows が書き、Linux が読む）は SMB でも NFS でも
  約 100 ms（1 回の待ち間隔）以内だった。
- Windows の `FileShare.None` は、NFS の open も拒否した。Linux 側の `fcntl` ロックは、Windows の
  読み取りをロック違反として止めた。どちらの組も、プロトコルをまたいで排他が効いた。

### 再現（2026-10-11 と 2026-10-12、ONTAP 9.19.1P3）

> 環境は ap-northeast-1、Single-AZ、1 ボリューム `appdata`（security style `ntfs`）。値はいずれも各組 1 回の観測。
> 2 台の協調は、`probe_merge.py` が同期 ID の一致と時刻の重なりを確かめ `cross-host`（`reasons` が空）と
> 判定した組だけを数えた。

| run id | 内容 | file-locking | write-visibility（Windows 書き→Linux 読み / Linux 書き→Windows 読み） |
|---|---|---|---|
| `s1-20261011T075058Z`（2026-10-11） | 旧 Probe（Linux 側は Python）、SMB 4 組と NFS 4 組 | 4 組とも contender が拒否され、解放後に成功 | SMB: 38 ms / 8,546 ms。NFS: 18 ms / 8,168 ms |
| `s2m-20261012T090934Z`（2026-10-12） | 近代化 Probe（Linux 側は net10.0）、Windows 側は旧ビルド、8 組 | 4 組とも contender が拒否され、解放後に成功。Linux の holder は `FileShare.None` | SMB: 66 ms / 7,968 ms。NFS: 35 ms / 8,342 ms |

- file-locking の拒否は、Windows の holder に対して Linux の contender が `errno=16`（cifs）または `errno=13`（NFS）で
  拒否された。Linux の holder に対して Windows の contender は、open は成功し、読み取りがロック違反
  （`0x80070021`）、排他 open が共有違反（`0x80070020`）になった。Linux の holder の機構は、旧 Probe が
  `fcntl.lockf`、近代化 Probe が `FileShare.None` で違うが、Windows の contender が拒否される結果は同じだった。
- 「Linux が書き Windows が読む方向で約 8 秒、逆方向で 100 ms 未満」という形は、4 つの run
  （`s1-20261007T164155Z`、`s0-20261011T065443Z`（SMB のみ）、`s1-20261011T075058Z`、`s2m-20261012T090934Z`）で現れた。
  Linux の書き手が Python（`os.fsync` あり）でも近代化 Probe（`File.WriteAllBytes`、fsync なし）でも同じ形だった。
  原因は調べていない。
- 読者が取る行動: Windows クライアントが Linux（SMB または NFS）の書いたファイルを読む処理には、読めるまでの遅れ（この
  構成で約 8 秒）を許す待機やリトライを置く。逆向きの遅れが小さいことを根拠に、遅れの無い設計にしない。
  ここで測った秒数は 1 つの構成での値で、他の構成の遅れの長さを示すものではない。
- 測ったのは、一方が `FileShare.None` または排他ロックを取り、他方がその間に open を試みた場合だけである。
  ロックを取らずに書く側が止められるかは測っていない。

### NFS からの拒否と原因

NFS 側の表示（`ls -l`）は、読み書きできた `appsvc` に対してもモードを `d---------`、所有者を
`root` または `nobody` と示し、拒否の原因の手がかりにならなかった。原因は ONTAP の effective-permissions、name mapping の解決結果、
EMS のイベントで切り分けた（NFS 側の表示だけでは NTFS の拒否理由が分からないことは Hub の
[nfs-side-view-does-not-explain-ntfs-denials](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/nfs-side-view-does-not-explain-ntfs-denials.md)
にある）。

| NFS の主体 | 結果 | Windows 側の対応付け | 原因 |
|---|---|---|---|
| root（uid 0） | 最上位から拒否 | `superuser none` で匿名ユーザー `pcuser` になり、Windows 名に対応付かない | name mapping の欠落。EMS に `secd.nfsAuth.noNameMap` が記録され、既定の Windows ユーザーがないことが理由として出た |
| uid 10001（`appsvc`） | 読み書きできた | `APPMOD\appsvc` | 拒否なし |
| uid 10002（`appreader`） | 最上位は一覧できるが `seed/` に入れない | `APPMOD\appreader` | NTFS ACL。effective-permissions に `execute`（走査）がない |

- `appreader` の effective-permissions は、Windows 名で引いても UNIX 名で引いても `read`、`read_ea`、
  `read_attributes`、`read_control`、`synchronize` で、`execute` を含まない。ACL は `appreader` に
  読み取り系の許可と書き込みの拒否だけを与えており、走査の権利を与えていない。
- 同じ `appreader` は、同じ ACL のまま SMB では `seed/` 配下のファイルを読めた。ACL は変更して
  いない。SMB と NFS でこの差が出る理由は下の「未検証の仮説」に置く。
- NTFS スタイルのボリュームでは、権限評価に使われるのは Windows の ACL で、NFS の主体もいったん
  Windows 名に対応付けてから評価される（Hub の
  [security-style-and-permission-evaluation](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/security-style-and-permission-evaluation.md)）。
  root が対応付けの段階で、`appreader` が ACL の段階で拒否されたのは、この順序どおりだった。

## 実測で判明した所見

- 対応付けのない NFS の主体（root を含む）は、既定の Windows ユーザーを設定しないことで拒否として
  表に出た。予想どおりで、原因は EMS で確かめられた。
- 反対方向（Windows 名から UNIX 名）の既定ユーザー（CIFS の `default_unix_user`）には、SVM の
  既定値 `pcuser` が入っている。この方向で「対応付けのない主体は拒否される」は成り立たない。SMB の
  アクセスにこの値が効くかどうかは確かめていない。
- NFS の Probe と目録は、Run Command の root ではなく UNIX ユーザー `appsvc`（uid 10001）で実行する
  必要があった。`sec=sys` では NFS の主体はローカルの uid で、root は匿名ユーザーに置き換わるため。
  2026-10-12 の `s2m-20261012T090934Z` では、root で `cd /mnt/appdata` すると `Permission denied` になり、
  近代化 Probe と Worker は `setpriv --reuid=10001 --regid=10001 --clear-groups` で uid 10001 に切り替えて実行した
  （ONTAP 9.19.1P3、`sec=sys`、1 回の観測）。ntfs ボリュームでは、権限は ACL で評価され、NFS の主体は
  name mapping で Windows 名に対応付いた場合にだけ評価の対象になる。
  読者が取る行動: NFS クライアントは name mapping が効く uid で動かす。root で動かすと、ボリュームの
  最上位から拒否される。
- NFS の `ls -l` のモード表示は、可否を表さない。2026-10-12 の NFS 側の一覧でも、最上位の `out`・`probe`・`seed` は
  `d---------`（root 所有）と表示されたが、uid 10001 は読み書きできた（1 回の一覧）。読者が取る行動は、
  `ls` のモード表示を権限の根拠にせず、ONTAP の effective-permissions と name mapping の解決結果で確かめること。
- 段階 0 の `file-locking` と `write-visibility` は、2026-10-07 の記録では 2 台の相互作用を測っていなかった。段階 0 の文書の
  該当の主張は撤回した（[段階 0](stage-0-smb-ntfs.md)）。

## 主張の出典

マルチプロトコルの一般知見は、主張ごとに Hub のノートを出典として引く（内容は写さない）。

- NFS を足すのに複製が要らないこと: [adding-a-protocol-does-not-need-a-clone](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/adding-a-protocol-does-not-need-a-clone.md)
- セキュリティスタイルと権限評価: [security-style-and-permission-evaluation](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/security-style-and-permission-evaluation.md)
- NFS 側の表示だけでは NTFS の拒否理由が分からないこと: [nfs-side-view-does-not-explain-ntfs-denials](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/nfs-side-view-does-not-explain-ntfs-denials.md)

## 未検証の仮説

- `appreader` が SMB では走査の権利なしに `seed/` 配下を読め、NFS では入れない理由は、SMB の
  アクセスでは走査チェックを省略するユーザー権利（Bypass traverse checking）が効き、NFS では
  ディレクトリごとに `execute` が評価されるためと推測する。`hypothesis`。未検証。
- Windows が読み手の組で約 8 秒の遅れが出た理由は、Windows の SMB クライアントがディレクトリの
  内容や「ファイルがない」という結果を一定時間キャッシュするためと推測する。読み手は書き込みの
  前からファイルの有無を問い合わせていた。`hypothesis`。クライアントの設定値は確かめていない。
  遅れの形が現れること自体は、上の「再現」の 4 つの run で確認した。遅れの原因は調べていない。
- NFS の `\` を含む名前（`exists_literal`）は 2026-10-07 の Python 版 Probe の観測を引用したものである。
  近代化 Probe は `\` を含む名前を書かないので、再観測していない。

## 未測定の範囲

次は、この段階では測っていない。結論に使わない。

- security style `unix` と `mixed` での挙動
- ONTAP 9.19.1P2・9.19.1P3 以外の版
- Multi-AZ、複数 SVM、1 ボリュームを超える構成
- cifs の他の mount options（`nocase` を含む）、Kerberos、NFS 4.1 以外
- 請求額
