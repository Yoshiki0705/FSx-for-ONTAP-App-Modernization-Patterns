# サンプルアプリ: DocIntake の構成と 5 つの挙動

> 証拠区分: `hypothesis`（未測定）。挙動の「予想」は実測前の仮説で、段階 0〜1 の記録で置き換える。

`DocIntake` は、受付フォルダに置かれた文書を検査して索引と要約を書き出す小さな自作アプリである。
DB と認証を持たない。外部の OSS を検証対象にせず、観測点を設計できるように自作した。

## サンプルアプリの構成

| プロジェクト | 種別 | 内容 |
|---|---|---|
| `DocIntake.Core` | クラスライブラリ | `IFileStore`（`List`・`Read`・`Write`・`OpenExclusive`・`CanWrite`）と `SmbFileStore` |
| `DocIntake.Worker` | コンソール | `seed/inbox/` を読み、`out/<stage>/index.json` と `reports/` を書く |
| `DocIntake.Probe` | コンソール | 5 つの挙動を測り JSON を出力する |

移行元は .NET Framework 4.8、C# 5、旧形式の `.csproj`、NuGet 依存なしとする。
ストレージの切り替えは `appSettings` の `Store` キー 1 つで行い、Bob's Used Books Classic の
`FileService` の形に寄せる。これにより AIMF の観点 MP-10 と MP-25 が当たる。
`Web` 層を持たないコンソール構成にしたので、ASP.NET の Web 層の移行と、`Web` の静的参照に仕込む
大小不一致の観点は当たらない。NuGet 依存がないので `packages.config` に関わる観点も当たらない。

## 5 つの挙動

移行元コードには、各挙動について「Windows + SMB では顕在化しない書き方」を 1 か所ずつ置く。

| ID | 挙動 | 仕込みの場所 | 予想（未検証） |
|---|---|---|---|
| `case-sensitivity` | 大文字小文字 | 参照 `Docs\Index.json`、実体 `docs/index.json`（`StorePaths`） | SMB は一致扱い、NFS は不一致 |
| `path-separator` | パス区切り | `\` を文字列連結する `StorePaths.Combine` | Linux では `\` がファイル名の一部になる |
| `file-locking` | ファイルロック | `FileShare.None` の `OpenExclusive`（`SmbFileStore`） | SMB 同士は拒否。プロトコル間は未確認 |
| `acl-evaluation` | ACL と権限評価 | `File.GetAccessControl` の事前判定（`SmbFileStore.CanWrite`） | Linux では事前判定が使えず I/O と食い違う |
| `write-visibility` | 書き込みの可視化 | 保存直後の読み戻しを即時可視と仮定（`Worker`） | NFS は属性キャッシュの分だけ遅れる |

.NET Framework 4.8 は Linux で動かないので、段階 0・1 の Linux 側は `scripts/probe_peer.py`
（標準ライブラリのみ）が同じ 5 つの挙動を測る。

## Probe の出力形式

Probe は観測値だけを JSON で出す。`outcome` は `measured`・`error`・`skipped` の 3 値。
`ok` / `differs` / `not-comparable` の判定は `scripts/compare-probe.py` が段階 0 と比べて行う。

```json
{
  "schema": "appmod-probe/1",
  "run_id": "s1-20261007T010203Z",
  "started_at": "2026-10-07T01:02:03Z",
  "stage": 1,
  "role": "writer",
  "behaviors": [
    {"id": "file-locking", "outcome": "measured", "observed": {"topology": "cross-host"}}
  ]
}
```

## ビルドの前提

AWS Tools for PowerShell は Windows ベースの AMI に既定で入る（版は AMI による）。
Windows Server 2022 の AMI に .NET Framework 4.x の MSBuild が同梱されているかは未確認（U18）で、
同梱されていなければ SDK 形式の `.csproj` と参照アセンブリで作業端末から `net48` 向けにビルドする。
この切り替えをすると、旧形式の `.csproj` に関わる観点が当たらなくなるので、その旨を段階 2 に記録する。

## 予想（未検証）

- 5 つの挙動の予想列はすべて仮説で、FSx for ONTAP 上では未測定である。
- Probe は、どの挙動の計測が失敗しても他を続け、失敗を `outcome: error` と例外の型で記録する。
