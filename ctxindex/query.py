"""查询 / 分层检索 / 上下文打包 API（供 CLI 与 MCP 复用）。

核心：_tiered() 把"沾边的候选"按优先级分成三层下发——
    tier1 直接命中（最可能帮上忙，默认只给这层）
    tier2 关联扩展（记忆关联 / 共享实体主题 / 同文件）
    tier3 语义近似（近义词/词干扩展命中，原词没出现）
"""
from __future__ import annotations

from pathlib import Path

from . import common, rank, store as store_mod


def open_store(index_dir: Path) -> store_mod.Store:
    return store_mod.Store(Path(index_dir) / "index.db")


# ---------------------------------------------------------------------------
# 分层检索（核心）
# ---------------------------------------------------------------------------
def _empty_tier() -> dict:
    return {"symbols": [], "files": [], "memories": [], "content": []}


def _tiered(st: store_mod.Store, query: str, max_each: int = 8, expand: bool = False) -> dict:
    base, extra = rank.analyze(query)
    t1 = _empty_tier()

    # ---- tier1：原词直接命中 ----
    for r in st.cand_symbols(base, 300):
        e = rank.score_symbol(r, base, [])
        if e["direct"]:
            t1["symbols"].append(e)
    for r in st.cand_files(base, 300):
        e = rank.score_file(r, base, [])
        if e["direct"]:
            t1["files"].append(e)
    for r in st.cand_memories(base, 300):
        e = rank.score_memory(r, base, [])
        if e["direct"]:
            t1["memories"].append(e)
    for r in st.cand_content(base, 200):
        e = rank.score_content(r, base, [])
        if e["direct"]:
            t1["content"].append(e)
    for k in t1:
        t1[k].sort(key=lambda x: -x["score"])
        del t1[k][max_each:]

    seen = {
        "symbols": {(x["name"], x["file_rel"]) for x in t1["symbols"]},
        "files": {x["rel"] for x in t1["files"]},
        "memories": {x["id"] for x in t1["memories"]},
        "content": {x["rel"] for x in t1["content"]},
    }

    t2 = _empty_tier()
    t3 = _empty_tier()
    if expand:
        _fill_tier2(st, t1, t2, seen, max_each)
        if extra:
            _fill_tier3(st, extra, t1, t2, t3, seen, max_each)

    return {
        "task": query, "base_terms": base, "extra_terms": extra,
        "workspace_overview": _overview(st),
        "tier1": t1, "tier2": t2, "tier3": t3,
    }


def _fill_tier3(st, extra, t1, t2, t3, seen, max_each):
    """语义近似：仅由扩展词命中，且未出现在 tier1/tier2。"""
    def add(kind, e, key):
        if key in seen[kind]:
            return
        t3[kind].append(e)
        seen[kind].add(key)

    for r in st.cand_symbols(extra, 200):
        e = rank.score_symbol(r, [], extra)
        if e["score"] > 0:
            add("symbols", e, (e["name"], e["file_rel"]))
    for r in st.cand_files(extra, 200):
        e = rank.score_file(r, [], extra)
        if e["score"] > 0:
            add("files", e, e["rel"])
    for r in st.cand_memories(extra, 200):
        e = rank.score_memory(r, [], extra)
        if e["score"] > 0:
            add("memories", e, e["id"])
    for k in t3:
        t3[k].sort(key=lambda x: -x["score"])
        del t3[k][max_each:]


