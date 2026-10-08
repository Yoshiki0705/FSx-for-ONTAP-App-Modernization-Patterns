# 段階 1: 同じボリュームへの NFS の追加
>
> 証拠区分: 「実測結果（verified）」と「実測で判明した所見」の節は `verified`。それ以外の節（手順、
> 確認項目、予想、未検証の仮説）は計画と仮説で、未測定である。
段階 0 の対象ボリュームに NFS のアクセスを足す。ボリュームの複製・作り直し・データの移動はしない。
セキュリティスタイルは NTFS のまま維持する。

## 手順（予定）

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
> 実測日 2026-10-07、リージョン ap-northeast-1、ONTAP 9.19.1P2。Amazon FSx for NetApp ONTAP
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

### 1 台で完結する 3 つの挙動

| 挙動 | Windows（SMB） | Linux（SMB） | Linux（NFS） |
|---|---|---|---|
| `path-separator`（`\` を含む名前の作成） | 区切りとして扱われる | 拒否（`errno=22`） | 作成できる。`\` は名前の一部になる |
| `case-sensitivity`（大小だけ違う 2 ファイル） | 参照 `Docs\Index.json` は解決しない | 2 ファイルにならない | 2 つの別ファイルになる |
| `acl-evaluation` | 事前判定は書き込み可 | 事前判定の手段なし | 事前判定の手段なし |

- NFS で作った `sep\check.txt` は、Windows からは 8.3 形式の短い名前 `SEPCHE~1.TXT` として見え、
  読めた。段階 0 で Linux の SMB が拒否した名前は、同じボリュームでも NFS からは作成できる。
- NFS で作った `casecheck.txt` と `CaseCheck.txt` は、Windows からは `casecheck.txt` と
  `CASECH~2.TXT` の 2 つとして見え、どちらも内容を読めた。
- Windows と Linux（SMB）の 3 つの挙動は段階 0 と同じ観測だった（`compare-probe.py` の判定は `ok`）。
  Linux（NFS）は段階 0 に対応する経路がないので比較対象がない。

### 2 台のクライアントによる挙動

2 台は同じファイルに対して同時に操作し、同期は検証対象のボリュームではなく成果物の S3 バケットを
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

- 読み手は 100 ms 間隔で一覧と読み取りを繰り返したので、値の分解能は約 100 ms である。各組で
  1 回ずつの計測で、再現性は確かめていない。性能の主張ではない。
- NFS で書いたファイルを Windows が SMB で読めるまでの時間（8,085 ms）は、SMB で書いた場合
  （7,849 ms）とほぼ同じだった。この構成での約 8 秒の遅れは、書き手のプロトコルではなく Windows
  が読み手であることに伴って現れた。逆向き（Windows が書き、Linux が読む）は SMB でも NFS でも
  約 100 ms（1 回の待ち間隔）以内だった。
- Windows の `FileShare.None` は、NFS の open も拒否した。Linux 側の `fcntl` ロックは、Windows の
  読み取りをロック違反として止めた。どちらの組も、プロトコルをまたいで排他が効いた。

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
- 段階 0 の `file-locking` と `write-visibility` は 2 台の相互作用を測っていなかった。段階 0 の文書の
  該当の主張は撤回した（[段階 0](stage-0-smb-ntfs.md)）。

## 主張の出典

マルチプロトコルの一般知見は、主張ごとに Hub のノートを出典として引く（内容は写さない）。

- NFS を足すのに複製が要らないこと: [adding-a-protocol-does-not-need-a-clone](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/adding-a-protocol-does-not-need-a-clone.md)
- セキュリティスタイルと権限評価: [security-style-and-permission-evaluation](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/security-style-and-permission-evaluation.md)
- NFS 側の表示だけでは NTFS の拒否理由が分からないこと: [nfs-side-view-does-not-explain-ntfs-denials](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/nfs-side-view-does-not-explain-ntfs-denials.md)

## 予想（未検証）

- `sec=ntlmssp` で Linux から SMB に接続できる見込み。だめなら `sec=krb5` に切り替える（段階 0 で
  `sec=ntlmssp` での接続を確認済み）。
- name mapping の欠落は、既定ユーザーを設定しないことで拒否として表に出る見込み（NFS からの
  アクセスでは上の実測で成立した。反対方向は既定値 `pcuser` があり成立しない）。

## 未検証の仮説

- `appreader` が SMB では走査の権利なしに `seed/` 配下を読め、NFS では入れない理由は、SMB の
  アクセスでは走査チェックを省略するユーザー権利（Bypass traverse checking）が効き、NFS では
  ディレクトリごとに `execute` が評価されるためと推測する。未検証。
- Windows が読み手の組で約 8 秒の遅れが出た理由は、Windows の SMB クライアントがディレクトリの
  内容や「ファイルがない」という結果を一定時間キャッシュするためと推測する。読み手は書き込みの
  前からファイルの有無を問い合わせていた。クライアントの設定値も、遅れの再現性も確かめていない。
