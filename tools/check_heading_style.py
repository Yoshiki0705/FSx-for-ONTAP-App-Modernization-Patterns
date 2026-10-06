#!/usr/bin/env python3
"""日本語の節見出しが体言止め（名詞句）であることを検査する。

節見出しはラベルである。ラベルを期待する位置に文が来ると読みにくいので、
`##` 以下の日本語見出しは名詞句で終わらせる。H1（文書タイトル）は別規約で
「1 行の主張文」と定めているため対象外。このリポジトリは frontmatter を
持たず、本文先頭の `#` がタイトルなので、`#` は検査しない。

叙述・助言・目標のように名詞化すると内容が壊れる見出しは、見出し行に
`<!-- allow:heading-style -->` を付けて除外し、理由を前後の本文に書く。

    python3 tools/check_heading_style.py --selftest   # 検査が落ちる能力の確認
    python3 tools/check_heading_style.py              # リポジトリ全体
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {
    ".git",
    "node_modules",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
    ".private",
    ".kiro",
    # ツールのキャッシュはリポジトリの内容ではない。走査対象に入れると、
    # ローカルのキャッシュに何が置かれているかで結果が変わる。
    ".pytest_cache",
    ".ruff_cache",
}

HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*$")
FENCE = re.compile(r"^\s*(?:```|~~~)")
ALLOW = re.compile(r"<!--\s*allow:heading-style\s*-->")
JAPANESE = re.compile(r"[ぁ-んァ-ヶ一-龠]")

# 文字クラスはう段だけ。動詞の終止形はう段で終わる。
#
#   `れ` を入れてはいけない。え段であって終止形にはならず、単独の `れ` は連用形の
#   名詞化（流れ / 崩れ / 遅れ / ずれ）。入れると閉じられない名詞クラスを誤検出し、
#   許可リストでは対処できない。
#
#   `ない` は個別に列挙する。`い$` で一括にしてはいけない。平叙の否定（…できない）は
#   文だが、`問い` `扱い` は名詞である。列挙を省くと否定の述語見出しを無言で通す。
VERBAL = re.compile(
    r"(?:ます|ません|ました|でした|です|ください|でしょうか|のか|か|ない|[うくぐすずつぬふぶむる])$"
)

# 名詞の許可リストは置かない。`れ` をクラスから外した時点で、許可リストの全語が
# そもそも VERBAL に一致しなくなる。発火しない許可リストは、提供していない保証を
# 表明することになる。効いているのは「`ない` をリテラルにした」点だけ。


def violations(text: str) -> list[tuple[int, str, str]]:
    """体言止めでない節見出しを (行番号, レベル, 見出し) で返す。"""
    found: list[tuple[int, str, str]] = []
    in_fence = False
    for n, line in enumerate(text.split("\n"), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or ALLOW.search(line):
            continue
        m = HEADING.match(line)
        if not m:
            continue
        h = ALLOW.sub("", m.group(2)).strip()
        if not JAPANESE.search(h):
            continue
        if VERBAL.search(h):
            found.append((n, m.group(1), h))
    return found


# 両方向を証明する。落ちない検査は、検査が無いのと区別できない。
CASES = [
    ("## 自分の環境で確かめる", True),
    ("## 検証を自動化する", True),
    ("## なぜこの区分が必要か", True),
    ("## どう分けるか", True),
    ("## 読み取りがあります", True),
    ("## 面に分かれました", True),
    ("## 既定は「同一」です", True),
    ("## アクセスは成立する", True),
    ("## AWS 側からしか消せない", True),
    ("## この経路を見ていない", True),
    ("## 自環境での確認手順", False),
    ("## 必要な理由", False),
    ("## 読み取りの存在", False),
    ("## 解除の不可", False),
    ("## 追加する流れ", False),
    ("## 最小権限の崩れ", False),
    ("## 実測の遅れ", False),
    ("## 扱う問い", False),
    ("## 権限の扱い", False),
    ("## よくある誤解", False),
    ("## 判断フロー", False),
    ("## ログの保存先", False),
    ("## リスクの一覧", False),
    ("## Deleting a volume", False),
    ("## How to choose", False),
    ("# タイトルは主張文で書く", False),
    ("## 15:29 気付く <!-- allow:heading-style -->", False),
]


def selftest() -> int:
    bad = [(c, want) for c, want in CASES if bool(violations(c)) != want]
    if violations("```bash\n# コピー元で実行しておく\n```\n"):
        bad.append(("fence: コードフェンス内のコメント行", False))
    for c, want in bad:
        print(f"selftest FAIL (expected flag={want}): {c}", file=sys.stderr)
    if bad:
        return 1
    print(f"selftest: {len(CASES) + 1} case(s) passed")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    total = 0
    for p in sorted(ROOT.rglob("*.md")):
        if any(part in SKIP for part in p.parts):
            continue
        hits = violations(p.read_text(encoding="utf-8"))
        if not hits:
            continue
        print(f"\n{p.relative_to(ROOT)}")
        for n, h, t in hits:
            print(f"  L{n:>4} {h} {t}")
        total += len(hits)
    if total:
        print(
            f"\n{total} 件が体言止めではありません。接尾語で断定を保って名詞化してください。",
            file=sys.stderr,
        )
        return 1
    print("heading style: all Japanese section headings are noun phrases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