def _fill_tier2(st, t1, t2, seen, max_each):
    """关联扩展：从 tier1 沿记忆关联 / 共享实体 / 同文件扩展。"""
    for m in t1["memories"][:3]:
        for n in st.memory_neighbors(m["id"], 6):
            if n["id"] in seen["memories"]:
                continue
            seen["memories"].add(n["id"])
            t2["memories"].append({
                "kind": "memory", "id": n["id"], "title": n["title"],
                "project": n["project"], "date": n["date"],
                "category": "", "usefulness": n["usefulness"],
                "score": round(1.0 + n["weight"], 2),
                "why": f"关联：{n['reason']}",
            })
    for f in t1["files"][:3]:
        for m in st.memories_by_entity(f["rel"], 5):
            if m["id"] in seen["memories"]:
                continue
            seen["memories"].add(m["id"])
            t2["memories"].append({
                "kind": "memory", "id": m["id"], "title": m["title"],
                "project": m["project"], "date": m["date"],
                "category": m["category"], "usefulness": m["usefulness"],
                "score": round(1.0 + float(m["usefulness"] or 0), 2),
                "why": "提到该文件",
            })
    for s in t1["symbols"][:3]:
        for sym in st.symbols_in_file(s["file_rel"], 10):
            key = (sym["name"], sym["file_rel"])
            if key in seen["symbols"]:
                continue
            seen["symbols"].add(key)
            t2["symbols"].append({
                "kind": "symbol", "name": sym["name"], "signature": sym["signature"],
                "file_rel": sym["file_rel"], "project": s["project"],
                "score": 1.0, "why": f"同文件（{s['name']} 所在）",
            })
    for f in t1["files"][:2]:
        d = f["rel"].rsplit("/", 1)[0] + "/" if "/" in f["rel"] else ""
        for x in st.files_in_dir(d, 10):
            if x["rel"] in seen["files"]:
                continue
            seen["files"].add(x["rel"])
            t2["files"].append({
                "kind": "file", "rel": x["rel"], "project": x["project"],
                "summary": x["summary"], "score": 0.8, "why": "同目录",
            })
    for k in t2:
        t2[k].sort(key=lambda x: -x["score"])
        del t2[k][max_each:]


# ---------------------------------------------------------------------------
# 对外查询
# ---------------------------------------------------------------------------
def search(st: store_mod.Store, q: str, limit: int = 10, expand: bool = False) -> dict:
    return _tiered(st, q, max_each=limit, expand=expand)


def context(st: store_mod.Store, task: str, max_each: int = 8, expand: bool = False) -> dict:
    return _tiered(st, task, max_each=max_each, expand=expand)


def file_outline(st: store_mod.Store, path: str) -> dict | None:
    f = st.get_file(path)
    if not f:
        return None
    return {
        "rel": f["rel"], "project": f["project"], "lang": f["lang"],
        "line_count": f["line_count"], "summary": f["summary"],
        "imports": f["imports"],
        "symbols": [
            {"name": s["name"], "kind": s["kind"], "signature": s["signature"],
             "lines": f"L{s['start_line']}-{s['end_line']}", "doc": s.get("doc", "")}
            for s in f["symbols"]
        ],
    }


def list_files(st: store_mod.Store, project: str | None = None, lang: str | None = None,
               keyword: str | None = None, limit: int = 200) -> list[dict]:
    return st.list_files(project=project, lang=lang, keyword=keyword, limit=limit)


def memory_timeline(st: store_mod.Store, project: str | None = None, limit: int = 50) -> list[dict]:
    return st.timeline(project=project, limit=limit)


def memory_about(st: store_mod.Store, topic: str, limit: int = 10) -> dict:
    hits = st.search_memories(topic, limit)
    enriched = []
    for h in hits:
        mid = h["id"]
        full = st.get_memory(mid) or {}
        full["neighbors"] = st.memory_neighbors(mid, limit=5)
        enriched.append(full)
    return {"topic": topic, "memories": enriched}


def memory_get(st: store_mod.Store, mid: str) -> dict | None:
    d = st.get_memory(mid)
    if d:
        d["neighbors"] = st.memory_neighbors(mid, limit=8)
    return d


def workspace_map(st: store_mod.Store, project: str | None = None, max_lines: int = 400) -> str:
    buf: list[str] = []
    rows = st.conn.execute("SELECT name, tree FROM projects ORDER BY name").fetchall()
    for r in rows:
        if project and r["name"] != project:
            continue
        buf.append(r["tree"] or r["name"])
        buf.append("")
    text = "\n".join(buf)
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]) + f"\n…（已截断，共 {len(lines)} 行）"
    return text


