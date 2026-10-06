# 段階 1: 同じボリュームへの NFS の追加

> 証拠区分: `hypothesis`（未測定）。手順は計画で、予想は実測前の仮説である。実測記録で置き換える。

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

## 主張の出典

マルチプロトコルの一般知見は、主張ごとに Hub のノートを出典として引く（内容は写さない）。

- セキュリティスタイルと権限評価: [security-style-and-permission-evaluation](https://github.com/Yoshiki0705/fsxn-adoption-playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/security-style-and-permission-evaluation.md)
- NFS 側の表示だけでは NTFS の拒否理由が分からないこと: [nfs-side-view-does-not-explain-ntfs-denials](https://github.com/Yoshiki0705/fsxn-adoption-playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/nfs-side-view-does-not-explain-ntfs-denials.md)

## 予想（未検証）

- `sec=ntlmssp` で Linux から SMB に接続できる見込み。だめなら `sec=krb5` に切り替える（未確認）。
- name mapping の欠落は、既定ユーザーを設定しないことで拒否として表に出る見込み。
