"""相关度打分与分层：把"沾边的东西"按优先级分成三层。

- 第一层（tier1，直接命中）：原查询词在名称/签名/摘要/记忆标题里直接出现
- 第二层（tier2，关联扩展）：从第一层沿"记忆关联 / 共享实体主题 / 同文件"扩展到的
- 第三层（tier3，语义近似）：仅由近义词/词干/模糊扩展命中（原词没出现）

语义层当前用轻量近似（中英近义词表 + 词干），待接入小模型后可替换为 embedding。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 近义词 / 领域同义组（双向展开）
# ---------------------------------------------------------------------------
_SYNONYM_GROUPS = [
    {"自举", "引导", "bootstrap", "selfhost", "self-host", "自编译"},
    {"根因", "原因", "缘由", "cause", "reason", "rootcause"},
    {"编译", "compile", "compiler", "编译期"},
    {"解析", "parse", "parser", "语法分析"},
    {"词法", "lex", "lexer", "token", "词法分析"},
    {"发射", "emit", "codegen", "代码生成"},
    {"调度", "schedule", "scheduler", "dispatch", "轮询"},
    {"崩溃", "crash", "异常", "exception", "报错", "error", "访问违例"},
    {"内存", "memory", "alloc", "分配", "堆", "栈"},
    {"性能", "performance", "perf", "优化", "optimize", "提速"},
    {"测试", "test", "verify", "校验", "验收", "回归"},
    {"并发", "concurrency", "actor", "线程", "thread", "协程"},
    {"索引", "index", "检索", "搜索", "search"},
    {"记忆", "memory", "note", "笔记", "日志", "log"},
    {"结构", "structure", "outline", "大纲", "骨架"},
    {"架构", "architecture", "设计", "design", "分层", "模块"},
    {"修复", "fix", "patch", "修正", "改好"},
    {"调试", "debug", "断点", "breakpoint", "探针", "probe"},
    {"接口", "interface", "api", "abi", "调用约定"},
    {"文件", "file", "目录", "dir", "路径", "path"},
    {"进度", "progress", "状态", "status", "里程碑", "milestone"},
    {"待办", "todo", "计划", "plan", "路线", "roadmap", "下一步"},
    {"网络", "network", "socket", "tcp", "套接字"},
]

_SYNONYMS: dict[str, set[str]] = {}


def _build_synonyms() -> None:
    for group in _SYNONYM_GROUPS:
        g = {w.lower() for w in group}
        for w in g:
            _SYNONYMS.setdefault(w, set()).update(g - {w})


_build_synonyms()


def stem_variants(tok: str) -> set[str]:
    """极轻量词干：英文常见后缀剥离。"""
    out: set[str] = set()
    if not tok.isascii():
        return out
    for suf in ("ing", "ed", "es", "s", "er", "ion"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            out.add(tok[: -len(suf)])
    return out


def analyze(q: str) -> tuple[list[str], list[str]]:
    """返回 (base 原词, extra 仅扩展词)。"""
    from . import common

    base = [t for t in common.tokenize(q)]
    if not base:
        base = [t.lower() for t in re.split(r"[^\w\u4e00-\u9fff]+", q) if t]
    base_set = set(base)
    expanded: set[str] = set(base_set)
    for t in base:
        for s in _SYNONYMS.get(t, ()):  # 近义词
            expanded.add(s)
        expanded.update(stem_variants(t))  # 词干
    extra = sorted(expanded - base_set)
    return base, extra


# ---------------------------------------------------------------------------
# 命中计数与打分
# ---------------------------------------------------------------------------
def _hits(terms: list[str], text: str) -> int:
    if not terms or not text:
        return 0
    low = text.lower()
    return sum(1 for t in terms if t and t in low)


def score_symbol(row: dict, base: list[str], extra: list[str]) -> dict:
    name = row.get("name") or ""
    sig = row.get("signature") or ""
    doc = row.get("doc") or ""
    dn = _hits(base, name)
    df = _hits(base, sig + " " + doc)
    de = _hits(extra, name + " " + sig + " " + doc)
    score = 6.0 * dn + 2.0 * df + 1.5 * de
    direct = (dn + df) > 0
    why = []
    if dn:
        why.append(f"名称命中×{dn}")
    if df:
        why.append(f"签名/注释命中×{df}")
    if de and not direct:
        why.append(f"近义扩展×{de}")
    return {
        "kind": "symbol", "name": row.get("name"), "signature": sig,
        "file_rel": row.get("file_rel"), "project": row.get("project"),
        "score": round(score, 2), "direct": direct, "why": ";".join(why),
    }


def score_file(row: dict, base: list[str], extra: list[str]) -> dict:
    rel = row.get("rel") or ""
    summary = row.get("summary") or ""
    syms = row.get("symbols") or ""
    drel, dsum, dsym = _hits(base, rel), _hits(base, summary), _hits(base, syms)
    eall = _hits(extra, rel + " " + summary + " " + syms)
    score = 5.0 * drel + 3.0 * dsum + 2.0 * dsym + 1.2 * eall
    direct = (drel + dsum + dsym) > 0
    why = []
    if drel:
        why.append("路径命中")
    if dsum:
        why.append("摘要命中")
    if dsym:
        why.append("符号命中")
    if eall and not direct:
        why.append(f"近义扩展×{eall}")
    return {
        "kind": "file", "rel": rel, "project": row.get("project"),
        "summary": summary, "score": round(score, 2), "direct": direct, "why": ";".join(why),
    }


def score_memory(row: dict, base: list[str], extra: list[str]) -> dict:
    title = row.get("title") or ""
    summary = row.get("summary") or ""
    tags = row.get("tags") or ""
    ents = row.get("entities") or ""
    dt, ds = _hits(base, title), _hits(base, summary)
    dg = _hits(base, tags + " " + ents)
    eall = _hits(extra, title + " " + summary + " " + tags + " " + ents)
    useful = float(row.get("usefulness") or 0)
    score = 6.0 * dt + 3.0 * ds + 2.0 * dg + 0.35 * useful + 1.4 * eall
    direct = (dt + ds + dg) > 0
    why = []
    if dt:
        why.append("标题命中")
    if ds:
        why.append("摘要命中")
    if dg:
        why.append("主题/实体命中")
    if eall and not direct:
        why.append(f"近义扩展×{eall}")
    return {
        "kind": "memory", "id": row.get("id"), "title": title,
        "project": row.get("project"), "date": row.get("date"),
        "category": row.get("category"), "usefulness": useful,
        "score": round(score, 2), "direct": direct, "why": ";".join(why),
    }


def score_content(row: dict, base: list[str], extra: list[str]) -> dict:
    snip = row.get("snippet") or ""
    dn = _hits(base, snip)
    de = _hits(extra, snip)
    score = 4.0 * dn + 1.2 * de
    return {
        "kind": "content", "rel": row.get("rel"), "project": row.get("project"),
        "snippet": snip.replace("\n", " "), "score": round(score, 2),
        "direct": dn > 0, "why": ("正文命中" if dn else f"近义扩展×{de}"),
    }
