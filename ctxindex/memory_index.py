"""AI 记忆索引与离线提纯。

把「AI 的思考/记忆结构」压成结构化条目：
    - 时间线（date）
    - 主题标签（tags）
    - 实体（文件/函数/错误码/工具）
    - 相互联系（relations：共现实体 / 同主题 / 前后续）
    - 实际用处（category + usefulness 分数）
    - 对项目进度的帮助（progress_impact）

提纯方式：纯离线规则（关键词、标题结构、实体共现、时效衰减），
不依赖任何模型；后续可平滑替换为「小 AI」模型增强。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import common, config

_HEAD_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*$")
_DATE_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
_FRONT_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.S)


@dataclass
class MemoryFile:
    path: Path
    rel: str
    project: str
    kind: str          # long_term / daily / project_doc / user_memory
    title: str
    date: str          # YYYY-MM-DD 或 ""
    entries: list["MemoryEntry"] = field(default_factory=list)


@dataclass
class MemoryEntry:
    id: str
    file_rel: str
    project: str
    kind: str
    date: str
    title: str
    summary: str
    tags: list[str]
    entities: list[str]
    category: str = "资料"
    usefulness: float = 1.0
    progress_impact: str = ""
    start_line: int = 0
    line_count: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id, "file": self.file_rel, "project": self.project,
            "kind": self.kind, "date": self.date, "title": self.title,
            "summary": self.summary, "tags": self.tags, "entities": self.entities,
            "category": self.category, "usefulness": self.usefulness,
            "progress_impact": self.progress_impact,
            "start_line": self.start_line, "line_count": self.line_count,
        }


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------
def discover_memory_files(project: Path, level_root: Path) -> list[tuple[Path, str]]:
    """返回 (文件路径, kind)。kind ∈ long_term / daily / project_doc。"""
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    # 1) 记忆目录
    for rel_dir in config.MEMORY_DIR_REL:
        d = project / rel_dir
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.md")):
            if f in seen:
                continue
            seen.add(f)
            name = f.stem.lower()
            if name in ("memory", "memory_index", "index"):
                kind = "long_term"
            elif _DATE_RE.search(f.stem):
                kind = "daily"
            else:
                kind = "long_term"
            found.append((f, kind))

    # 2) 项目根目录文档
    try:
        root_mds = [f for f in project.iterdir() if f.is_file() and f.suffix.lower() in (".md", ".markdown")]
    except OSError:
        root_mds = []
    for f in sorted(root_mds):
        if f in seen:
            continue
        name = f.name.lower()
        if any(h in name for h in config.PROJECT_DOC_HINTS):
            seen.add(f)
            found.append((f, "project_doc"))

    return found


def discover_user_memory() -> list[tuple[Path, str]]:
    """Loomy 用户级长期记忆目录（可选、自动探测）。"""
    out: list[tuple[Path, str]] = []
    for root_str in config.extra_memory_roots():
        root = Path(root_str)
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.md")):
            out.append((f, "user_memory"))
    return out


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _split_sections(text: str) -> list[tuple[str, int, list[str]]]:
    """按标题切分；返回 (title, start_line, body_lines)。"""
    lines = text.splitlines()
    sections: list[tuple[str, int, list[str]]] = []
    cur_title: str | None = None
    cur_start = 1
    cur_lines: list[str] = []
    for i, line in enumerate(lines, start=1):
        m = _HEAD_RE.match(line)
        if m:
            if cur_title is not None:
                sections.append((cur_title, cur_start, cur_lines))
            cur_title = m.group(2).strip()
            cur_start = i
            cur_lines = []
        else:
            cur_lines.append(line)
    if cur_title is not None:
        sections.append((cur_title, cur_start, cur_lines))
    elif cur_lines and any(l.strip() for l in cur_lines):
        sections.append(("(正文)", 1, cur_lines))
    return sections


def _extract_date(name: str, text: str) -> str:
    m = _DATE_RE.search(name)
    if not m:
        m = _DATE_RE.search(text[:500])
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return ""
    return ""


def _mark(text: str) -> tuple[str, float]:
    for cat, w, kws in config.PROGRESS_MARKERS:
        for k in kws:
            if k in text:
                return cat, w
    return "资料", 0.6


def _progress_impact(category: str, title: str) -> str:
    handlers = {
        "里程碑": "推进关键节点：",
        "修复": "消除阻塞/隐患：",
        "待办": "指向下一步工作：",
        "维护": "巩固工程基础：",
        "资料": "提供背景与依据：",
    }
    return handlers.get(category, "提供背景与依据：") + common.truncate(title, 60)


# ---------------------------------------------------------------------------
# 提纯
# ---------------------------------------------------------------------------
def distill(level_root: Path, mem_files: list[tuple[Path, str]]) -> tuple[list[MemoryEntry], list[dict]]:
    entries: list[MemoryEntry] = []
    # 参考日期（用于时效）
    all_dates: list[str] = []

    raw: list[tuple[MemoryFile, str, int]] = []  # file, title, start_line -> handled below
    tmp: list[tuple[str, str, str, str, str, list[str], int]] = []
    # (file_rel, project, kind, date, title, body_lines, start_line)

    for path, kind in mem_files:
        text = common.read_text(path)
        if not text:
            continue
        text = _FRONT_RE.sub("", text)
        if kind == "user_memory":
            project = "user-memory"
            rel = "user-memory/" + path.name
        else:
            rel = common.rel_to(path, level_root)
            project = rel.split("/")[0] if "/" in rel else path.parent.name
        fdate = _extract_date(path.stem, text)
        if fdate:
            all_dates.append(fdate)
        sections = _split_sections(text)
        file_title = path.stem
        if not sections:
            sections = [(file_title, 1, text.splitlines())]
        for title, start, body_lines in sections:
            body = "\n".join(body_lines).strip()
            if not body and title == file_title:
                continue
            ttl = title or file_title
            tmp.append((rel, project, kind, fdate, ttl, body, start))

    ref_date = max(all_dates) if all_dates else datetime.now().date().isoformat()
    try:
        ref_dt = date.fromisoformat(ref_date)
    except ValueError:
        ref_dt = datetime.now().date()

    for idx, (rel, project, kind, fdate, title, body, start) in enumerate(tmp):
        eid = f"M{idx:05d}"
        tags = common.top_keywords(title + "\n" + body, limit=8)
        entities = common.extract_entities(body + "\n" + title)
        category, _w = _mark(title + "\n" + body)
        usefulness = _score(kind, fdate, ref_dt, body, entities, category)
        summary = _summarize(body)
        entries.append(MemoryEntry(
            id=eid, file_rel=rel, project=project, kind=kind, date=fdate, title=title,
            summary=summary, tags=tags, entities=entities, category=category,
            usefulness=usefulness, progress_impact=_progress_impact(category, title),
            start_line=start, line_count=body.count("\n") + 1 if body else 0,
        ))

    relations = _build_relations(entries)
    return entries, relations


def _summarize(body: str) -> str:
    lines = [l.strip() for l in body.splitlines()]
    picked = []
    for l in lines:
        if not l or l.startswith(("---", "|", "```")):
            continue
        l = re.sub(r"^[-*+]\s+", "", l)
        l = re.sub(r"^\d+[.)]\s+", "", l)
        picked.append(l)
        if len(picked) >= 2:
            break
    return common.truncate(" / ".join(picked), 220)


def _score(kind: str, fdate: str, ref: date, body: str,
           entities: list[str], category: str) -> float:
    score = 0.5
    # 进度价值
    marker_weight = {"里程碑": 3.0, "修复": 2.2, "待办": 1.2, "维护": 1.0, "资料": 0.6}[category]
    score += 0.45 * marker_weight
    # 实体密度
    score += min(len(entities) / 10.0, 1.0)
    # 内容体量（适度加分）
    score += min(len(body) / 1500.0, 0.6)
    # 长期记忆加成
    if kind in ("long_term", "user_memory"):
        score += 1.5
    # 时效衰减（以参考日期为基准）
    if fdate:
        try:
            d = date.fromisoformat(fdate)
            age = (ref - d).days
            if age <= 1:
                score += 1.2
            elif age <= 3:
                score += 1.0
            elif age <= 7:
                score += 0.6
            elif age <= 30:
                score += 0.3
        except ValueError:
            pass
    return round(score, 2)


def _build_relations(entries: list[MemoryEntry]) -> list[dict]:
    """构建记忆相互联系：共现实体 / 共享主题 / 同文件前后续。"""
    relations: list[dict] = []
    # 实体倒排
    ent_index: dict[str, list[str]] = {}
    for e in entries:
        for ent in e.entities:
            ent_index.setdefault(ent, []).append(e.id)
    # 共享主题
    tag_index: dict[str, list[str]] = {}
    for e in entries:
        for t in e.tags:
            tag_index.setdefault(t, []).append(e.id)

    edge_weight: dict[tuple[str, str], float] = {}
    edge_reason: dict[tuple[str, str], set[str]] = {}

    def add(a: str, b: str, w: float, reason: str) -> None:
        if a == b:
            return
        key = tuple(sorted((a, b)))
        edge_weight[key] = edge_weight.get(key, 0) + w
        edge_reason.setdefault(key, set()).add(reason)

    for ent, ids in ent_index.items():
        if 2 <= len(ids) <= 8:
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    add(ids[i], ids[j], 1.0, f"实体:{ent}")
    for tag, ids in tag_index.items():
        if 2 <= len(ids) <= 10:
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    add(ids[i], ids[j], 0.4, f"主题:{tag}")

    # 同文件内按行号前后续
    by_file: dict[str, list[MemoryEntry]] = {}
    for e in entries:
        by_file.setdefault(e.file_rel, []).append(e)
    for rel, group in by_file.items():
        group.sort(key=lambda x: x.start_line)
        for a, b in zip(group, group[1:]):
            add(a.id, b.id, 0.6, "同文件续接")

    # 截断：每节点保留权重最高的若干边
    per_node: dict[str, list[tuple[float, tuple[str, str]]]] = {}
    for key, w in edge_weight.items():
        for node in key:
            per_node.setdefault(node, []).append((w, key))
    keep: set[tuple[str, str]] = set()
    for node, lst in per_node.items():
        lst.sort(key=lambda x: -x[0])
        for _w, key in lst[:8]:
            keep.add(key)

    for key in sorted(keep):
        w = round(edge_weight[key], 2)
        reasons = ";".join(sorted(edge_reason[key])[:3])
        relations.append({"src": key[0], "dst": key[1], "weight": w, "reason": reasons})
    return relations


# ---------------------------------------------------------------------------
# 进度时间线
# ---------------------------------------------------------------------------
def progress_timeline(entries: list[MemoryEntry], project: str | None = None,
                      limit: int = 50) -> list[dict]:
    rows = [e for e in entries if (project is None or e.project == project)]
    rows.sort(key=lambda e: (e.date or "0000-00-00", e.file_rel, e.start_line))
    out = []
    for e in rows[-limit:]:
        out.append({
            "date": e.date or "-", "category": e.category, "project": e.project,
            "title": e.title, "impact": e.progress_impact,
            "usefulness": e.usefulness,
        })
    return out
