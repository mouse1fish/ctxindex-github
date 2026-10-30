"""命令行入口：python -m ctxindex <命令>"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import build as build_mod
from . import config, query, scan
from .mcp_server import run as run_mcp


def _default_paths(root: str | None):
    level_root = Path(root).resolve() if root else scan.default_level_root()
    index_dir = scan.package_root() / config.INDEX_DIRNAME
    return level_root, index_dir


def _fix_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ctxindex", description="项目脉络索引（代码结构 + AI 记忆）")
    p.add_argument("--root", help="索引层级根（默认：工具目录的父目录）")
    sub = p.add_subparsers(dest="cmd", required=True)

    bp = sub.add_parser("build", help="构建/刷新索引")
    bp.add_argument("--full", action="store_true", help="全量重建")
    bp.add_argument("--include", help="只索引这些项目（逗号分隔）")
    bp.add_argument("--exclude", help="排除这些项目（逗号分隔）")

    sub.add_parser("status", help="查看索引统计")

    sp = sub.add_parser("search", help="分层检索")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--expand", action="store_true", help="追加次级（关联+语义近似）")

    fp = sub.add_parser("files", help="列出文件")
    fp.add_argument("--project")
    fp.add_argument("--lang")
    fp.add_argument("--keyword")
    fp.add_argument("--limit", type=int, default=200)

    op = sub.add_parser("outline", help="查看文件大结构")
    op.add_argument("path")

    mp = sub.add_parser("memory", help="记忆：时间线或按主题")
    mp.add_argument("--project")
    mp.add_argument("--topic")
    mp.add_argument("--limit", type=int, default=50)

    cp = sub.add_parser("context", help="上下文打包（分层）")
    cp.add_argument("task")
    cp.add_argument("--max-each", type=int, default=8)
    cp.add_argument("--expand", action="store_true", help="追加次级（关联+语义近似）")

    wp = sub.add_parser("map", help="目录树总览")
    wp.add_argument("--project")

    wa = sub.add_parser("watch", help="轮询监测文件变化并自动重建")
    wa.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒")

    sub.add_parser("mcp", help="启动 stdio MCP 服务")
    return p


def _staleness_signature(level_root: Path, include, exclude) -> tuple:
    """给工作区做一个廉价的变更指纹（文件数 + 最大 mtime + 总大小）。"""
    projects = scan.discover_projects(level_root, include, exclude)
    newest = 0.0
    total = 0
    count = 0
    for proj in projects:
        for fi in scan.iter_source_files(level_root, proj):
            try:
                stt = fi.path.stat()
            except OSError:
                continue
            count += 1
            total += stt.st_size
            newest = max(newest, stt.st_mtime)
    return (count, total, round(newest, 3))


def main(argv: list[str] | None = None) -> int:
    _fix_stdout()
    args = build_parser().parse_args(argv)
    level_root, index_dir = _default_paths(args.root)

    if args.cmd == "build":
        build_mod.build(level_root, index_dir, incremental=not args.full, full=args.full,
                        include=_split(args.include), exclude=_split(args.exclude))
        return 0

    if args.cmd == "mcp":
        run_mcp(level_root, index_dir)
        return 0

    if args.cmd == "watch":
        return _watch(level_root, index_dir, args.interval, getattr(args, "include", None),
                      getattr(args, "exclude", None))

    st = query.open_store(index_dir)
    if not _has_index(st):
        print("索引为空，请先运行：python -m ctxindex build", file=sys.stderr)
        return 2

    if args.cmd == "status":
        import json
        print(json.dumps(st.stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "search":
        ex = getattr(args, "expand", False)
        print(query.fmt_search(query.search(st, args.query, args.limit, expand=ex), expand=ex))
    elif args.cmd == "files":
        rows = query.list_files(st, args.project, args.lang, args.keyword, args.limit)
        for r in rows:
            print(f"{r['rel']}  [{r['lang']}, {r['line_count']}行]  {r['summary']}")
    elif args.cmd == "outline":
        o = query.file_outline(st, args.path)
        print(query.fmt_outline(o) if o else f"未找到文件：{args.path}")
    elif args.cmd == "memory":
        if args.topic:
            print(query.fmt_memory_about(query.memory_about(st, args.topic, args.limit)))
        else:
            print(query.fmt_timeline(query.memory_timeline(st, args.project, args.limit)))
    elif args.cmd == "context":
        ex = getattr(args, "expand", False)
        print(query.fmt_context(query.context(st, args.task, args.max_each, expand=ex), expand=ex))
    elif args.cmd == "map":
        print(query.workspace_map(st, args.project))
    return 0


def _watch(level_root: Path, index_dir: Path, interval: float, include, exclude) -> int:
    print(f"监视中：{level_root}（每 {interval}s 检查一次，Ctrl+C 退出）")
    build_mod.build(level_root, index_dir, incremental=True,
                    include=include, exclude=exclude)
    last = _staleness_signature(level_root, include, exclude)
    try:
        while True:
            time.sleep(interval)
            cur = _staleness_signature(level_root, include, exclude)
            if cur != last:
                print(f"[watch] 检测到变更 {last} -> {cur}，重建索引 …")
                build_mod.build(level_root, index_dir, incremental=True,
                                include=include, exclude=exclude)
                last = cur
    except KeyboardInterrupt:
        print("\n已停止监视。")
        return 0


def _split(s: str | None) -> list[str] | None:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def _has_index(st) -> bool:
    try:
        return st.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] > 0 or \
            st.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] > 0
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