def _overview(st: store_mod.Store) -> dict:
    rows = st.conn.execute(
        "SELECT name, file_count, symbol_count, memory_count FROM projects "
        "WHERE file_count>0 OR memory_count>0 ORDER BY file_count DESC").fetchall()
    return {"projects": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 文本格式化
# ---------------------------------------------------------------------------
def _count(t: dict) -> int:
    return sum(len(v) for v in t.values())


def fmt_context(pack: dict, expand: bool = False) -> str:
    b = [f"# 上下文包：{pack['task']}", ""]
    ov = pack["workspace_overview"]["projects"]
    if ov:
        b.append("## 工作区概览")
        for p in ov:
            b.append(f"- {p['name']}：文件 {p['file_count']} · 符号 {p['symbol_count']} · 记忆 {p['memory_count']}")
        b.append("")
    b.extend(_fmt_tier("第一层 · 直接命中（最可能帮上忙）", pack["tier1"]))
    if expand:
        b.extend(_fmt_tier("第二层 · 关联扩展（卡住了再看）", pack["tier2"]))
        b.extend(_fmt_tier("第三层 · 语义近似（换种说法的相关项）", pack["tier3"]))
    elif _count(pack["tier1"]) == 0 and _count(pack["tier3"]) > 0:
        b.append("> 直接命中为空，以下给出语义近似结果（可再 `expand` 取关联项）。")
        b.append("")
        b.extend(_fmt_tier("语义近似", pack["tier3"]))
    elif not expand:
        b.append("> 以上为第一层。若信息不足，用 `expand=true` 获取第二层（关联）与第三层（语义近似）。")
        b.append("")
    b.append("> 提示：以上为索引摘要；确需完整实现时，再单独读对应文件的指定行段。")
    return "\n".join(b)


def _fmt_tier(title: str, t: dict) -> list[str]:
    b = [f"## {title}", ""]
    if _count(t) == 0:
        b.append("（无）")
        b.append("")
        return b
    if t.get("memories"):
        b.append("**记忆**")
        for m in t["memories"]:
            b.append(f"- [{m.get('category','')}|用处{m.get('usefulness','')}] {m['title']}"
                     f"（{m.get('date','') or '-'}·{m.get('project','')}）  ← {m.get('why','')}")
        b.append("")
    if t.get("symbols"):
        b.append("**符号**")
        for s in t["symbols"]:
            b.append(f"- `{s.get('signature') or s.get('name')}` → {s.get('file_rel')}"
                     f"  ← {s.get('why','')}")
        b.append("")
    if t.get("files"):
        b.append("**文件**")
        for f in t["files"]:
            b.append(f"- `{f['rel']}`  {common.truncate(f.get('summary','') or '', 100)}"
                     f"  ← {f.get('why','')}")
        b.append("")
    if t.get("content"):
        b.append("**正文片段**")
        for c in t["content"]:
            b.append(f"- `{c['rel']}`：{common.truncate(c.get('snippet',''), 220)}")
        b.append("")
    return b


def fmt_search(res: dict, expand: bool = False) -> str:
    return fmt_context(res, expand=expand)


def fmt_outline(o: dict) -> str:
    b = [f"# {o['rel']}  [{o['lang']}, {o['line_count']} 行]", ""]
    if o["summary"]:
        b.append(f"摘要：{o['summary']}")
    if o["imports"]:
        b.append(f"依赖：{', '.join(o['imports'][:20])}")
    b.append("")
    b.append("## 结构大纲")
    for s in o["symbols"]:
        doc = f"  — {s['doc']}" if s.get("doc") else ""
        b.append(f"- {s['lines']} [{s['kind']}] {s['signature']}{doc}")
    return "\n".join(b)


def fmt_timeline(rows: list[dict]) -> str:
    b = ["# 进度时间线", ""]
    for r in rows:
        b.append(f"- {r['date'] or '????'} [{r['category']}] {r['title']} "
                 f"(用处 {r['usefulness']}) — {r['progress_impact']}")
    return "\n".join(b)


def fmt_memory_about(res: dict) -> str:
    b = [f"# 关于「{res['topic']}」的记忆", ""]
    for m in res["memories"]:
        b.append(f"## {m.get('title','')}  [{m.get('category','')}|用处{m.get('usefulness','')}]")
        b.append(f"- 来源：`{m.get('file_rel','')}`  日期：{m.get('date','-')}  kind：{m.get('kind','')}")
        if m.get("summary"):
            b.append(f"- 摘要：{m['summary']}")
        if m.get("entities"):
            b.append(f"- 实体：{', '.join(m['entities'][:12])}")
        if m.get("progress_impact"):
            b.append(f"- 进度价值：{m['progress_impact']}")
        nb = m.get("neighbors") or []
        if nb:
            b.append("- 关联：" + "; ".join(f"{n['title']}({n['reason']})" for n in nb))
        b.append("")
    if not res["memories"]:
        b.append("（无相关记忆）")
    return "\n".join(b)
