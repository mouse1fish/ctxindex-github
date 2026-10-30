"""通用工具：路径、哈希、文本读取、分词、截断。"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from . import config


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: Path) -> str | None:
    """尽量稳健地读文本；失败返回 None。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > config.MAX_FILE_BYTES:
        # 只解析前 MAX 字节，避免大文件拖垮
        data = data[: config.MAX_FILE_BYTES]
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def norm_ext(path: Path) -> str:
    return path.suffix.lower()


def lang_of(path: Path) -> str | None:
    return config.LANG_BY_EXT.get(norm_ext(path))


def is_probably_binary(path: Path) -> bool:
    if norm_ext(path) in config.IGNORE_EXTS:
        return True
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    return False


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


_WS_RE = re.compile(r"[ \t\u3000]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 分词（中英混合，用于主题标签）
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]{2,}")


def tokenize(text: str) -> list[str]:
    out = []
    for tok in _TOKEN_RE.findall(text or ""):
        low = tok.lower()
        if low in config.STOPWORDS:
            continue
        if len(low) < 2:
            continue
        out.append(low)
    return out


def top_keywords(text: str, limit: int = 8) -> list[str]:
    from collections import Counter

    counts = Counter(tokenize(text))
    # 过滤纯数字
    items = [(w, c) for w, c in counts.items() if not w.isdigit()]
    items.sort(key=lambda x: (-x[1], x[0]))
    return [w for w, _ in items[:limit]]


# ---------------------------------------------------------------------------
# 实体抽取（文件名 / 函数名 / 错误码 / 工具名）
# ---------------------------------------------------------------------------
_ENTITY_RES = [
    (re.compile(r"[\w./\\-]+\.(?:py|wk|wko|js|ts|tsx|c|cpp|h|hpp|go|rs|java|md|sh|ps1|bat|exe|json|txt)\b"), 1.0),
    (re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}(?=\()"), 0.8),          # 函数/方法名 name(
    (re.compile(r"0x[0-9A-Fa-f]{4,}"), 1.0),                            # 错误码/地址
    (re.compile(r"\b(?:rt_[a-z0-9_]+|[a-z0-9_]+\.(?:py|sh|wk))\b"), 0.9),
    (re.compile(r"\bSTATUS_[A-Z_]+\b"), 1.0),
]


def extract_entities(text: str, limit: int = 24) -> list[str]:
    seen: dict[str, float] = {}
    for rx, w in _ENTITY_RES:
        for m in rx.findall(text or ""):
            key = m.strip()
            if len(key) < 2:
                continue
            seen[key] = seen.get(key, 0) + w
    items = sorted(seen.items(), key=lambda x: (-x[1], x[0]))
    return [k for k, _ in items[:limit]]


def rel_to(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
