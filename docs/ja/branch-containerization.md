# 分岐: コンテナ化

> 証拠区分: `open/not measured`。この文書は、将来のナビゲーションと責務の所有範囲を示します。コンテナ runtime の検証済み挙動は、この Spoke の対象に含みません。

## 分岐の位置づけ

この Spoke は `DocIntake` の段階的モダナイゼーション（段階 0〜3）を扱います。
コンテナ化は将来の分岐として Container-Datastore Spoke に渡す責務であり、この Spoke では実行形、デプロイ、データストア接続の runtime 挙動を検証していません。

段階 1（マルチプロトコル化）以降は、Linux 上の .NET 10 / NFS へモダナイズする経路（段階 2）と、コンテナ化を検討する経路に分かれます。コンテナ化の手順や結果は、この Spoke に複製しません。

## 所有範囲へのリンク

- Container-Datastore Spoke: [FSx-for-ONTAP-as-Container-Datastore](https://github.com/Yoshiki0705/FSx-for-ONTAP-Container-Datastore-Patterns)

このリンクは隣接 Spoke のナビゲーションと責務の所有範囲を示す `documented` なコンテキストです。リンク先の記載は、この Spoke の検証済み範囲を拡張しません。

## 未検証の範囲

Container-Datastore のコンテナ化は、この Spoke では `open/not measured` です。ECS、EKS、コンテナからの NFS / SMB / S3 Access Points、クラスタの実行、デプロイ、障害時の挙動は検証していません。
