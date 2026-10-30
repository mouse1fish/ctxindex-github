# ctxindex · 项目脉络索引

> 给 AI 用的「**代码结构 + 记忆提纯**」实时索引。
> 把项目文件树、每个文件的**大结构**，以及 AI 的**思考/记忆结构**压成紧凑、可检索的索引，
> 让 AI 直接查索引，而不是每次都把整个文件读一遍。

**纯 Python 标准库** · **SQLite + FTS5** · **100% 本地** · **CLI + MCP 服务**

---

## 它解决什么

AI 每接手一个任务，都要反复 `grep / glob / Read` 去摸清项目结构，浪费算力与时间。
本项目把「项目上下文」提纯成结构化索引，让 AI **查索引**而不是**读整文件**：

- **文件结构** → 目录树总览
- **代码大结构** → 每个文件的符号 / 函数签名 / 行范围 / 依赖（不必读全文）
- **正文检索** → 命中片段直接给出（替代 `grep + Read`）
- **AI 记忆结构** → 时间线、主题、实体、相互联系、实际用处、对项目进度的帮助

## 核心特性

- **多语言代码大纲**：Python（AST 精确）、Whisker `.wk`（专用）、Markdown（标题树）、
  C/C++/JS/TS/Go/Rust/Java/C#/Vue/Svelte 等（启发式）
- **记忆提纯（离线规则）**：发现记忆 → 切分 → 提纯（摘要/主题/实体/类别/有用度/进度价值）→ 关联成图
- **分层检索**：相似就抽取，按优先级下发——`第一层 直接命中` / `第二层 关联扩展` / `第三层 语义近似`
- **上下文打包**：一个任务描述，一次拿回相关记忆 + 符号 + 文件 + 正文片段
- **MCP 服务**：11 个工具，AI Agent 可直接调用
- **实时性**：增量构建 + `watch` 监听变更 + `rebuild_index` 工具随时刷新
- **零依赖、纯本地**：不联网、不上传数据、无第三方库

---

## 快速开始

需要 Python 3.10+（仅标准库）。

```bash
# 1) 构建索引（首次全量；默认扫描本工具所在目录的"同级兄弟目录"）
python -m ctxindex build --full

# 2) 查询
python -m ctxindex status                       # 统计
python -m ctxindex context "你的任务或问题"        # 上下文打包（默认只给第一层）
python -m ctxindex context "你的任务" --expand    # 信息不足时，追加关联+语义近似
python -m ctxindex search "关键词"
python -m ctxindex outline 路径/到/文件.py         # 文件大结构
python -m ctxindex memory --topic "某主题"         # 相关记忆 + 关联
python -m ctxindex memory --limit 30             # 进度时间线
python -m ctxindex map                           # 目录树总览

# 3) 实时监听（轮询变更自动重建）
python -m ctxindex watch --interval 5

# 4) 启动 MCP 服务（供 AI 客户端调用）
python -m ctxindex mcp
```

**放置规则**：把本工具放在某个目录下，它会默认索引「**该目录的同级兄弟目录**」全部。
想收窄范围：`python -m ctxindex build --include 项目A,项目B`。

---

## 接入 MCP（给 AI 用）

配置一个 **stdio MCP 服务**，命令指向 `ctxindex_mcp.py` 即可：

```json
{
  "mcpServers": {
    "ctxindex": {
      "type": "local",
      "command": ["python", "/绝对路径/ctxindex_mcp.py"],
      "environment": {}
    }
  }
}
```

可用环境变量：`CTXINDEX_ROOT`（索引层级根）、`CTXINDEX_INDEX_DIR`（索引产物目录）、
`CTXINDEX_EXTRA_ROOTS`（额外记忆根，逗号分隔）。

### MCP 工具

| 工具 | 用途 |
|---|---|
| `context` | **首选**：默认给第一层直接命中；`expand=true` 追加关联 + 语义近似 |
| `search` | 分层检索（符号/文件/正文/记忆），同上 |
| `search_code` | 代码正文检索，返回命中片段（替代 grep + 读整文件） |
| `find_symbol` | 按名找定义位置（文件:行） |
| `file_outline` | 单文件大结构（不读整文件） |
| `memory_timeline` | 项目进度时间线 |
| `memory_about` | 按主题取记忆 + 关联 + 进度价值 |
| `workspace_map` | 目录树总览 |
| `list_files` | 过滤列文件 + 摘要 |
| `index_status` | 索引健康与统计 |
| `rebuild_index` | 刷新索引，保证结果最新 |

