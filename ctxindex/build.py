"""构建编排：扫描 -> 代码索引 -> 记忆提纯 -> 落库 -> 导出。"""
from __future__ import annotations

import time
import warnings
from pathlib import Path

from . import code_index, common, config, memory_index, scan, store


def _existing_meta(store: store.Store) -> dict[str, dict]:
    rows = store.conn.execute("SELECT rel,sha1,size,mtime FROM files").fetchall()
    return {r["rel"]: {"sha1": r["sha1"], "size": r["size"], "mtime": r["mtime"]} for r in rows}


def build(level_root: Path, index_dir: Path, incremental: bool = True,
          include: list[str] | None = None, exclude: list[str] | None = None,
          full: bool = False, log=print) -> dict:
    t0 = time.time()
    warnings.filterwarnings("ignore")  # 静默用户源码里的 SyntaxWarning 等噪声
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    db_path = index_dir / "index.db"
    st = store.Store(db_path)

    if full or not incremental:
        st.clear_all()

    existing = _existing_meta(st) if incremental and not full else {}

    projects, files = scan.scan_workspace(level_root, include=include, exclude=exclude)
    log(f"[scan] 层级根: {level_root}")
    log(f"[scan] 项目 {len(projects)} 个, 源文件 {len(files)} 个")

    seen_rels: set[str] = set()
    changed = 0
    skipped = 0
    failed = 0
    for fi in files:
        seen_rels.add(fi.rel)
        prev = existing.get(fi.rel)
        try:
            stat = fi.path.stat()
        except OSError:
            failed += 1
            continue
        if prev and prev["size"] == fi.size and abs(prev["mtime"] - stat.st_mtime) < 1e-6:
            skipped += 1
            continue
        sha = common.sha1_file(fi.path)
        if prev and prev["sha1"] == sha:
            # 内容未变，只刷新 mtime
            st.conn.execute("UPDATE files SET mtime=? WHERE rel=?", (stat.st_mtime, fi.rel))
            skipped += 1
            continue
        text = common.read_text(fi.path)
        if text is None:
            failed += 1
            continue
        try:
            outline = code_index.extract_outline(fi.rel, fi.lang, text)
        except Exception:
            # 回退：仍登记文件与摘要，保证不出现在文件树里的空档
            summary = ""
            for ln in text.splitlines()[:20]:
                if ln.strip():
                    summary = common.truncate(ln.strip(), 120)
                    break
            outline = code_index.FileOutline(
                rel=fi.rel, lang=fi.lang, line_count=text.count("\n") + 1, summary=summary)
        symbols = [s.as_dict() for s in outline.symbols]
        st.upsert_file({
            "rel": fi.rel, "project": fi.project, "abs_path": str(fi.path),
            "lang": fi.lang, "size": fi.size, "mtime": stat.st_mtime, "sha1": sha,
            "line_count": outline.line_count, "summary": outline.summary,
            "imports": outline.imports,
        }, symbols, body=text)
        changed += 1
        if changed % 200 == 0:
            st.commit()
            log(f"[code] 已处理 {changed} 个文件 …")

    # 删除已消失的文件
    removed = 0
    for rel in list(existing.keys()):
        if rel not in seen_rels:
            st.delete_file(rel)
            removed += 1
    log(f"[code] 更新 {changed} / 跳过 {skipped} / 失败 {failed} / 删除 {removed}")

    # ---- 记忆（每次全量重建，成本低）----
    mem_files: list[tuple[Path, str]] = []
    for proj in projects:
        mem_files.extend(memory_index.discover_memory_files(proj, level_root))
    mem_files.extend(memory_index.discover_user_memory())
    entries, relations = memory_index.distill(level_root, mem_files)
    st.clear_memories()
    for e in entries:
        st.upsert_memory(e.as_dict())
    st.upsert_links(relations)
    log(f"[memory] 记忆文件 {len(mem_files)} 个 -> 条目 {len(entries)} 条, 关联 {len(relations)} 条")

    # ---- 项目 + 目录树 ----
    file_rels = {f.rel for f in files}
    tree_lines_all: list[str] = [f"{level_root.name}/"]
    for proj in projects:
        proj_rels = {r for r in file_rels if r.split("/")[0] == proj.name}
        tree = scan.build_tree(level_root, [proj], proj_rels)
        lines = [f"{proj.name}/"] + scan.tree_to_lines(tree, prefix="", depth=0, max_depth=4)
        st.upsert_project(proj.name, str(proj), "\n".join(lines))
        tree_lines_all.append("")
        tree_lines_all.append("\n".join(lines))

    st.rebuild_fts()
    st.finalize_counts()
    st.set_meta("level_root", str(level_root))
    st.set_meta("built_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    st.set_meta("schema_version", config.SCHEMA_VERSION)
    st.set_meta("version", __import__("ctxindex").__version__)
    st.commit()

    exports = export_all(st, index_dir, level_root, entries, relations)
    stats = st.stats()
    stats.update({"changed": changed, "skipped": skipped, "removed": removed,
                  "elapsed": round(time.time() - t0, 2), "exports": exports,
                  "index_dir": str(index_dir)})
    log(f"[done] {stats['elapsed']}s  文件 {stats['files']} / 符号 {stats['symbols']} / 记忆 {stats['memories']}")
    return stats


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def export_all(st: store.Store, index_dir: Path, level_root: Path,
               entries: list, relations: list) -> list[str]:
    exp = index_dir / "exports"
    exp.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    # tree.txt
    tree_path = exp / "tree.txt"
    lines = [f"{level_root.name}/", f"（生成于 {st.get_meta('built_at')}）", ""]
    for p in st.conn.execute("SELECT name, tree FROM projects ORDER BY name"):
        lines.append(p["tree"] or p["name"])
        lines.append("")
    tree_path.write_text("\n".join(lines), encoding="utf-8")
    written.append(str(tree_path))

    # code-map.md
    cm = exp / "code-map.md"
    buf = ["# 代码结构索引（code-map）", "", f"层级根：`{level_root}`  ·  生成：{st.get_meta('built_at')}", ""]
    for proj in st.conn.execute("SELECT name,file_count,symbol_count,memory_count FROM projects ORDER BY name"):
        buf.append(f"## {proj['name']}  （文件 {proj['file_count']} · 符号 {proj['symbol_count']} · 记忆 {proj['memory_count']}）")
        buf.append("")
        for f in st.conn.execute(
                "SELECT rel,lang,line_count,summary FROM files WHERE project=? ORDER BY rel",
                (proj["name"],)):
            buf.append(f"- `{f['rel']}`  [{f['lang']}, {f['line_count']}行]  {f['summary']}")
            syms = st.conn.execute(
                "SELECT name,kind,signature,start_line FROM symbols WHERE file_rel=? ORDER BY start_line LIMIT 25",
                (f["rel"],)).fetchall()
            for s in syms:
                sig = (s["signature"] or s["name"])[:100]
                buf.append(f"    - L{s['start_line']} [{s['kind']}] {sig}")
        buf.append("")
    cm.write_text("\n".join(buf), encoding="utf-8")
    written.append(str(cm))

    # memory.md
    mm = exp / "memory.md"
    mbuf = ["# AI 记忆脉络索引（memory）", "",
            f"生成：{st.get_meta('built_at')}  ·  条目 {len(entries)} · 关联 {len(relations)}", ""]
    mbuf.append("## 进度时间线")
    mbuf.append("")
    for row in st.timeline(limit=120):
        mbuf.append(f"- {row['date'] or '????'}  [{row['category']}] {row['title']} "
                    f"(用处 {row['usefulness']}) — {row['progress_impact']}")
    mbuf.append("")
    mbuf.append("## 记忆条目（按用处降序）")
    mbuf.append("")
    for e in sorted(entries, key=lambda x: -x.usefulness)[:400]:
        mbuf.append(f"### [{e.usefulness}] {e.title}")
        mbuf.append(f"- 来源：`{e.file_rel}`  ·  日期：{e.date or '-'}  ·  类别：{e.category}  ·  kind：{e.kind}")
        mbuf.append(f"- 主题：{', '.join(e.tags) or '-'}")
        mbuf.append(f"- 实体：{', '.join(e.entities[:10]) or '-'}")
        if e.summary:
            mbuf.append(f"- 摘要：{e.summary}")
        mbuf.append(f"- 进度价值：{e.progress_impact}")
        nb = st.memory_neighbors(e.id, limit=5)
        if nb:
            links = "; ".join(f"{n['title']}({n['reason']},w={n['weight']})" for n in nb)
            mbuf.append(f"- 关联：{links}")
        mbuf.append("")
    mm.write_text("\n".join(mbuf), encoding="utf-8")
    written.append(str(mm))

    return written
