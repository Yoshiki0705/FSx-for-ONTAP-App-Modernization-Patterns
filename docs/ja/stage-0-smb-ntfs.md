# 段階 0: SMB と NTFS の出発点

> 証拠区分: `hypothesis`（未測定）。手順は計画で、予想は実測前の仮説である。実測記録で置き換える。

Windows EC2 上の .NET Framework 4.8 アプリが、NTFS セキュリティスタイルの対象ボリュームを SMB で
読み書きする出発点の構成を作り、Probe の 5 つの挙動を段階 0 の基準値として記録する。

## 手順（予定）

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
- Probe の 5 件の `outcome` が揃い、2 台の組が `cross-host` であること
- `seed/` の目録（相対パス、サイズ、SHA-256）が境界記録に含まれること

## 予想（未検証）

- 段階 0 では NFS の経路がないので、`b0` の目録は Windows 側の 1 つになる見込み。
- 5 つの挙動の予想は別文書「サンプルアプリ」の表のとおりで、すべて仮説である。

## 実測結果（verified）

> 証拠区分: 以下の箇条書きは `verified`。実測日 2026-10-07、リージョン ap-northeast-1、
> ONTAP 9.19.1P2、FSx for ONTAP Single-AZ（1,024 GiB / 128 MBps）、NTFS ボリューム `appdata`、
> AWS Managed Microsoft AD（Standard）に参加した SVM で実測した。ID と IP はマスクしてある
> （`fs-0123456789abcdef0` / `123456789012` / `10.0.x.x`）。この節以外は未測定の仮説のまま。

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
- Probe の 5 つの挙動は Windows（SMB、holder）と Linux（SMB、contender）の 2 台でいずれも
  `outcome=measured`、`observed.topology=cross-host` で揃った。`path-separator` は Linux の SMB で
  `\` を含む名前が拒否され（`rejected=true`、`errno=22`）、その拒否自体を観測として記録した。
  これは「Linux では例外なく別名ファイルができる」という事前の予想とは異なる所見で、NFS を足す
  段階 1 との比較が主眼になる。

## この段階で判明した所見（手順への反映）

- ONTAP REST の共有 ACL・file-security・`files` の各エンドポイントは、パスに SVM 名・ボリューム名
  ではなく UUID を要求する。NTFS ACL は `apply_to` に `this_folder`/`sub_folders`/`files` を
  指定しないと、子ファイルへ継承された ACE がフォルダ限定になり、一覧はできるが内容の読み取りが
  拒否される。
- FSx for ONTAP の管理エンドポイントの証明書 CN は管理 DNS 名で、スクリプトは管理 IP で到達する
  ため、ONTAP REST の `curl` には TLS 検証の調整が要る。
- SSM Run Command は SYSTEM で動くため、共有へは `appsvc` の SMB セッションを張ってから読み書き
  する。
