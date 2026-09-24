"""把 corpus 按 session 落成文件，并提供只读记忆工具后端。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


def header(sid: int, date: str) -> str:
    return f"[周期{sid} | 日期 {date}] "


@dataclass
class Doc:
    session_id: int
    date: str
    doc_id: str
    doc_type: str
    content: str
    path: Path

    @property
    def labeled(self) -> str:
        return f"{header(self.session_id, self.date)}[{self.doc_type} | {self.doc_id}]\n{self.content}"


class MemoryStore:
    def __init__(self, sessions: list[dict], workspace: Path):
        self.workspace = Path(workspace)
        self.docs: list[Doc] = []
        self.by_id: dict[str, Doc] = {}
        self._materialize(sessions)

    def _materialize(self, sessions: list[dict]) -> None:
        root = self.workspace / "sessions"
        root.mkdir(parents=True, exist_ok=True)
        index_lines = ["# 记忆索引（按周期先后）", ""]
        for s in sessions:
            sid = int(s["session_id"])
            date = s["date"]
            folder = root / f"s{sid:02d}_{date}"
            folder.mkdir(parents=True, exist_ok=True)
            index_lines.append(f"## 周期 {sid} | {date} | {len(s.get('docs') or [])} 篇")
            for i, raw in enumerate(s.get("docs") or []):
                doc_id = str(raw.get("doc_id") or f"s{sid}_doc_{i}")
                doc_type = str(raw.get("type") or raw.get("doc_type") or "文档")
                content = str(raw.get("content") or "")
                fname = f"{i:02d}_{_safe(doc_type)}_{_safe(doc_id)}.md"
                path = folder / fname
                body = f"# {header(sid, date)}{doc_type}  `{doc_id}`\n\n{content}\n"
                path.write_text(body, encoding="utf-8")
                doc = Doc(sid, date, doc_id, doc_type, content, path)
                self.docs.append(doc)
                self.by_id[doc_id] = doc
                index_lines.append(f"- `{doc_id}` ({doc_type}) {path.relative_to(self.workspace)}")
            index_lines.append("")
        (self.workspace / "INDEX.md").write_text("\n".join(index_lines), encoding="utf-8")

    @classmethod
    def from_workspace(cls, workspace: Path) -> "MemoryStore":
        """只读已落盘的 sessions/，不重写语料。给 MCP / 后处理共用。"""
        inst = object.__new__(cls)
        inst.workspace = Path(workspace)
        inst.docs = []
        inst.by_id = {}
        root = inst.workspace / "sessions"
        if not root.is_dir():
            return inst
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(inst.workspace)
            sid, date = 0, ""
            if len(rel.parts) >= 2:
                m = re.match(r"s(\d+)_(\d{4}-\d{2}-\d{2})", rel.parts[1])
                if m:
                    sid = int(m.group(1))
                    date = m.group(2)
            content = path.read_text(encoding="utf-8", errors="ignore")
            doc = Doc(sid, date, path.stem, "文档", content, path)
            inst.docs.append(doc)
            inst.by_id[path.stem] = doc
        return inst

    def list_sessions_text(self) -> str:
        groups: dict[int, list[Doc]] = {}
        for d in self.docs:
            groups.setdefault(d.session_id, []).append(d)
        lines = []
        for sid in sorted(groups):
            ds = groups[sid]
            lines.append(f"周期{sid} 日期={ds[0].date} 文档数={len(ds)}")
        return "\n".join(lines) or "(空)"

    def list_docs_text(self, session_id: int) -> str:
        rows = [d for d in self.docs if d.session_id == int(session_id)]
        if not rows:
            return f"(周期{session_id} 无文档)"
        lines = [f"{d.doc_id}\t{d.doc_type}\t{d.date}\t{_preview(d.content)}" for d in rows]
        return "\n".join(lines)

    def read_doc_text(self, doc_id: str) -> str:
        d = self.by_id.get(doc_id)
        if d is None:
            return f"(找不到 doc_id={doc_id!r}。请先 grep_memory 或 list_sessions。)"
        return d.labeled

    def read_file_text(self, rel_path: str) -> str:
        rel = (rel_path or "").strip().lstrip("./")
        if not rel:
            return "(空路径)"
        path = (self.workspace / rel).resolve()
        try:
            path.relative_to(self.workspace.resolve())
        except ValueError:
            return f"(拒绝读取工作区外路径: {rel_path!r})"
        if not path.is_file():
            return f"(找不到文件 {rel_path!r}。可用 grep_memory 返回的相对路径。)"
        return path.read_text(encoding="utf-8", errors="ignore")

    def grep_text(self, query: str, limit_files: int = 80) -> str:
        """整库子串检索，不按「先到先得 12 条」截断晚期周期。"""
        q = (query or "").strip()
        if not q:
            return "(空查询)"
        qlow = q.lower()
        hits: list[tuple[int, str, str]] = []
        for d in self.docs:
            if (
                qlow in d.content.lower()
                or qlow in d.doc_id.lower()
                or q in d.doc_type
                or qlow in d.path.name.lower()
            ):
                rel = str(d.path.relative_to(self.workspace))
                hits.append((d.session_id, rel, _snippet(d.content, q)))
        if not hits:
            return f"(未命中: {q})"
        total = len(hits)
        if total > limit_files:
            shown = hits[:20] + hits[-(limit_files - 20) :]
            note = f"命中 {total} 个文件，展示前 20 + 后 {limit_files - 20}（含晚期周期）"
        else:
            shown = hits
            note = f"命中 {total} 个文件"
        blocks = [
            f"[周期{sid} | {rel}]\n{snip}" for sid, rel, snip in shown
        ]
        return note + "\n\n" + "\n\n".join(blocks)

    def search_text(self, query: str, limit: int = 80) -> str:
        return self.grep_text(query, limit_files=limit)


def _safe(s: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", s, flags=re.UNICODE)
    return (s or "x")[:40]


def _preview(text: str, n: int = 60) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t[:n] + ("…" if len(t) > n else "")


def _snippet(text: str, query: str, window: int = 160) -> str:
    low = text.lower()
    q = query.lower()
    i = low.find(q)
    if i < 0:
        return _preview(text, window)
    a = max(0, i - window // 3)
    b = min(len(text), i + len(query) + window)
    chunk = text[a:b].replace("\n", " ")
    prefix = "…" if a else ""
    suffix = "…" if b < len(text) else ""
    return prefix + chunk + suffix
