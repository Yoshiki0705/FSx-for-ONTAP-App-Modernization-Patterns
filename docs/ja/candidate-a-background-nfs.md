# Candidate A: .NET 10 BackgroundService を Linux/NFS で動かす

> 証拠区分: 運用手順（systemd ユニット、環境変数、昇格ゲートの入力）は、Candidate A の実環境検証
> （ap-northeast-1、ONTAP 9.18.1、使い捨て SVM/ボリューム上の NFS v4.1、Amazon Linux 2023 arm64、
> .NET 10）で `verified` として確認した。環境固有の識別子（アカウント、IP、ファイルシステム/SVM/
> ボリューム/インスタンスの ID）は非公開の ledger にあり、本書には載せない。「1 回だけの観測」は
> その旨を本文に書く。

Candidate A は、サンプルアプリ DocIntake のバックグラウンド処理を、`DocIntake.Worker`（コンソール）
とは別に .NET 10 の `BackgroundService` として実装したものである。Linux EC2 上で
Amazon FSx for NetApp ONTAP のボリュームを NFS マウントし、systemd サービスとして動かす。
本書はそのデプロイ手順と、実環境検証で判明した落とし穴をまとめる。

成果物は `app/modern/deploy/` にある。

| パス | 内容 |
|---|---|
| `app/modern/deploy/docintake-bgworker.service` | systemd ユニットのサンプル（`Type=simple`、`RequiresMountsFor`） |
| `app/modern/deploy/docintake-bgworker.env.example` | 環境変数テンプレート（`/etc/docintake-bgworker.env` に複製して編集） |

## 前提

- .NET 10 ランタイムを導入した Linux ホスト（検証は Amazon Linux 2023、arm64）。ビルドは別ホストで
  行い、発行物（`DocIntake.BackgroundWorker.dll` とその依存）を配置してよい。
- FSx for ONTAP のボリュームを NFS v4.1 でマウント済みであること。検証では security style は `unix`、
  認証は AUTH_SYS（`sec=sys`）。
- ワーカーがマウントを読み書きできる非特権サービスアカウント（例: `docintake`）。

## 環境変数

ワーカーの設定はすべて `DOCINTAKE_*` 環境変数で与える。1 つでも欠落・不正だと起動時に終了する。
意味は `docintake-bgworker.env.example` に記載した。要点は 2 つ。

- `DOCINTAKE_ROOT` は NFS マウントの根。`seed/inbox`、出力の `out/<stage>/reports/`、claim の
  `.claims` はすべてこの下に作られる。
- `DOCINTAKE_CONTROL_PLANE_PATH` は制御プレーンの記録先で、`DOCINTAKE_ROOT` の**外**に置く。
  NFS 側を片付けても事後条件が残るようにするためで、ワーカーは appdata の内側を指定すると拒否する。

## systemd ユニット

`docintake-bgworker.service` を `/etc/systemd/system/` に置き、環境ファイルを複製して起動する。

```bash
sudo cp app/modern/deploy/docintake-bgworker.service /etc/systemd/system/
sudo cp app/modern/deploy/docintake-bgworker.env.example /etc/docintake-bgworker.env
sudo "${EDITOR:-vi}" /etc/docintake-bgworker.env
sudo systemctl daemon-reload
sudo systemctl enable --now docintake-bgworker.service
systemctl status docintake-bgworker.service
```

### `Type=simple` を使う理由

このワーカーは素の .NET 汎用ホスト（`Program.Main` → `Host...RunAsync`）で、
`Microsoft.Extensions.Hosting.Systemd` を参照せず `UseSystemd()` も呼ばない。したがって systemd の
readiness 通知（sd_notify の `READY=1`）を送らない。`Type=notify` にすると、`systemctl start` は
来ない readiness を待ち続け、ユニットの起動タイムアウトまでブロックする。検証でもこの挙動を確認した
（`Type=notify` で起動がブロックし、`Type=simple` で正常起動した）。`notify` を使いたい場合は、
ワーカーに systemd ホスティング統合を追加する。追加しないなら `Type=simple` が正しい。

### マウント依存

`RequiresMountsFor=/mnt/appdata` により、systemd はこのユニットを NFS マウントの後に順序付け、
マウントが失われるとユニットを止める。ワーカーが存在しない根に対して走ることを防ぐ。

### 停止時の挙動

`systemctl stop` は SIGTERM を送る。汎用ホストはループを取り消し、制御プレーンに正常停止を記録し、
処理中の項目を終えて claim を解放してから終了する。`TimeoutStopSec` はその猶予で、検証では既定より
短い 30 秒でも graceful に停止した。

## 昇格ゲートの入力（`source_revision`）

`NfsPromotionGate` は検証の証拠を受け取って昇格可否を判定する。`source_revision` は **64 桁 16 進の
SHA-256 コンテンツダイジェスト**であり、40 桁の Git コミット SHA-1 ではない。コミット ID はコミットを
指すだけで、未コミットの作業ツリー差分を捕捉しないため、実際に動いたバイト列を証明できない。
ダイジェストはソースの各ファイルの SHA-256 マニフェストから導く。

```bash
# 各ファイルのハッシュを安定した順序で並べ、その全体をもう一度 SHA-256 にかけて 64 桁を得る。
find app/modern/src -type f \( -name '*.cs' -o -name '*.csproj' \) | sort \
  | xargs shasum -a 256 \
  | shasum -a 256 | cut -d' ' -f1
```

この 64 桁を `source_revision` に渡す。生成物（raw artifact）のハッシュ検証も同じ 64 桁 SHA-256 の
規約を共有する。

> 補足: ローカルの昇格ゲートは `approved_runtime_provenance` を常に「欠落」として扱い、自分では
> `verified` に昇格しない。実行の来歴（provenance）は、候補固有の実環境実行を記録した外部の ledger が
> 供給する。ゲートは検証入力をそろえる道具であって、最終判断そのものではない。

## 検証で確認したこと

Candidate A の実環境検証（ONTAP 9.18.1、使い捨て SVM/ボリューム上の NFS v4.1、Amazon Linux 2023
arm64、.NET 10）で次を `verified` とした。この範囲は本候補とこの記録環境に限る。

| 観点 | 結果 |
|---|---|
| NFS 上の処理 | 列挙 → claim → 読み取り → レポート書き込み → 解放が成立。事後条件は NFS 外に記録 |
| systemd ライフサイクル | `Type=simple` で起動・処理・graceful stop。マウント依存を強制 |
| claim 競合 | 他オーナーの有効 claim を持つ項目を拒否し、二重処理しない |
| タイムアウト境界 | 完了未確認のタイムアウトを NFS 外に記録し、遅延完了を回収 |
| 大文字小文字 | NFS マウントは case-sensitive（Linux/NFS の期待どおり。SMB/Windows とは異なる） |

環境固有の識別子は非公開 ledger にある。
