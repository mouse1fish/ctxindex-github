"""代码大结构提取：把每个文件压成「符号大纲」（不读全文也能知道文件骨架）。

输出 FileOutline：
    - symbols：name / kind / signature / start_line / end_line / doc
    - imports：依赖项
    - summary：一句话概括
支持：Python(AST 精确)、Whisker(.wk 专用)、Markdown(标题)、通用花括号语言(启发式)。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import common


@dataclass
class Symbol:
    name: str
    kind: str          # func / class / method / actor / handler / heading / block
    signature: str
    start_line: int
    end_line: int
    doc: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name, "kind": self.kind, "signature": self.signature,
            "start_line": self.start_line, "end_line": self.end_line, "doc": self.doc,
        }


@dataclass
class FileOutline:
    rel: str
    lang: str
    line_count: int
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    summary: str = ""

    def as_dict(self) -> dict:
        return {
            "rel": self.rel, "lang": self.lang, "line_count": self.line_count,
            "summary": self.summary, "imports": self.imports,
            "symbols": [s.as_dict() for s in self.symbols],
        }


# ---------------------------------------------------------------------------
# Python（AST 精确）
# ---------------------------------------------------------------------------
def _py_sig(node: ast.AST) -> str:
    try:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            if node.args.vararg:
                args.append("*" + node.args.vararg.arg)
            if node.args.kwarg:
                args.append("**" + node.args.kwarg.arg)
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            return f"{prefix} {node.name}({', '.join(args)})"
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases]
            return f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
    except Exception:
        pass
    return getattr(node, "name", "?")


def _py_doc(text: str, start_line: int, end_line: int) -> str:
    return ""


def extract_python(text: str) -> tuple[list[Symbol], list[str]]:
    symbols: list[Symbol] = []
    imports: list[str] = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], []
    lines = text.splitlines()

    def doc_of(node) -> str:
        d = ast.get_docstring(node, clean=True)
        if d:
            return common.truncate(d.replace("\n", " "), 160)
        return ""

    def walk(body, parent: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if parent else "func"
                name = f"{parent}.{node.name}" if parent else node.name
                symbols.append(Symbol(
                    name=name, kind=kind, signature=_py_sig(node),
                    start_line=node.lineno, end_line=getattr(node, "end_lineno", node.lineno),
                    doc=doc_of(node),
                ))
            elif isinstance(node, ast.ClassDef):
                symbols.append(Symbol(
                    name=node.name, kind="class", signature=_py_sig(node),
                    start_line=node.lineno, end_line=getattr(node, "end_lineno", node.lineno),
                    doc=doc_of(node),
                ))
                walk(node.body, parent=node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        imports.append(a.name)
                else:
                    mod = node.module or ""
                    for a in node.names:
                        imports.append(f"{mod}.{a.name}" if mod else a.name)

    walk(tree.body)

    # summary：模块 docstring 或首个符号
    summary = doc_of(tree) or (symbols[0].signature if symbols else "")
    return symbols, imports, summary  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Whisker (.wk)
#   语法：actor Name { ... }、def name[arg: T, ...] -> Ret { ... }、on Handler { ... }
#   字段/常量位于 actor 体一层缩进：name: Type  或  NAME: Type = 值
# ---------------------------------------------------------------------------
_WK_ACTOR = re.compile(r"^\s*actor\s+([A-Za-z_]\w*)")
_WK_DEF = re.compile(
    r"^\s*def\s+([A-Za-z_]\w*)\s*(?:\[([^\]]*)\])?\s*(?:->\s*([A-Za-z_]\w*(?:\[[^\]]*\])?))?")
_WK_ON = re.compile(r"^\s*on\s+([A-Za-z_]\w*)")
_WK_FIELD = re.compile(
    r"^(\s+)([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*(?:\[[^\]]*\])?)\s*(?:=\s*(.*))?$")


def extract_whisker(text: str) -> tuple[list[Symbol], list[str]]:
    lines = text.splitlines()
    symbols: list[Symbol] = []
    cur_actor = ""
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        m = _WK_ACTOR.match(line)
        if m:
            cur_actor = m.group(1)
            symbols.append(Symbol(cur_actor, "actor", f"actor {cur_actor}",
                                  i, _match_brace(lines, i - 1)))
            continue
        m = _WK_DEF.match(line)
        if m:
            args = (m.group(2) or "").strip()
            ret = m.group(3)
            sig = f"def {m.group(1)}" + (f"[{args}]" if args else "")
            if ret:
                sig += f" -> {ret}"
            symbols.append(Symbol(m.group(1), "func", sig, i, _match_brace(lines, i - 1)))
            continue
        m = _WK_ON.match(line)
        if m:
            name = f"{cur_actor}.{m.group(1)}" if cur_actor else m.group(1)
            symbols.append(Symbol(name, "handler", f"on {m.group(1)}",
                                  i, _match_brace(lines, i - 1)))
            continue
        m = _WK_FIELD.match(line)
        if m and len(m.group(1)) == 4:  # 仅 actor 体一层的字段/常量
            name, typ, val = m.group(2), m.group(3), m.group(4)
            kind = "const" if val is not None else "field"
            symbols.append(Symbol(name, kind, f"{name}: {typ}", i, i))
    actor_sig = next((s.signature for s in symbols if s.kind == "actor"), "")
    summary = actor_sig or (symbols[0].signature if symbols else _first_comment(lines))
    return symbols, [], summary


# ---------------------------------------------------------------------------
# Markdown（标题即结构）
# ---------------------------------------------------------------------------
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def extract_markdown(text: str) -> tuple[list[Symbol], list[str]]:
    lines = text.splitlines()
    heads: list[tuple[int, int, str, str]] = []  # level, line, title, raw
    for i, line in enumerate(lines, start=1):
        m = _MD_HEAD.match(line)
        if m:
            heads.append((len(m.group(1)), i, m.group(2), line.strip()))
    symbols: list[Symbol] = []
    for idx, (level, line, title, raw) in enumerate(heads):
        end = (heads[idx + 1][1] - 1) if idx + 1 < len(heads) else len(lines)
        symbols.append(Symbol(title, f"h{level}", raw, line, end))
    summary = heads[0][2] if heads else ""
    return symbols, [], summary


# ---------------------------------------------------------------------------
# 通用花括号语言（启发式）
# ---------------------------------------------------------------------------
_BRACE_LANGS = {"javascript", "typescript", "c", "cpp", "csharp", "go", "rust",
                "java", "php", "swift", "kotlin", "dart", "lua", "shell",
                "powershell", "css", "vue", "svelte", "liquid", "sql", "json",
                "yaml", "toml", "ini", "xml", "html", "batch"}

_CLASS_RE = re.compile(
    r"^\s*(?:export\s+|default\s+|public\s+|private\s+|protected\s+|internal\s+|"
    r"abstract\s+|final\s+|sealed\s+|static\s+|partial\s+|open\s+)*"
    r"(class|struct|interface|enum|trait|impl|record|object|protocol|extension)\s+([A-Za-z_]\w*)")
_FUNC_RE = re.compile(
    r"^\s*(?:export\s+|default\s+|public\s+|private\s+|protected\s+|static\s+|"
    r"async\s+|inline\s+|virtual\s+|override\s+|extern\s+|func\s+|fn\s+|function\s+|"
    r"def\s+|pub\s+|const\s+)*"
    r"([A-Za-z_][\w:<>,\[\]\*&\s]*?)\b([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*"
    r"(\{|$|:)")
_CONTROL = {"if", "for", "while", "switch", "catch", "return", "else", "do",
            "case", "new", "delete", "sizeof", "and", "or", "not", "in", "is",
            "print", "throw", "typeof", "when", "guard", "match", "assert"}
_IMPORT_RE = re.compile(r"^\s*(?:#include|import\s|from\s|require\s*\(|use\s|using\s|@import)")


def _match_brace(text_lines: list[str], start_idx: int) -> int:
    """从 start_idx 行起做花括号配平，返回结束行号（1-based）。"""
    depth = 0
    started = False
    for i in range(start_idx, len(text_lines)):
        line = text_lines[i]
        # 粗略去除字符串/注释
        line = re.sub(r"//.*$", "", line)
        line = re.sub(r"/\*.*?\*/", "", line)
        line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
        line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
        for ch in line:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
        if started and depth <= 0:
            return i + 1
    return len(text_lines)


def extract_generic(text: str, lang: str) -> tuple[list[Symbol], list[str]]:
    lines = text.splitlines()
    symbols: list[Symbol] = []
    imports: list[str] = []
    for i, line in enumerate(lines, start=1):
        if len(line) > 400:
            continue
        if _IMPORT_RE.match(line):
            imports.append(line.strip()[:120])
            continue
        if line.lstrip().startswith(("#", "//", "*", "/*")):
            continue
        mc = _CLASS_RE.match(line)
        if mc:
            end = _match_brace(lines, i - 1)
            symbols.append(Symbol(mc.group(2), mc.group(1), line.strip()[:160], i, end))
            continue
        mf = _FUNC_RE.match(line)
        if mf:
            name = mf.group(2)
            if name.lower() in _CONTROL:
                continue
            end = _match_brace(lines, i - 1) if "{" in line or mf.group(4) == "{" else i
            symbols.append(Symbol(name, "func", line.strip()[:160], i, end))
    summary = symbols[0].signature if symbols else _first_comment(lines)
    return symbols, imports, summary


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------
def _first_comment(lines: list[str]) -> str:
    for line in lines[:20]:
        s = line.strip()
        if s.startswith(("//", "#", "*", "/*")) and len(s) > 3:
            return common.truncate(s.strip("/*# \t"), 120)
    for line in lines[:20]:
        if line.strip():
            return common.truncate(line.strip(), 120)
    return ""


def extract_outline(rel: str, lang: str, text: str) -> FileOutline:
    line_count = text.count("\n") + 1 if text else 0
    if lang == "python":
        symbols, imports, summary = extract_python(text)  # type: ignore[misc]
    elif lang == "whisker":
        symbols, imports, summary = extract_whisker(text)
    elif lang == "markdown":
        symbols, imports, summary = extract_markdown(text)
    elif lang in _BRACE_LANGS:
        symbols, imports, summary = extract_generic(text, lang)
    else:
        symbols, imports = [], []
        summary = _first_comment(text.splitlines())
    return FileOutline(rel=rel, lang=lang, line_count=line_count,
                       symbols=symbols, imports=imports[:40], summary=summary)


def read_and_extract(path: Path, rel: str, lang: str) -> FileOutline | None:
    text = common.read_text(path)
    if text is None:
        return None
    try:
        return extract_outline(rel, lang, text)
    except Exception:
        return FileOutline(rel=rel, lang=lang, line_count=text.count("\n") + 1)
