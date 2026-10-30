"""全局配置：忽略规则、语言映射、记忆发现模式、提纯权重。"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 身份与目录
# ---------------------------------------------------------------------------
TOOL_DIRNAME = "trea-shujukusuoyin-ai"      # 工具自身目录名（用于自识别）
INDEX_DIRNAME = ".ctxindex"                 # 索引产物目录（放在工具根下）

# ---------------------------------------------------------------------------
# 文件遍历忽略规则
# ---------------------------------------------------------------------------
# 目录名忽略（任意层级命中即跳过该目录）
IGNORE_DIRS = {
    # VCS / 编辑器
    ".git", ".hg", ".svn", ".idea", ".vscode", ".codegraph", ".ctxindex",
    # 依赖 / 构建 / 缓存
    "node_modules", "dist", "out", "target", "obj", "bin", "build", "output",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache",
    ".venv", "venv", "env", ".tox", ".eggs", ".cxx", ".gradle",
    "Pods", ".next", ".nuxt", ".svelte-kit",
    # 本项目特有 / 用户环境
    "backups", "backup", "NVIDIA Corporation", "NVIDIA",
    # 记忆目录交给 memory 索引单独处理，代码遍历跳过避免重复
    ".workbuddy",
}

# 后缀（小写，含点）忽略：二进制 / 大体积 / 生成物
IGNORE_EXTS = {
    ".iso", ".zip", ".7z", ".rar", ".gz", ".tgz", ".bz2", ".xz", ".tar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".obj", ".lib", ".a", ".o",
    ".pdb", ".ilk", ".exp", ".wko", ".class", ".jar", ".war",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".flac", ".ogg",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".db", ".sqlite", ".sqlite3", ".mdb", ".pyc", ".pyo",
    ".wasm", ".node", ".map", ".min.js", ".min.css", ".lock",
}

# 单文件大小上限（字节），超过则只登记不解析内容
MAX_FILE_BYTES = 1_000_000

# ---------------------------------------------------------------------------
# 语言识别（后缀 -> 语言名）
# ---------------------------------------------------------------------------
LANG_BY_EXT = {
    ".py": "python", ".pyw": "python",
    ".wk": "whisker",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp", ".go": "go", ".rs": "rust", ".java": "java",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin",
    ".dart": "dart", ".lua": "lua", ".luau": "lua",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".ps1": "powershell", ".bat": "batch", ".cmd": "batch",
    ".sql": "sql", ".json": "json", ".jsonc": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".xml": "xml", ".html": "html", ".htm": "html", ".css": "css",
    ".vue": "vue", ".svelte": "svelte", ".liquid": "liquid",
    ".md": "markdown", ".markdown": "markdown", ".txt": "text", ".rst": "rst",
}

# 默认纳入代码索引（用于 workspace_map / outline）
CODE_EXTS = set(LANG_BY_EXT.keys())
BIG_TEXT_EXTS = {".md", ".markdown", ".txt", ".rst"}

# ---------------------------------------------------------------------------
# 记忆发现
# ---------------------------------------------------------------------------
# 记忆目录候选（相对项目根）：命中即作为"AI 记忆"来源
MEMORY_DIR_REL = [
    ".workbuddy/memory",
    ".workbuddy",
    "memory",
    ".memory",
    "mem",
]

# 项目根目录下的文档，若命中这些名字片段，视为"项目记忆/文档"
PROJECT_DOC_HINTS = [
    "交接文档", "进度", "readme", "教程", "设计草案", "设计文档", "方案",
    "claude", "agents", "memory", "roadmap", "changelog", "todo", "笔记",
    "note", "docs", "notes",
]

# 额外记忆根（Loomy 用户级记忆等）——逗号分隔，可由环境变量覆盖
_EXTRA_ENV = "CTXINDEX_EXTRA_ROOTS"


def _autodetect_extra_roots() -> list[str]:
    """自动探测 Loomy 用户级记忆目录（如 C:\\Users\\Public\\Loomy\\*\\share\\memory）。"""
    roots: list[str] = []
    candidates = []
    pub = os.environ.get("PUBLIC")
    if pub:
        candidates.append(Path(pub) / "Loomy")
    home = Path.home()
    candidates.append(home / "AppData" / "Local" / "Loomy")
    candidates.append(home / "Loomy")
    for base in candidates:
        try:
            if not base.is_dir():
                continue
            for child in base.iterdir():
                mem = child / "share" / "memory"
                if mem.is_dir():
                    roots.append(str(mem))
        except OSError:
            continue
    return roots


def extra_memory_roots() -> list[str]:
    env = os.environ.get(_EXTRA_ENV, "").strip()
    if env:
        return [p.strip() for p in env.split(",") if p.strip()]
    return _autodetect_extra_roots()


# ---------------------------------------------------------------------------
# 记忆提纯：权重与关键词
# ---------------------------------------------------------------------------
# 进度价值标记 -> (类别, 权重)
PROGRESS_MARKERS = [
    ("里程碑", 3.0, ["里程碑", "达成", "首次", "全通", "打通", "闭环", "fixpoint", "全绿", "发布", "开源"]),
    ("修复", 2.2, ["根因", "修复", "bug", "崩溃", "崩溃点", "坑", "死锁", "回归", "报错"]),
    ("待办", 1.2, ["待办", "todo", "下一步", "计划", "路线", "目标", "未完成"]),
    ("维护", 1.0, ["清理", "重构", "备份", "文档", "整理", "教程"]),
]

# 用于主题标签提取的停用词（中英混合，精简）
STOPWORDS = set("""
的 了 是 在 和 与 及 或 也 就 都 而 但 若 则 于 从 到 对 为 以 被 把 让 使 这 那 些 一个 我们 你们
the a an of to in on for and or is are was were be been being with as at by from it its this that these those
we you they he she it them us our your their my me his her not no yes do does did done have has had will would
can could should may might must shall than then there here what which who whom whose when where why how
""".split())

# ---------------------------------------------------------------------------
# 其他
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