---

## 分层检索：相似就抽取 + 按优先级下发

| 层 | 含义 | 何时给 |
|---|---|---|
| **第一层** 直接命中 | 原查询词在名称/签名/摘要/记忆标题里直接出现（按综合得分排序） | **默认只给这层** |
| **第二层** 关联扩展 | 从第一层沿「记忆关联 / 共享实体主题 / 同文件同目录」扩展 | `expand=true` |
| **第三层** 语义近似 | 仅由近义词/词干扩展命中（如「自举」↔「bootstrap」） | `expand=true`；第一层为空时自动兜底 |

综合得分 = 名称/标题命中×6 + 摘要/签名命中×3~2 + 实体主题命中×2 + 记忆有用度×0.35 + 近义扩展×1.4，
跨「代码 + 记忆」统一排序。语义层当前用内置近义词表 + 词干，后续可替换为 embedding。

---

## 目录结构

```
ctxindex/
├─ config.py          忽略规则 / 语言映射 / 记忆发现 / 提纯权重
├─ common.py          路径、哈希、文本、分词、实体抽取
├─ scan.py            工作区发现 + 文件遍历 + 目录树
├─ code_index.py      多语言「文件大结构」提取
├─ memory_index.py    记忆发现 + 离线提纯 + 关联构建
├─ rank.py            相关度打分 + 三层分级（直接/关联/语义）
├─ store.py           SQLite + FTS5 存储与检索
├─ build.py           构建编排 + 导出
├─ query.py           查询 / 上下文打包 API
├─ mcp_server.py      stdio MCP 服务
└─ cli.py             命令行入口
ctxindex_mcp.py       MCP 启动器
ctxindex.cmd          命令行包装器（Windows）
```

生成的索引位于 `.ctxindex/`（`index.db` + `exports/` 里的 tree.txt / code-map.md / memory.md），
可安全删除后重建。

---

## 覆盖范围与忽略规则

- 默认扫描**层级根**（工具目录的父目录）下的**所有同级兄弟目录**。
- 自动忽略：`.git`、`node_modules`、`dist`、`build`、`backups`、`__pycache__`、`.venv` 等；
  以及 `.iso/.zip/.exe/.dll/.bin/图片/音视频` 等二进制与大文件；单文件超过 1 MB 只登记不解析。
- 记忆来源：`.workbuddy/memory/*.md`、项目根的 `交接文档/进度/README/教程/设计草案` 等文档。
- 自定义：改 `ctxindex/config.py` 的 `IGNORE_DIRS` / `IGNORE_EXTS`，或用 `--include` / `--exclude`。

---

## 设计文档

原理、数据模型、提纯规则、分层算法等详见 [`方案设计文档-项目脉络索引.md`](方案设计文档-项目脉络索引.md)。

---

## 许可证 · License

本项目采用 **PolyForm Noncommercial License 1.0.0**（见 [LICENSE](LICENSE)）：

- ✅ **允许**：个人学习、研究、实验、非商业项目；学校、科研机构、公益组织、政府机构等非营利使用；
  以及在这些许可范围内的复制、修改与再分发（需保留许可证与署名）。
- ❌ **禁止**：任何**商业用途**（含把本软件用于商业产品 / 商业服务、作为商业产品的一部分等）。

> 说明：**"开源"若严格按 OSI 定义，是不允许限制商用的**。因此本项目准确说是
> **"源码公开、仅限非商业使用"（source-available, noncommercial）**。
> 如需商业授权，请另行联系作者取得许可。

使用前请将 `LICENSE` 中的 `Required Notice` 一行替换为你（或你的组织）的名字，例如：

```
Required Notice: Copyright (c) 2026 <你的名字或组织>
```

---

## 免责声明

软件按「原样」提供，不附带任何明示或暗示的担保。
