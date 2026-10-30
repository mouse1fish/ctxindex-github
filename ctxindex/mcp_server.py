"""stdio MCP 服务：把索引能力暴露给 AI Agent（纯标准库，无第三方依赖）。

传输：newline-delimited JSON-RPC 2.0（MCP stdio 规范）。
日志一律走 stderr，stdout 只输出协议消息。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from . import build as build_mod
from . import query, scan

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ctxindex"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
ctxindex 是本机项目的「代码结构 + AI 记忆」实时索引。
优先用它来定位代码与历史决策，而不是反复 grep / 读整个文件：
- 先 `context`（给一个任务/问题，默认只给第一层"直接命中"：最可能帮上忙的记忆+符号+文件+正文片段）。
- **信息不足或卡住时**，再调 `context(expand=true)` / `search(expand=true)`：
  追加第二层（关联扩展：记忆关联、共享实体主题、同文件同目录）与第三层（语义近似：换种说法的相关项）。
- 看某文件骨架用 `file_outline`；看正文命中片段用 `search_code`；查历史决策/踩过的坑用 `memory_about` / `memory_timeline`。
- 索引可能不是最新：必要时调用 `rebuild_index` 刷新，再查询。
只有确认要看完整实现时，才单独读取对应文件的指定行段。
"""


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


