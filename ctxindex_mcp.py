#!/usr/bin/env python3
"""MCP stdio 启动器：可被 MCP 客户端以绝对路径直接拉起。

用法（MCP 配置里）：
    command: python
    args:    ["G:\\trea-Eon-os\\trea-shujukusuoyin-ai\\ctxindex_mcp.py"]

默认索引层级根 = 本工具所在目录的父目录（即同级兄弟目录全部纳入）。
也可用环境变量覆盖：CTXINDEX_ROOT（层级根）、CTXINDEX_INDEX_DIR（索引产物目录）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path  # noqa: E402

from ctxindex import scan  # noqa: E402
from ctxindex.mcp_server import run  # noqa: E402


def main() -> None:
    root = os.environ.get("CTXINDEX_ROOT")
    index_dir = os.environ.get("CTXINDEX_INDEX_DIR")
    run(Path(root) if root else scan.default_level_root(),
        Path(index_dir) if index_dir else (scan.package_root() / ".ctxindex"))


if __name__ == "__main__":
    main()
