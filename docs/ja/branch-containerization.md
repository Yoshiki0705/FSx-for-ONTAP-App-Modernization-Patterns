# 分岐: コンテナ化

> 証拠区分: `hypothesis`（未測定）。この文書は分岐点の説明と隣の Spoke へのリンクだけを持つ。

この Spoke はアプリケーションの段階的モダナイゼーション（段階 0〜3）を扱う。
コンテナ化はこの Spoke では扱わず、分岐として Container-Datastore Spoke へ渡す。

## 分岐できる段階

段階 1（マルチプロトコル化）以降は、Linux 上の新しい .NET へ移す経路（段階 2）と、
コンテナ化する経路に分かれる。コンテナ化の手順はこの Spoke には複製しない。

## 隣の Spoke へのリンク

- Container-Datastore Spoke: [FSx-for-ONTAP-as-Container-Datastore（公開後にリンクを有効化）](https://github.com/Yoshiki0705/FSx-for-ONTAP-Container-Datastore-Patterns)

## 予想（未検証）

- 分岐点の正確な位置づけは、段階 1 の記録が揃った後に隣の Spoke と相互参照して確定する。