TOOLS = [
    {
        "name": "index_status",
        "description": "查看索引健康状态与统计（文件数/符号数/记忆数/分项目与语言分布/构建时间）。",
        "inputSchema": _schema({}),
    },
    {
        "name": "workspace_map",
        "description": "获取工作区目录树（文件结构总览）。可按 project 过滤，返回紧凑文本树。",
        "inputSchema": _schema({"project": {"type": "string", "description": "项目名，可选"}}),
    },
    {
        "name": "list_files",
        "description": "按项目/语言/路径关键词过滤列出已索引文件及其一句话摘要。",
        "inputSchema": _schema({
            "project": {"type": "string"},
            "lang": {"type": "string"},
            "keyword": {"type": "string", "description": "匹配路径片段"},
            "limit": {"type": "integer", "default": 200},
        }),
    },
    {
        "name": "file_outline",
        "description": "取某个文件的大结构（符号/函数签名/行范围/依赖），不必读整文件。",
        "inputSchema": _schema({"path": {"type": "string", "description": "相对路径或路径片段"}},
                               ["path"]),
    },
    {
        "name": "search",
        "description": "分层检索：默认只返回第一层（直接命中：符号+文件+正文片段+记忆）；"
                       "信息不足时传 expand=true 追加第二层（关联扩展）与第三层（语义近似/近义词）。",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
            "expand": {"type": "boolean", "default": False,
                       "description": "true=卡住时再要次级（关联+语义近似）"},
        }, ["query"]),
    },
    {
        "name": "search_code",
        "description": "在代码正文里检索，返回带上下文的命中片段（替代 grep + 读整文件）。",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        }, ["query"]),
    },
    {
        "name": "find_symbol",
        "description": "按符号名查找定义位置（文件 + 行范围 + 签名）。",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 20},
        }, ["query"]),
    },
    {
        "name": "memory_timeline",
        "description": "项目进度时间线（按日期，含类别/用处/进度价值）。",
        "inputSchema": _schema({
            "project": {"type": "string"},
            "limit": {"type": "integer", "default": 50},
        }),
    },
    {
        "name": "memory_about",
        "description": "按主题取相关记忆条目，含摘要、实体、错误码、进度价值与相互关联。",
        "inputSchema": _schema({
            "topic": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        }, ["topic"]),
    },
    {
        "name": "context",
        "description": "上下文打包：给一个任务/问题，默认只给第一层直接命中（最可能帮上忙）；"
                       "信息不足时传 expand=true 追加第二层（关联扩展）与第三层（语义近似）。",
        "inputSchema": _schema({
            "task": {"type": "string"},
            "max_each": {"type": "integer", "default": 8},
            "expand": {"type": "boolean", "default": False,
                       "description": "true=卡住时再要次级（关联+语义近似）"},
        }, ["task"]),
    },
    {
        "name": "rebuild_index",
        "description": "重建/刷新索引（增量），保证查询结果是最新的。",
        "inputSchema": _schema({"full": {"type": "boolean", "default": False}}),
    },
]


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
class Server:
    def __init__(self, level_root: Path, index_dir: Path):
        self.level_root = Path(level_root)
        self.index_dir = Path(index_dir)
        self._st = None

    @property
    def st(self):
        if self._st is None:
            self._st = query.open_store(self.index_dir)
        return self._st

    def _reopen(self):
        if self._st is not None:
            try:
                self._st.conn.close()
            except Exception:
                pass
        self._st = None

    # -- 工具实现 ---------------------------------------------------------
    def call_tool(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            return self._dispatch(name, args or {}), False
        except Exception as e:  # noqa: BLE001
            return f"错误：{e}\n{traceback.format_exc()}", True

    def _dispatch(self, name: str, a: dict) -> str:
        st = self.st
        if name == "index_status":
            return _json(st.stats())
        if name == "workspace_map":
            return query.workspace_map(st, project=a.get("project"))
        if name == "list_files":
            rows = query.list_files(st, a.get("project"), a.get("lang"),
                                    a.get("keyword"), int(a.get("limit", 200)))
            return "\n".join(f"- `{r['rel']}` [{r['lang']}, {r['line_count']}行] {r['summary']}"
                             for r in rows) or "（无）"
        if name == "file_outline":
            o = query.file_outline(st, a["path"])
            return query.fmt_outline(o) if o else f"未找到文件：{a['path']}"
        if name == "search":
            ex = bool(a.get("expand", False))
            return query.fmt_search(query.search(st, a["query"], int(a.get("limit", 10)), expand=ex), expand=ex)
        if name == "search_code":
            hits = st.search_content(a["query"], int(a.get("limit", 10)))
            if not hits:
                return "（无命中）"
            return "\n".join(f"- `{h['rel']}`：{(h.get('snippet') or '').replace(chr(10),' ')[:300]}"
                             for h in hits)
        if name == "find_symbol":
            rows = st.search_symbols(a["query"], int(a.get("limit", 20)))
            return "\n".join(f"- `{r['signature'] or r['name']}` → {r['file_rel']}"
                             for r in rows) or "（无）"
        if name == "memory_timeline":
            return query.fmt_timeline(query.memory_timeline(st, a.get("project"), int(a.get("limit", 50))))
        if name == "memory_about":
            return query.fmt_memory_about(query.memory_about(st, a["topic"], int(a.get("limit", 10))))
        if name == "context":
            ex = bool(a.get("expand", False))
            return query.fmt_context(query.context(st, a["task"], int(a.get("max_each", 8)), expand=ex), expand=ex)
        if name == "rebuild_index":
            res = build_mod.build(self.level_root, self.index_dir,
                                  incremental=not a.get("full", False),
                                  full=bool(a.get("full", False)),
                                  log=lambda *x: print("[ctxindex]", *x, file=sys.stderr))
            self._reopen()
            return f"重建完成：文件 {res['files']} / 符号 {res['symbols']} / 记忆 {res['memories']} / 用时 {res['elapsed']}s"
        raise ValueError(f"未知工具：{name}")

    # -- 协议 -------------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return _ok(mid, {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            text, is_err = self.call_tool(name, args)
            return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": is_err})
        if mid is None:
            return None
        return _err(mid, -32601, f"未知方法：{method}")

    def serve(self) -> None:
        out = sys.stdout
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = self.handle(msg)
            if resp is not None:
                out.write(json.dumps(resp, ensure_ascii=False) + "\n")
                out.flush()


def _ok(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def run(level_root: Path | None = None, index_dir: Path | None = None) -> None:
    level_root = Path(level_root) if level_root else scan.default_level_root()
    index_dir = Path(index_dir) if index_dir else (scan.package_root() / ".ctxindex")
    Server(level_root, index_dir).serve()


if __name__ == "__main__":
    run()
