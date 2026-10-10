# 段階 2: Linux 上の新しい .NET への移行

> 証拠区分: `hypothesis`（未測定）。手順は計画で、予想は実測前の仮説である。実測記録で置き換える。

AI Modernization Flow（以下 AIMF、v0.11.0）の `dotnetfw-to-modern-dotnet` playbook で、
移行元アプリを新しい .NET に移し、Linux EC2 上で NFS 経由に動かす。
Windows クライアントを段階 3 まで残すので、セキュリティスタイルは NTFS のまま維持する。

## 手順（予定）

1. フックの配線を再検査し、`fsxadmin` を段階 2 の間だけ Linux のインスタンスロールから読めなくする
2. AWS Transform custom を唯一の入口（`run-atx.sh`）から実行する（承認後）
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
