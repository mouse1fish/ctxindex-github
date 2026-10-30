"""工作区发现与文件遍历。

放置规则（用户约定）：工具落在某个文件夹里，就把「该文件夹的同级兄弟文件夹」
全部纳入索引。即：
    工具目录        = <level>/trea-shujukusuoyin-ai
    索引层级根      = <level>            （默认扫描整个 <level>）
    索引对象        = <level> 下所有子目录（含各兄弟项目）
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import common, config


def package_root() -> Path:
    """返回工具根目录（ctxindex 包的上一级）。"""
    return Path(__file__).resolve().parent.parent


def default_level_root() -> Path:
    """默认索引层级根 = 工具目录的父目录。"""
    return package_root().parent


@dataclass(frozen=True)
class FileInfo:
    path: Path
    rel: str            # 相对 level_root 的 posix 路径
    size: int
    ext: str
    lang: str
    project: str        # 所属顶层项目目录名


def discover_projects(level_root: Path, include: list[str] | None = None,
                      exclude: list[str] | None = None) -> list[Path]:
    """列出 level_root 下的顶层项目目录（同级兄弟目录）。"""
    inc = {s.lower() for s in include} if include else None
    exc = {s.lower() for s in exclude} if exclude else set()
    projects: list[Path] = []
    if not level_root.is_dir():
        return projects
    for child in sorted(level_root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("."):
            continue
        if name in config.IGNORE_DIRS:
            continue
        if inc is not None and name.lower() not in inc:
            continue
        if name.lower() in exc:
            continue
        projects.append(child)
    return projects


def _should_skip_dir(name: str) -> bool:
    return name in config.IGNORE_DIRS or name.startswith(".git")


def iter_source_files(level_root: Path, project: Path,
                      max_files: int | None = None) -> Iterator[FileInfo]:
    """遍历单个项目下的源码/文档文件（已应用忽略规则）。"""
    count = 0
    stack = [project]
    while stack:
        cur = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if _should_skip_dir(entry.name):
                        continue
                    stack.append(entry)
                elif entry.is_file():
                    ext = common.norm_ext(entry)
                    if ext not in config.LANG_BY_EXT:
                        continue
                    if ext in config.IGNORE_EXTS:
                        continue
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        continue
                    if size == 0:
                        continue
                    lang = config.LANG_BY_EXT[ext]
                    yield FileInfo(
                        path=entry,
                        rel=common.rel_to(entry, level_root),
                        size=size,
                        ext=ext,
                        lang=lang,
                        project=project.name,
                    )
                    count += 1
                    if max_files is not None and count >= max_files:
                        return
            except OSError:
                continue


def scan_workspace(level_root: Path, include: list[str] | None = None,
                   exclude: list[str] | None = None) -> tuple[list[Path], list[FileInfo]]:
    """扫描整个层级根，返回 (项目列表, 全部文件信息)。"""
    projects = discover_projects(level_root, include=include, exclude=exclude)
    files: list[FileInfo] = []
    for proj in projects:
        files.extend(iter_source_files(level_root, proj))
    return projects, files


def build_tree(level_root: Path, projects: list[Path],
               file_rels: set[str], max_depth: int = 4) -> dict:
    """构建轻量目录树（只保留含源码的目录与文件）。"""
    tree: dict = {"name": level_root.name, "type": "dir", "children": {}}

    def ensure(parts: list[str]) -> dict:
        node = tree
        for p in parts:
            node = node["children"].setdefault(p, {"name": p, "type": "dir", "children": {}})
        return node

    for rel in sorted(file_rels):
        parts = rel.split("/")
        if len(parts) > max_depth:
            parts = parts[:max_depth] + ["…"]
        ensure(parts[:-1])
        node = ensure(parts[:-1])
        node["children"][parts[-1]] = {"name": parts[-1], "type": "file"}
    return tree


def tree_to_lines(node: dict, prefix: str = "", depth: int = 0,
                  max_depth: int = 4) -> list[str]:
    """把目录树渲染成紧凑文本行。"""
    lines: list[str] = []
    children = node.get("children", {})
    dirs = [c for c in children.values() if c.get("type") == "dir"]
    files = [c for c in children.values() if c.get("type") == "file"]
    dirs.sort(key=lambda c: c["name"])
    files.sort(key=lambda c: c["name"])
    items = dirs + files
    for i, child in enumerate(items):
        last = i == len(items) - 1
        branch = "└─ " if last else "├─ "
        name = child["name"]
        if child.get("type") == "dir":
            lines.append(f"{prefix}{branch}{name}/")
            if depth + 1 < max_depth:
                lines.extend(tree_to_lines(child, prefix + ("   " if last else "│  "),
                                           depth + 1, max_depth))
        else:
            lines.append(f"{prefix}{branch}{name}")
    return lines
