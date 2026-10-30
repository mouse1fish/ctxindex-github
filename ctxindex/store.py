"""索引存储层：SQLite + FTS5（不可用时自动降级为 LIKE）。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import common, config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY, path TEXT, tree TEXT,
    file_count INTEGER DEFAULT 0, symbol_count INTEGER DEFAULT 0,
    memory_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
    rel TEXT PRIMARY KEY, project TEXT, abs_path TEXT, lang TEXT,
    size INTEGER, mtime REAL, sha1 TEXT, line_count INTEGER,
    summary TEXT, symbols_json TEXT, imports_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_project ON files(project);
CREATE INDEX IF NOT EXISTS idx_files_lang ON files(lang);
CREATE TABLE IF NOT EXISTS files_body (rel TEXT PRIMARY KEY, body TEXT);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_rel TEXT, project TEXT, name TEXT, kind TEXT, signature TEXT,
    start_line INTEGER, end_line INTEGER, doc TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_rel);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, project TEXT, file_rel TEXT, kind TEXT, date TEXT,
    title TEXT, summary TEXT, tags TEXT, entities TEXT, category TEXT,
    usefulness REAL, progress_impact TEXT, start_line INTEGER, line_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_memories_date ON memories(date);
CREATE TABLE IF NOT EXISTS memory_links (
    src TEXT, dst TEXT, weight REAL, reason TEXT, PRIMARY KEY (src, dst)
);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_files USING fts5(rel, project, summary, symbols, body);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_symbols USING fts5(name, signature, doc, file_rel, project);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_memories USING fts5(title, summary, tags, entities, project, id);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.has_fts = False
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        try:
            self.conn.executescript(_FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError:
            self.has_fts = False
        self.conn.commit()

    # -- meta -------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                          (key, json.dumps(value, ensure_ascii=False)))

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # -- 写入 -------------------------------------------------------------
    def clear_all(self) -> None:
        for t in ("files", "files_body", "symbols", "memories", "memory_links", "projects"):
            self.conn.execute(f"DELETE FROM {t}")
        if self.has_fts:
            for t in ("fts_files", "fts_symbols", "fts_memories"):
                self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()

    def upsert_project(self, name: str, path: str, tree: str = "") -> None:
        self.conn.execute(
            "INSERT INTO projects(name,path,tree) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET path=excluded.path, tree=excluded.tree",
            (name, path, tree))

    def existing_hashes(self) -> dict[str, str]:
        return {r["rel"]: r["sha1"] for r in self.conn.execute("SELECT rel, sha1 FROM files")}

    def delete_file(self, rel: str) -> None:
        self.conn.execute("DELETE FROM files WHERE rel=?", (rel,))
        self.conn.execute("DELETE FROM files_body WHERE rel=?", (rel,))
        self.conn.execute("DELETE FROM symbols WHERE file_rel=?", (rel,))

    def upsert_file(self, f: dict, symbols: list[dict], body: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO files(rel,project,abs_path,lang,size,mtime,sha1,"
            "line_count,summary,symbols_json,imports_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f["rel"], f["project"], f["abs_path"], f["lang"], f["size"], f["mtime"],
             f["sha1"], f["line_count"], f["summary"],
             json.dumps(symbols, ensure_ascii=False),
             json.dumps(f.get("imports", []), ensure_ascii=False)))
        self.conn.execute("INSERT OR REPLACE INTO files_body(rel,body) VALUES(?,?)",
                          (f["rel"], (body or "")[:60000]))
        self.conn.execute("DELETE FROM symbols WHERE file_rel=?", (f["rel"],))
        self.conn.executemany(
            "INSERT INTO symbols(file_rel,project,name,kind,signature,start_line,end_line,doc)"
            " VALUES(?,?,?,?,?,?,?,?)",
            [(f["rel"], f["project"], s["name"], s["kind"], s["signature"],
              s["start_line"], s["end_line"], s.get("doc", "")) for s in symbols])

    def upsert_memory(self, m: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO memories(id,project,file_rel,kind,date,title,summary,"
            "tags,entities,category,usefulness,progress_impact,start_line,line_count)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["id"], m["project"], m["file"], m["kind"], m["date"], m["title"],
             m["summary"], json.dumps(m["tags"], ensure_ascii=False),
             json.dumps(m["entities"], ensure_ascii=False), m["category"],
             m["usefulness"], m["progress_impact"], m["start_line"], m["line_count"]))

    def upsert_links(self, links: Iterable[dict]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO memory_links(src,dst,weight,reason) VALUES(?,?,?,?)",
            [(l["src"], l["dst"], l["weight"], l["reason"]) for l in links])

    def clear_memories(self) -> None:
        self.conn.execute("DELETE FROM memories")
        self.conn.execute("DELETE FROM memory_links")

    def rebuild_fts(self) -> None:
        if not self.has_fts:
            return
        for t in ("fts_files", "fts_symbols", "fts_memories"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.execute(
            "INSERT INTO fts_files(rel,project,summary,symbols,body) "
            "SELECT rel, project, IFNULL(summary,''), "
            "(SELECT group_concat(name||' '||IFNULL(signature,''),' ') FROM symbols s WHERE s.file_rel=files.rel), "
            "IFNULL((SELECT body FROM files_body b WHERE b.rel=files.rel),'') "
            "FROM files")
        self.conn.execute(
            "INSERT INTO fts_symbols(name,signature,doc,file_rel,project) "
            "SELECT name, IFNULL(signature,''), IFNULL(doc,''), file_rel, project FROM symbols")
        self.conn.execute(
            "INSERT INTO fts_memories(title,summary,tags,entities,project,id) "
            "SELECT title, IFNULL(summary,''), IFNULL(tags,''), IFNULL(entities,''), project, id FROM memories")

    def finalize_counts(self) -> None:
        for p in self.conn.execute("SELECT name FROM projects"):
            name = p["name"]
            fc = self.conn.execute("SELECT COUNT(*) c FROM files WHERE project=?", (name,)).fetchone()["c"]
            sc = self.conn.execute("SELECT COUNT(*) c FROM symbols WHERE project=?", (name,)).fetchone()["c"]
            mc = self.conn.execute("SELECT COUNT(*) c FROM memories WHERE project=?", (name,)).fetchone()["c"]
            self.conn.execute("UPDATE projects SET file_count=?,symbol_count=?,memory_count=? WHERE name=?",
                              (fc, sc, mc, name))
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    # -- 查询 -------------------------------------------------------------
    def stats(self) -> dict:
        def one(sql: str) -> int:
            return self.conn.execute(sql).fetchone()[0]
        by_lang = [dict(r) for r in self.conn.execute(
            "SELECT lang, COUNT(*) c FROM files GROUP BY lang ORDER BY c DESC")]
        by_project = [dict(r) for r in self.conn.execute(
            "SELECT name, file_count, symbol_count, memory_count FROM projects ORDER BY name")]
        return {
            "files": one("SELECT COUNT(*) FROM files"),
            "symbols": one("SELECT COUNT(*) FROM symbols"),
            "memories": one("SELECT COUNT(*) FROM memories"),
            "links": one("SELECT COUNT(*) FROM memory_links"),
            "projects": by_project,
            "languages": by_lang,
            "fts": self.has_fts,
            "built_at": self.get_meta("built_at"),
        }

    def list_files(self, project: str | None = None, lang: str | None = None,
                   keyword: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT rel, project, lang, size, line_count, summary FROM files WHERE 1=1"
        args: list[Any] = []
        if project:
            sql += " AND project=?"; args.append(project)
        if lang:
            sql += " AND lang=?"; args.append(lang)
        if keyword:
            sql += " AND rel LIKE ?"; args.append(f"%{keyword}%")
        sql += " ORDER BY rel LIMIT ?"; args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def get_file(self, rel_like: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM files WHERE rel=? OR rel LIKE ? ORDER BY LENGTH(rel) LIMIT 1",
            (rel_like, f"%{rel_like}")).fetchone()
        if not row:
            return None
        d = dict(row)
        d["symbols"] = json.loads(d.pop("symbols_json") or "[]")
        d["imports"] = json.loads(d.pop("imports_json") or "[]")
        return d

    def search_symbols(self, q: str, limit: int = 20) -> list[dict]:
        if self.has_fts:
            try:
                rows = self.conn.execute(
                    "SELECT name, signature, doc, file_rel, project FROM fts_symbols "
                    "WHERE fts_symbols MATCH ? ORDER BY rank LIMIT ?",
                    (_fts_query(q), limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{q}%"
        return [dict(r) for r in self.conn.execute(
            "SELECT name, signature, doc, file_rel, project FROM symbols "
            "WHERE name LIKE ? OR signature LIKE ? LIMIT ?", (like, like, limit))]

    def search_files(self, q: str, limit: int = 20) -> list[dict]:
        if self.has_fts:
            try:
                rows = self.conn.execute(
                    "SELECT rel, project, summary, "
                    "snippet(fts_files, 4, '⟦', '⟧', ' … ', 12) AS snip "
                    "FROM fts_files WHERE fts_files MATCH ? ORDER BY rank LIMIT ?",
                    (_fts_query(q), limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{q}%"
        return [dict(r) for r in self.conn.execute(
            "SELECT rel, project, summary, '' AS snip FROM files "
            "WHERE rel LIKE ? OR summary LIKE ? LIMIT ?", (like, like, limit))]

    def search_content(self, q: str, limit: int = 20, context_chars: int = 200) -> list[dict]:
        """在文件正文里检索，返回带命中片段的文件（替代 grep + 读整文件）。"""
        if self.has_fts:
            try:
                rows = self.conn.execute(
                    "SELECT rel, project, "
                    "snippet(fts_files, 4, '⟦', '⟧', ' … ', 16) AS snippet "
                    "FROM fts_files WHERE fts_files MATCH ? ORDER BY rank LIMIT ?",
                    (_fts_query(q), limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{q}%"
        rows = self.conn.execute(
            "SELECT rel, project, body FROM files_body WHERE body LIKE ? LIMIT ?",
            (like, limit)).fetchall()
        out = []
        for r in rows:
            body = r["body"] or ""
            i = body.lower().find(q.lower())
            seg = body[max(0, i - 60): i + context_chars] if i >= 0 else body[:context_chars]
            out.append({"rel": r["rel"], "project": r["project"], "snippet": seg})
        return out

    # -- 候选检索（供分层打分；terms 为词列表）----------------------------
    def cand_symbols(self, terms: list[str], limit: int = 300) -> list[dict]:
        if not terms:
            return []
        if self.has_fts:
            try:
                rows = self.conn.execute(
                    "SELECT name,signature,doc,file_rel,project FROM fts_symbols "
                    "WHERE fts_symbols MATCH ? LIMIT ?", (_fts_or(terms), limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        clause = " OR ".join(["name LIKE ? OR signature LIKE ? OR doc LIKE ?"] * len(terms))
        args: list[Any] = []
        for t in terms:
            args += [f"%{t}%"] * 3
        args.append(limit)
        return [dict(r) for r in self.conn.execute(
            f"SELECT name,signature,doc,file_rel,project FROM symbols WHERE {clause} LIMIT ?", args)]

    def cand_files(self, terms: list[str], limit: int = 300) -> list[dict]:
        if not terms:
            return []
        if self.has_fts:
            try:
                rows = self.conn.execute(
                    "SELECT rel,project,summary,symbols FROM fts_files "
                    "WHERE fts_files MATCH ? LIMIT ?", (_fts_or(terms), limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        clause = " OR ".join(["rel LIKE ? OR summary LIKE ?"] * len(terms))
        args: list[Any] = []
        for t in terms:
            args += [f"%{t}%"] * 2
        args.append(limit)
        return [dict(r) for r in self.conn.execute(
            f"SELECT rel,project,summary,'' AS symbols FROM files WHERE {clause} LIMIT ?", args)]

    def cand_memories(self, terms: list[str], limit: int = 300) -> list[dict]:
        if not terms:
            return []
        if self.has_fts:
            try:
                rows = self.conn.execute(
                    "SELECT m.id,m.title,m.summary,m.tags,m.entities,m.project,m.date,"
                    "m.category,m.usefulness FROM fts_memories "
                    "JOIN memories m ON m.id=fts_memories.id "
                    "WHERE fts_memories MATCH ? LIMIT ?", (_fts_or(terms), limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        clause = " OR ".join(
            ["title LIKE ? OR summary LIKE ? OR tags LIKE ? OR entities LIKE ?"] * len(terms))
        args: list[Any] = []
        for t in terms:
            args += [f"%{t}%"] * 4
        args.append(limit)
        return [dict(r) for r in self.conn.execute(
            "SELECT id,title,summary,tags,entities,project,date,category,usefulness "
            f"FROM memories WHERE {clause} LIMIT ?", args)]

    def cand_content(self, terms: list[str], limit: int = 200) -> list[dict]:
        if not terms:
            return []
        if self.has_fts:
            try:
                rows = self.conn.execute(
                    "SELECT rel,project,snippet(fts_files,4,'⟦','⟧',' … ',16) AS snippet "
                    "FROM fts_files WHERE fts_files MATCH ? LIMIT ?",
                    (_fts_or(terms), limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        clause = " OR ".join(["body LIKE ?"] * len(terms))
        args = [f"%{t}%" for t in terms]
        args.append(limit)
        return [dict(r) for r in self.conn.execute(
            f"SELECT rel,project,substr(body,1,200) AS snippet FROM files_body "
            f"WHERE {clause} LIMIT ?", args)]

    def memories_by_entity(self, text: str, limit: int = 8) -> list[dict]:
        like = f"%{text}%"
        return [dict(r) for r in self.conn.execute(
            "SELECT id,title,summary,tags,entities,project,date,category,usefulness FROM memories "
            "WHERE entities LIKE ? OR title LIKE ? LIMIT ?", (like, like, limit))]

    def symbols_in_file(self, rel: str, limit: int = 12) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT name,kind,signature,start_line,end_line,file_rel FROM symbols "
            "WHERE file_rel=? ORDER BY start_line LIMIT ?", (rel, limit))]

    def files_in_dir(self, rel_prefix: str, limit: int = 8) -> list[dict]:
        like = f"{rel_prefix}%"
        return [dict(r) for r in self.conn.execute(
            "SELECT rel,project,lang,line_count,summary FROM files WHERE rel LIKE ? LIMIT ?",
            (like, limit))]


    def search_memories(self, q: str, limit: int = 20) -> list[dict]:
        if self.has_fts:
            try:
                rows = self.conn.execute(
                    "SELECT m.id,m.title,m.summary,m.tags,m.entities,m.project,"
                    "m.date,m.category,m.usefulness "
                    "FROM fts_memories JOIN memories m ON m.id=fts_memories.id "
                    "WHERE fts_memories MATCH ? ORDER BY rank LIMIT ?",
                    (_fts_query(q), limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{q}%"
        return [dict(r) for r in self.conn.execute(
            "SELECT id,title,summary,tags,entities,project,date,category,usefulness FROM memories "
            "WHERE title LIKE ? OR summary LIKE ? OR tags LIKE ? OR entities LIKE ? LIMIT ?",
            (like, like, like, like, limit))]

    def get_memory(self, mid: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tags"] = json.loads(d["tags"] or "[]")
        d["entities"] = json.loads(d["entities"] or "[]")
        return d

    def memory_neighbors(self, mid: str, limit: int = 12) -> list[dict]:
        rows = self.conn.execute(
            "SELECT src,dst,weight,reason FROM memory_links WHERE src=? OR dst=? "
            "ORDER BY weight DESC LIMIT ?", (mid, mid, limit)).fetchall()
        out = []
        for r in rows:
            other = r["dst"] if r["src"] == mid else r["src"]
            d = self.get_memory(other)
            if d:
                out.append({"weight": r["weight"], "reason": r["reason"],
                            "id": d["id"], "title": d["title"], "project": d["project"],
                            "date": d["date"], "usefulness": d["usefulness"]})
        return out

    def timeline(self, project: str | None = None, limit: int = 50) -> list[dict]:
        sql = ("SELECT id,date,project,category,title,progress_impact,usefulness "
               "FROM memories WHERE date != ''")
        args: list[Any] = []
        if project:
            sql += " AND project=?"; args.append(project)
        sql += " ORDER BY date ASC, project ASC, start_line ASC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]


def _fts_query(q: str) -> str:
    """把用户输入转成安全的 FTS5 前缀查询。"""
    parts = [p for p in common.tokenize(q)]
    if not parts:
        parts = [t for t in q.replace('"', " ").split() if t]
    return _fts_or(parts)


def _fts_or(terms: list[str]) -> str:
    parts = [t for t in (terms or []) if t]
    if not parts:
        return '""'
    return " OR ".join(f'"{p}"*' for p in parts)
