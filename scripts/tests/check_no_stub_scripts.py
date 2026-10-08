#!/usr/bin/env python3
"""Fail when an orchestration shell script's real-mode path is a stub (stdlib only).

Why this exists: in stages 0-1, six orchestration scripts passed `make all` and review and still
turned out, on the live environment, to print their ONTAP REST, SSM or S3 operations with `echo` or
`note` and execute none of them. The dry-run tests only checked that a script ran. This gate makes
that shape fail `make test` mechanically.

What it checks. Every `*.sh` under <root>/scripts/ (found by walking the tree; scripts/tests/ is
excluded, being test harnesses and fixtures) must be classified in stub_check_classification.json:

  external     performs AWS / ONTAP / SSM / S3 work. Its real-mode path (what runs when
               APPMOD_DRY_RUN is unset) must contain at least one real invocation of aws, curl,
               gitleaks, atx, `git clone`, install.sh, pwsh / powershell, or of a wrapper that
               resolves to one of them.
  pure         local-only work (argument parsing, JSON munging, pure helpers). Exempt.
  fail-closed  real mode exits non-zero by design. Its real-mode-only path must contain a literal
               `exit <non-zero>`.

It fails when a script on disk is not classified, when an entry names a script that is not on disk,
when an `external` script has no real invocation, and when a `fail-closed` script has no real-only
non-zero exit.

Wrappers are not listed by hand. A shell function defined in the script, or in a file the script
sources with `.` / `source`, is a wrapper when its own real-mode body makes a real invocation
(directly or through another wrapper; resolved to a fixed point). A function whose real-mode body
runs `"$@"` is a runner: a call to it counts only when the command it is handed (after the
function's own `shift`s) is itself a real invocation, so `run echo aws ...` does not count.

Per-call rule (in external and fail-closed scripts): a real-mode runner call whose handed command
is `echo`, `printf` or an echo-only function (one whose real-mode body only prints, e.g. `note`) is
an error on its own, whatever other calls in the script invoke. This is the step-level stub shape,
`step "7 empty the bucket" echo "aws s3 rm ..."`, which a per-script count would absorb.

What does not count as an invocation: anything that is only an argument (`echo aws ...`,
`printf ... aws`, `note "aws ssm send-command ..."`), anything inside quotes that is not a command
substitution (`remote="... aws s3 cp ..."` sent to another host), comments, here-document bodies,
`command -v aws`, and any command in a branch that runs only under dry-run.

The parser is a small shell lexer with a block stack, not a bash parser. Dry-run branches are
recognised by condition text, where the condition is exactly a DRY_RUN test, or a DRY_RUN test
followed by one && / || clause:

  if [ -n "$DRY_RUN" ]; then <dry> else <real> fi
  if [ -n "$DRY_RUN" ]; then echo ...; return 0; fi        (one line; the branch is dry)
  if [ -z "$DRY_RUN" ]; then <real> fi
  elif [ -n "$DRY_RUN" ]; then <dry> else <real> fi        (else after the elif is real-only)
  if [ -n "$DRY_RUN" ] || X; then <either> else <real> fi
  if [ -z "$DRY_RUN" ] && X; then <real> else <either> fi

Known limits (stated so that a pass is not read as more than it is):
  - Short-circuit forms outside `if` (`[ -n "$DRY_RUN" ] && return 0`), `case` on DRY_RUN, a
    negated test (`! [ -n ... ]`) and DRY_RUN copied into another variable are not recognised;
    such a branch is treated as reachable in real mode.
  - Code after an early `return` / `exit` in a dry branch is "either mode", not "real-only". That
    is enough for counting invocations; a fail-closed exit must sit in an explicit real-only branch.
  - Reachability is not analysed: a real invocation inside a function that is never called still
    counts. Apart from the per-call runner rule, the gate is per script: one real call satisfies
    it even if another step prints its intended operation with a bare `echo` / `note` instead of
    handing it to a runner.
  - Command substitutions inside unquoted here-documents, `eval`, `bash -c "<string>"`, and
    commands built in variables (`"${CMD[@]}"`) are not analysed.
  - PowerShell scripts (*.ps1) are out of scope.

Run from the repository root:
  python3 scripts/tests/check_no_stub_scripts.py
  python3 scripts/tests/check_no_stub_scripts.py --root <dir-with-scripts/> --explain
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_CLASSIFICATION = HERE / "stub_check_classification.json"
CLASSES = ("external", "pure", "fail-closed")

TOOLS = frozenset({"aws", "curl", "gitleaks", "atx", "pwsh", "powershell"})
SHELLS = frozenset({"bash", "sh"})
# Prefix commands that run the next word as the command.
PREFIXES = frozenset({"exec", "builtin", "time", "nohup", "sudo", "setpriv"})

# Longest first, so that `;;` is not read as two `;` and `<<-` not as `<<` then `-`.
# fmt: off
OPS = (
    ";;&", "<<<", "&>>", "<<-",
    ";;", ";&", "&&", "||", "|&", "<<", ">>", ">&", "<&", "&>", ">|", "<>",
    ";", "|", "&", "(", ")", "<", ">",
)
# fmt: on
REDIRECTS = frozenset({"<", ">", ">>", ">&", "<&", "&>", "&>>", ">|", "<>", "<<<"})
HEREDOCS = frozenset({"<<", "<<-"})
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")
DRY_TEST = re.compile(
    r'^\s*\[\[?\s+-([nz])\s+"?\$\{?(?:APPMOD_)?DRY_RUN(?::-)?\}?"?\s+\]\]?\s*(.*)$',
    re.DOTALL,
)


@dataclass
class Tok:
    kind: str  # "word" | "op"
    text: str
    line: int
    nested: list[list[Tok]] = field(default_factory=list)


@dataclass
class Command:
    words: list[str]
    line: int
    mode: (
        str | None
    )  # None = either mode, "real" = real-only, "dry" / "dead" = never real
    func: str | None


@dataclass
class Frame:
    kind: str  # "if" | "brace" | "paren" | "case" | "loop"
    func: str | None = None
    phase: str = ""
    implied: str | None = None
    acc: str | None = None
    cond: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------- lexer


class Lexer:
    """Split shell source into word and operator tokens.

    Quotes are kept in the word text. Command substitutions ($(...), `...`, <(...), >(...)) are
    tokenised recursively and attached to the word as nested token streams. Comments and
    here-document bodies are dropped. Arithmetic $((...)) and ${...} are kept as opaque text.
    """

    def __init__(self, src: str, line: int = 1) -> None:
        self.s = src
        self.i = 0
        self.line = line
        self.heredocs: list[tuple[str, bool]] = []

    def tokens(self, closer: bool = False) -> list[Tok]:
        s = self.s
        out: list[Tok] = []
        word: list[str] = []
        nested: list[list[Tok]] = []
        start = self.line
        depth = 0

        def flush() -> None:
            nonlocal word, nested
            if word or nested:
                out.append(Tok("word", "".join(word), start, nested))
            word = []
            nested = []

        while self.i < len(s):
            c = s[self.i]
            if not word and not nested:
                start = self.line
            if c == "\\":
                pair = s[self.i : self.i + 2]
                self.i += 2
                if pair == "\\\n":
                    self.line += 1
                else:
                    word.append(pair)
                continue
            if c in " \t":
                flush()
                self.i += 1
                continue
            if c == "\n":
                flush()
                out.append(Tok("op", "\n", self.line))
                self.i += 1
                self.line += 1
                if self.heredocs:
                    self._skip_heredocs()
                continue
            if c == "#" and not word and not nested:
                end = s.find("\n", self.i)
                self.i = len(s) if end < 0 else end
                continue
            if c == "'":
                end = s.find("'", self.i + 1)
                end = len(s) - 1 if end < 0 else end
                chunk = s[self.i : end + 1]
                word.append(chunk)
                self.line += chunk.count("\n")
                self.i = end + 1
                continue
            if c == '"':
                self._double(word, nested)
                continue
            if c == "`":
                self._backtick(word, nested)
                continue
            if c == "$":
                self._dollar(word, nested)
                continue
            if c in "<>" and s[self.i + 1 : self.i + 2] == "(":
                word.append(c + "(")
                self.i += 2
                nested.append(self.tokens(closer=True))
                word.append(")")
                continue
            if c == "(" and word and "".join(word).endswith("="):
                # Array literal (NAME=(a b c)): data, not a command list.
                self._array_literal(word)
                continue
            op = next((o for o in OPS if s.startswith(o, self.i)), None)
            if op is None:
                word.append(c)
                self.i += 1
                continue
            if op in REDIRECTS | HEREDOCS and word and "".join(word).isdigit():
                word = []  # file descriptor prefix (2>&1), part of the redirection
            flush()
            self.i += len(op)
            if op == ")":
                if closer and depth == 0:
                    return out
                depth = max(depth - 1, 0)
            elif op == "(":
                depth += 1
            if op in HEREDOCS:
                self._heredoc_delimiter(strip_tabs=op == "<<-")
            out.append(Tok("op", op, self.line))
        flush()
        return out

    def _double(self, word: list[str], nested: list[list[Tok]]) -> None:
        s = self.s
        word.append('"')
        self.i += 1
        while self.i < len(s):
            c = s[self.i]
            if c == "\\":
                pair = s[self.i : self.i + 2]
                word.append(pair)
                self.line += pair.count("\n")
                self.i += 2
                continue
            if c == '"':
                word.append('"')
                self.i += 1
                return
            if c == "$":
                self._dollar(word, nested)
                continue
            if c == "`":
                self._backtick(word, nested)
                continue
            if c == "\n":
                self.line += 1
            word.append(c)
            self.i += 1

    def _dollar(self, word: list[str], nested: list[list[Tok]]) -> None:
        s = self.s
        if s.startswith("$((", self.i):
            j = self.i + 1
            level = 0
            while j < len(s):
                if s[j] == "(":
                    level += 1
                elif s[j] == ")":
                    level -= 1
                    if level == 0:
                        j += 1
                        break
                j += 1
            chunk = s[self.i : j]
            self.line += chunk.count("\n")
            word.append(chunk)
            self.i = j
            return
        nxt = s[self.i + 1 : self.i + 2]
        if nxt == "(":
            word.append("$(")
            self.i += 2
            nested.append(self.tokens(closer=True))
            word.append(")")
            return
        if nxt == "{":
            j = self.i + 1
            level = 0
            while j < len(s):
                ch = s[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch in "'\"":
                    close = s.find(ch, j + 1)
                    j = len(s) if close < 0 else close + 1
                    continue
                if ch == "{":
                    level += 1
                elif ch == "}":
                    level -= 1
                    if level == 0:
                        j += 1
                        break
                j += 1
            chunk = s[self.i : j]
            self.line += chunk.count("\n")
            word.append(chunk)
            self.i = j
            return
        if nxt == "'":
            j = self.i + 2
            while j < len(s) and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            word.append(s[self.i : j + 1])
            self.i = j + 1
            return
        word.append("$")
        self.i += 1

    def _backtick(self, word: list[str], nested: list[list[Tok]]) -> None:
        s = self.s
        j = self.i + 1
        while j < len(s) and s[j] != "`":
            j += 2 if s[j] == "\\" else 1
        inner = s[self.i + 1 : j]
        nested.append(Lexer(inner, self.line).tokens())
        word.append(s[self.i : j + 1])
        self.line += inner.count("\n")
        self.i = j + 1

    def _array_literal(self, word: list[str]) -> None:
        s = self.s
        j = self.i
        level = 0
        while j < len(s):
            ch = s[j]
            if ch == "\\":
                j += 2
                continue
            if ch in "'\"":
                close = s.find(ch, j + 1)
                j = len(s) if close < 0 else close + 1
                continue
            if ch == "(":
                level += 1
            elif ch == ")":
                level -= 1
                if level == 0:
                    j += 1
                    break
            j += 1
        chunk = s[self.i : j]
        self.line += chunk.count("\n")
        word.append(chunk)
        self.i = j

    def _heredoc_delimiter(self, strip_tabs: bool) -> None:
        s = self.s
        while self.i < len(s) and s[self.i] in " \t":
            self.i += 1
        j = self.i
        while j < len(s) and s[j] not in " \t\n;|&<>()":
            j += 1
        raw = s[self.i : j]
        self.i = j
        self.heredocs.append(
            (raw.replace("'", "").replace('"', "").replace("\\", ""), strip_tabs)
        )

    def _skip_heredocs(self) -> None:
        s = self.s
        for delimiter, strip_tabs in self.heredocs:
            while self.i < len(s):
                end = s.find("\n", self.i)
                end = len(s) if end < 0 else end
                text = s[self.i : end]
                self.i = min(end + 1, len(s))
                self.line += 1
                if (text.lstrip("\t") if strip_tabs else text) == delimiter:
                    break
        self.heredocs = []


# ---------------------------------------------------------------------------------- assembler


def combine(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None or a == b:
        return a
    return "dead"


def classify_condition(text: str) -> tuple[str | None, str | None]:
    """Return (mode of the then-branch, mode implied by the condition being false)."""
    m = DRY_TEST.match(text)
    if not m:
        return None, None
    then_mode, else_mode = ("dry", "real") if m.group(1) == "n" else ("real", "dry")
    rest = m.group(2).strip()
    if not rest:
        return then_mode, else_mode
    if rest.startswith("&&"):
        return then_mode, None
    if rest.startswith("||"):
        return None, else_mode
    return None, None


def unquote(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text).replace('"', "").replace("'", "")


class Assembler:
    """Turn token streams into simple commands, each tagged with its mode and enclosing function."""

    def __init__(self) -> None:
        self.commands: list[Command] = []

    def run(
        self,
        toks: list[Tok],
        base_mode: str | None = None,
        base_func: str | None = None,
    ):
        stack: list[Frame] = []
        cur: list[Tok] = []
        skip_target = False
        pending_func: str | None = None
        function_keyword = False
        for_header = False

        def mode_now() -> str | None:
            mode = base_mode
            for frame in stack:
                if frame.kind == "if":
                    mode = combine(
                        mode, frame.implied if frame.phase == "body" else frame.acc
                    )
            return mode

        def func_now() -> str | None:
            for frame in reversed(stack):
                if frame.kind == "brace" and frame.func:
                    return frame.func
            return base_func

        def innermost(kind: str) -> Frame | None:
            for frame in reversed(stack):
                if frame.kind == kind:
                    return frame
            return None

        def capture(text: str) -> None:
            frame = innermost("if")
            if frame is not None and frame.phase == "cond":
                frame.cond.append(text)

        def pop_until(kind: str) -> None:
            while stack:
                if stack.pop().kind == kind:
                    return

        def finish() -> None:
            nonlocal cur
            if cur:
                words = [unquote(t.text) for t in cur]
                self.commands.append(
                    Command(words, cur[0].line, mode_now(), func_now())
                )
            cur = []

        k = 0
        while k < len(toks):
            tok = toks[k]
            k += 1
            for sub in tok.nested:
                self.run(sub, mode_now(), func_now())
            top = stack[-1] if stack else None
            if top is not None and top.kind == "case" and top.phase != "body":
                if top.phase == "header":
                    if tok.kind == "word" and tok.text == "in":
                        top.phase = "pattern"
                elif tok.kind == "word" and tok.text == "esac":
                    stack.pop()
                elif tok.kind == "op" and tok.text == ")":
                    top.phase = "body"
                continue
            if for_header:
                if tok.kind == "word" and tok.text == "do":
                    stack.append(Frame("loop"))
                    for_header = False
                continue
            if tok.kind == "op":
                op = tok.text
                if op in ("&&", "||", "|"):
                    capture(op)
                if op in REDIRECTS:
                    skip_target = True
                    continue
                if op in HEREDOCS:
                    continue
                if (
                    op == "("
                    and len(cur) == 1
                    and k < len(toks)
                    and toks[k].text == ")"
                ):
                    pending_func = unquote(cur[0].text)
                    cur = []
                    k += 1
                    continue
                if op == "(" and pending_func and k < len(toks) and toks[k].text == ")":
                    k += 1  # `function name() {`
                    continue
                finish()
                if op in (";;", ";&", ";;&"):
                    case = innermost("case")
                    if case is not None:
                        while stack and stack[-1] is not case:
                            stack.pop()
                        case.phase = "pattern"
                elif op == "(":
                    stack.append(Frame("paren"))
                elif op == ")" and top is not None and top.kind == "paren":
                    stack.pop()
                continue
            text = tok.text
            if skip_target:
                skip_target = False
                capture(text)
                continue
            if not cur:
                if function_keyword:
                    pending_func = unquote(text).removesuffix("()")
                    function_keyword = False
                    continue
                if text in ("if", "elif"):
                    if text == "if":
                        stack.append(Frame("if", phase="cond"))
                    else:
                        frame = innermost("if")
                        if frame is not None:
                            frame.phase = "cond"
                            frame.cond = []
                    continue
                if text == "then":
                    frame = innermost("if")
                    if frame is not None and frame.phase == "cond":
                        then_mode, else_mode = classify_condition(" ".join(frame.cond))
                        frame.implied = combine(frame.acc, then_mode)
                        frame.acc = combine(frame.acc, else_mode)
                        frame.phase = "body"
                    continue
                if text == "else":
                    frame = innermost("if")
                    if frame is not None:
                        frame.implied = frame.acc
                        frame.phase = "body"
                    continue
                if text == "fi":
                    pop_until("if")
                    continue
                if text == "case":
                    stack.append(Frame("case", phase="header"))
                    continue
                if text == "esac":
                    pop_until("case")
                    continue
                if text in ("for", "select"):
                    for_header = True
                    continue
                if text == "do":
                    stack.append(Frame("loop"))
                    continue
                if text == "done":
                    pop_until("loop")
                    continue
                if text == "{":
                    capture(text)
                    stack.append(Frame("brace", func=pending_func))
                    pending_func = None
                    continue
                if text == "}":
                    capture(text)
                    pop_until("brace")
                    continue
                if text in ("while", "until", "!", "time"):
                    continue
                if text == "function":
                    function_keyword = True
                    continue
                if ASSIGNMENT.match(text):
                    capture(text)
                    continue
            cur.append(tok)
            capture(text)
        finish()


# ----------------------------------------------------------------------------------- analysis


@dataclass
class FuncInfo:
    commands: list[Command] = field(default_factory=list)
    wrapper_of: str | None = None
    runner_offset: int | None = None  # set when the function runs "$@"
    echo_only: bool = (
        False  # its real-mode body only prints (echo / printf / other echo-only)
    )


# Commands that print, and commands that neither print nor do work, for the echo-only test.
PRINTERS = frozenset({"echo", "printf"})
NEUTRAL = frozenset({"local", "shift", "return", ":", "true"})


def parse(path: Path) -> list[Command]:
    assembler = Assembler()
    assembler.run(Lexer(path.read_text(encoding="utf-8")).tokens())
    return assembler.commands


def is_real_mode(cmd: Command) -> bool:
    return cmd.mode in (None, "real")


def strip_prefixes(words: list[str]) -> list[str]:
    w = list(words)
    while w:
        name = os.path.basename(w[0])
        if name == "command":
            if w[1:2] and w[1] in ("-v", "-V"):
                return []
            w = w[1:]
        elif name == "env":
            w = w[1:]
            while w and (w[0].startswith("-") or ASSIGNMENT.match(w[0])):
                w = w[1:]
        elif name == "timeout":
            w = w[1:]
            while w and w[0].startswith("-"):
                w = w[1:]
            w = w[1:]
        elif name in PREFIXES:
            w = w[1:]
            while w and w[0].startswith("-"):
                w = w[1:]
        else:
            break
    return w


def git_subcommand(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        if args[i] in ("-C", "-c"):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i]
    return None


def invocation(words: list[str], funcs: dict[str, FuncInfo]) -> str | None:
    """Return what `words` really invokes (e.g. "aws", "curl via ontap_ok"), or None."""
    w = strip_prefixes(words)
    if not w:
        return None
    name = os.path.basename(w[0])
    if name.removesuffix(".exe") in TOOLS:
        return name
    if name == "git" and git_subcommand(w[1:]) == "clone":
        return "git clone"
    if name.endswith("install.sh"):
        return "install.sh"
    if name in SHELLS:
        args = [a for a in w[1:] if not a.startswith("-")]
        if args and args[0].endswith("install.sh"):
            return "install.sh"
    info = funcs.get(w[0])
    if info is None:
        return None
    if info.runner_offset is not None:
        inner = invocation(w[1 + info.runner_offset :], funcs)
        if inner:
            return f"{inner} via {w[0]}"
    if info.wrapper_of:
        return f"{info.wrapper_of} via {w[0]}"
    return None


def collect_functions(commands: list[Command], funcs: dict[str, FuncInfo]) -> None:
    for cmd in commands:
        if cmd.func:
            funcs.setdefault(cmd.func, FuncInfo()).commands.append(cmd)


def resolve_functions(funcs: dict[str, FuncInfo]) -> None:
    for info in funcs.values():
        shifts = 0
        for cmd in sorted(info.commands, key=lambda c: c.line):
            if not is_real_mode(cmd):
                continue
            w = strip_prefixes(cmd.words)
            if w[:1] == ["shift"]:
                shifts += int(w[1]) if w[1:2] and w[1].isdigit() else 1
            elif w[:1] and w[0] in ("$@", "${@}", "$*"):
                info.runner_offset = shifts
                break
    changed = True
    while changed:
        changed = False
        for info in funcs.values():
            if info.wrapper_of:
                continue
            for cmd in info.commands:
                if is_real_mode(cmd):
                    found = invocation(cmd.words, funcs)
                    if found:
                        info.wrapper_of = found
                        changed = True
                        break
    # Echo-only: no wrapper, no runner, at least one print, and every real-mode command prints, is
    # neutral, or calls another echo-only function. Resolved to a fixed point like the wrappers.
    changed = True
    while changed:
        changed = False
        for info in funcs.values():
            if info.echo_only or info.wrapper_of or info.runner_offset is not None:
                continue
            names = [
                strip_prefixes(cmd.words)[:1]
                for cmd in info.commands
                if is_real_mode(cmd)
            ]
            names = [n[0] for n in names if n]
            printing = [
                n for n in names if n in PRINTERS or (n in funcs and funcs[n].echo_only)
            ]
            if printing and all(n in NEUTRAL or n in printing for n in names):
                info.echo_only = True
                changed = True


def echo_handoff(words: list[str], funcs: dict[str, FuncInfo]) -> str | None:
    """Return "<runner> <cmd>" when `words` hands a runner a command that only prints.

    `step "7 empty the bucket" echo "aws s3 rm ..."` runs `"$@"` for real and so executes `echo`.
    Nested runners (`run step x echo ...`) are followed.
    """
    w = strip_prefixes(words)
    if not w:
        return None
    info = funcs.get(w[0])
    if info is None or info.runner_offset is None:
        return None
    handed = strip_prefixes(w[1 + info.runner_offset :])
    if not handed:
        return None
    name = os.path.basename(handed[0])
    target = funcs.get(handed[0])
    if name in PRINTERS or (target is not None and target.echo_only):
        return f"{w[0]} {handed[0]}"
    inner = echo_handoff(handed, funcs)
    return f"{w[0]} {inner}" if inner else None


def discover(root: Path) -> list[str]:
    """Every *.sh under <root>/scripts/, relative to root, scripts/tests/ excluded."""
    base = root / "scripts"
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith((".", "__"))
            and (rel_dir / d).as_posix() != "scripts/tests"
        )
        for name in sorted(filenames):
            if name.endswith(".sh"):
                found.append((rel_dir / name).as_posix())
    return sorted(found)


def sourced_files(
    commands: list[Command], script: str, on_disk: list[str]
) -> list[str]:
    """Resolve `. <path>` / `source <path>` to scripts on disk by their path suffix."""
    result: list[str] = []
    for cmd in commands:
        if cmd.words[:1] not in (["."], ["source"]) or len(cmd.words) < 2:
            continue
        tail = re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", "", cmd.words[1]).lstrip("/")
        tail = tail.removeprefix("./")
        here = str(Path(script).parent)
        candidates = [p for p in on_disk if p.endswith("/" + tail) or p == tail]
        candidates.sort(key=lambda p: (str(Path(p).parent) != here, p))
        if candidates:
            result.append(candidates[0])
    return result


@dataclass
class Result:
    invocations: list[tuple[int, str]]
    real_exits: list[int]
    echo_handoffs: list[tuple[int, str]] = field(default_factory=list)


def analyse(root: Path, scripts: list[str]) -> dict[str, Result]:
    parsed = {rel: parse(root / rel) for rel in scripts}
    results: dict[str, Result] = {}
    for rel, commands in parsed.items():
        funcs: dict[str, FuncInfo] = {}
        seen: list[str] = []
        queue = sourced_files(commands, rel, scripts)
        while queue:
            lib = queue.pop(0)
            if lib in seen or lib == rel:
                continue
            seen.append(lib)
            collect_functions(parsed[lib], funcs)
            queue.extend(sourced_files(parsed[lib], lib, scripts))
        # The script's own definitions are collected last, but a name defined in both places keeps
        # both bodies: a call then counts if either body makes a real invocation.
        collect_functions(commands, funcs)
        resolve_functions(funcs)
        invocations = []
        exits = []
        handoffs = []
        for cmd in commands:
            if not is_real_mode(cmd):
                continue
            found = invocation(cmd.words, funcs)
            if found:
                invocations.append((cmd.line, found))
            handoff = echo_handoff(cmd.words, funcs)
            if handoff:
                handoffs.append((cmd.line, handoff))
            if (
                cmd.mode == "real"
                and cmd.words[:1] == ["exit"]
                and cmd.words[1:2]
                and cmd.words[1].isdigit()
                and int(cmd.words[1]) != 0
            ):
                exits.append(cmd.line)
        results[rel] = Result(invocations, exits, handoffs)
    return results


def load_classification(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    errors: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"cannot read {path}: {exc}"]
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return {}, [f"{path}: expected an object with a 'scripts' object"]
    for rel, entry in scripts.items():
        if not isinstance(entry, dict) or entry.get("class") not in CLASSES:
            errors.append(f"{rel}: class must be one of {', '.join(CLASSES)}")
        elif not str(entry.get("reason", "")).strip():
            errors.append(f"{rel}: a one-line reason is required")
    return scripts, errors


def check(root: Path, classification: Path, explain: bool = False) -> list[str]:
    scripts = discover(root)
    entries, errors = load_classification(classification)
    for rel in scripts:
        if rel not in entries:
            errors.append(
                f"{rel}: not classified in {classification.name} (add external, pure or fail-closed)"
            )
    for rel in entries:
        if rel not in scripts:
            errors.append(f"{rel}: classified in {classification.name} but not on disk")
    results = analyse(root, scripts)
    for rel in scripts:
        klass = entries.get(rel, {}).get("class")
        res = results[rel]
        if explain:
            calls = ", ".join(
                f"{name} (line {line})" for line, name in res.invocations[:4]
            )
            more = (
                f" +{len(res.invocations) - 4} more" if len(res.invocations) > 4 else ""
            )
            exits = ", ".join(str(n) for n in res.real_exits) or "none"
            handoffs = (
                ", ".join(f"{name} (line {line})" for line, name in res.echo_handoffs)
                or "none"
            )
            print(
                f"{rel} [{klass}]: real invocations: {calls or 'none'}{more}; "
                f"real-only non-zero exits at: {exits}; echo handed to a runner: {handoffs}"
            )
        if klass == "external" and not res.invocations:
            errors.append(
                f"{rel}: classified external, but its real-mode path invokes none of "
                "aws/curl/gitleaks/atx/git clone/install.sh/pwsh or a wrapper of them "
                "(echo/printf/note arguments and dry-run branches do not count): a stub"
            )
        if klass != "pure":
            for line, handoff in res.echo_handoffs:
                errors.append(
                    f"{rel}: line {line}: the real-mode runner call `{handoff} ...` executes a "
                    "command that only prints: a stub step, whatever other steps invoke"
                )
        if klass == "fail-closed" and not res.real_exits:
            errors.append(
                f"{rel}: classified fail-closed, but no literal `exit <non-zero>` sits in a "
                "branch that runs only when APPMOD_DRY_RUN is unset"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="directory holding scripts/"
    )
    parser.add_argument("--classification", type=Path, default=DEFAULT_CLASSIFICATION)
    parser.add_argument(
        "--explain", action="store_true", help="print what was found per script"
    )
    args = parser.parse_args(argv)
    if not (args.root / "scripts").is_dir():
        print(
            f"check_no_stub_scripts: {args.root}/scripts is not a directory",
            file=sys.stderr,
        )
        return 1
    errors = check(args.root, args.classification, args.explain)
    for error in errors:
        print(f"check_no_stub_scripts: {error}", file=sys.stderr)
    if errors:
        return 1
    count = len(discover(args.root))
    print(
        f"check_no_stub_scripts: {count} shell script(s) classified; no stub in a real-mode path"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
