"""Ingest → hybrid search → permissions → rerank → grounded generation → audit."""
import hashlib
import io
import json
import math
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ABSTAIN = "I don't know based on the available, permitted documents."
ACCESS_DENIED = "You do not have access to the requested data. Please contact an admin."
STOP = set("a an the is are was were be to of for in on and or how what when where who do does can i my it with this that should".split())


def terms(text):
    return [t for t in re.findall(r"[\w]+", text.lower()) if t not in STOP]


def pages(content, filename):
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        result = [(n, page.extract_text() or "") for n, page in enumerate(PdfReader(io.BytesIO(content)).pages, 1)]
    elif suffix in {".txt", ".md"}:
        result = [(1, content.decode("utf-8"))]
    else:
        raise ValueError("Supported formats: PDF, TXT and Markdown.")
    if not any(text.strip() for _, text in result):
        raise ValueError("No extractable text. Scanned PDFs need OCR before ingestion.")
    return result


def chunk_pages(document_pages, size=250, overlap=40):
    if not 0 <= overlap < size:
        raise ValueError("Chunk overlap must be nonnegative and smaller than chunk size.")
    result = []
    for page, text in document_pages:
        words = text.replace("\x00", " ").split()
        for start in range(0, len(words), size - overlap):
            result.append((page, " ".join(words[start:start + size])))
            if start + size >= len(words):
                break
    return result


def source_quote(quote, source):
    """Match PDF layout noise only; return the original, auditable source span."""
    if quote in source:
        return quote

    def canonical(text):
        chars, positions = [], []
        for i, char in enumerate(text):
            # PDF bullets/control glyphs may be omitted when the model copies a list.
            char = " " if char.isspace() or ord(char) < 32 or ord(char) == 127 or char in "•■▪●" else char
            if char == " " and (not chars or chars[-1] == " "):
                continue
            chars.append(char)
            positions.append(i)
        return "".join(chars), positions

    needle, _ = canonical(quote)
    haystack, positions = canonical(source)
    needle = needle.strip()
    start = haystack.find(needle) if needle else -1
    if start < 0:
        return None
    return source[positions[start]:positions[start + len(needle) - 1] + 1]


def normalized(vector):
    if not vector or any(not math.isfinite(x) for x in vector):
        raise ValueError("Embedding must contain finite numbers.")
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise ValueError("Embedding must be nonzero.")
    return [x / norm for x in vector]


class Pipeline:
    def __init__(self, db_path, model, mode="local"):
        if mode not in {"local", "sharepoint", "rbac"}:
            raise ValueError("Unknown index mode.")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.model, self.mode = model, mode
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                page INTEGER NOT NULL, text TEXT NOT NULL, embedding TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS keywords USING fts5(text);
            CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY, created_at TEXT, user_id TEXT, question TEXT, result TEXT, latency_ms INTEGER);
            CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY, query_id TEXT REFERENCES audit(id), rating INTEGER CHECK(rating IN (-1,1)), comment TEXT);
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','employee')));
        """)
        for key, value in {"mode": mode, "embedding_model": model.embedding_model}.items():
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row and row[0] != value:
                self.db.close()
                raise ValueError(f"Index {key} mismatch. Use a separate database or rebuild the index.")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, value))
        self.db.commit()

    def close(self):
        self.db.close()

    def user(self, user_id):
        row = self.db.execute("SELECT id,username,role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise PermissionError("Please sign in.")
        return dict(row)

    @staticmethod
    def allowed(user, metadata):
        return metadata.get("access") == "all" or (user["role"] == "admin" and metadata.get("access") == "admin")

    def require_admin(self, user_id):
        if self.user(user_id)["role"] != "admin":
            raise PermissionError("Only admins can manage documents.")

    def documents(self, user_id):
        user = self.user(user_id)
        return [{"id": r["id"], **json.loads(r["metadata"])} for r in
                self.db.execute("SELECT id,metadata FROM documents")
                if self.allowed(user, json.loads(r["metadata"]))]

    def ingest(self, document_id, content, metadata, user_id=None):
        if self.mode == "rbac":
            self.require_admin(user_id)
            if metadata.get("access") not in {"all", "admin"}:
                raise ValueError("Choose all or admin access.")
        if metadata.get("kind") != self.mode:
            raise ValueError("Document kind must match index mode.")
        if self.mode == "sharepoint" and not all(metadata.get(k) for k in ("drive_id", "item_id", "etag")):
            raise ValueError("SharePoint IDs and version are required.")
        fingerprint = hashlib.sha256(content + json.dumps(metadata, sort_keys=True).encode()).hexdigest()
        old = self.db.execute("SELECT fingerprint FROM documents WHERE id=?", (document_id,)).fetchone()
        if old and old[0] == fingerprint:
            return {"document_id": document_id, "status": "unchanged"}
        chunks = chunk_pages(pages(content, metadata["title"]))
        vectors = []
        for start in range(0, len(chunks), 32):
            vectors.extend(self.model.embed([text for _, text in chunks[start:start + 32]]))
        if len(vectors) != len(chunks):
            raise ValueError("Embedding count mismatch.")
        vectors = [normalized(v) for v in vectors]
        dimensions = {len(v) for v in vectors}
        existing = self.db.execute("SELECT value FROM settings WHERE key='dimensions'").fetchone()
        if len(dimensions) != 1 or (existing and int(existing[0]) not in dimensions):
            raise ValueError("Embedding dimension mismatch; rebuild the index.")
        metadata = dict(metadata, indexed_at=datetime.now(timezone.utc).isoformat())
        # Replace text, vectors and sparse index atomically. Failed embedding preserves old version.
        with self.db:
            if self.mode == "rbac":
                self.require_admin(user_id)
            self._delete(document_id)
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('dimensions',?)", (str(len(vectors[0])),))
            self.db.execute("INSERT INTO documents VALUES (?,?,?)", (document_id, fingerprint, json.dumps(metadata)))
            for (page, text), vector in zip(chunks, vectors):
                row = self.db.execute("INSERT INTO chunks(document_id,page,text,embedding) VALUES (?,?,?,?)", (document_id, page, text, json.dumps(vector)))
                self.db.execute("INSERT INTO keywords(rowid,text) VALUES (?,?)", (row.lastrowid, text))
        return {"document_id": document_id, "status": "indexed", "chunks": len(chunks)}

    def _delete(self, document_id):
        self.db.execute("DELETE FROM keywords WHERE rowid IN (SELECT id FROM chunks WHERE document_id=?)", (document_id,))
        self.db.execute("DELETE FROM documents WHERE id=?", (document_id,))

    def delete(self, document_id, user_id=None):
        if self.mode == "rbac":
            self.require_admin(user_id)
        with self.db:
            self._delete(document_id)

    def retrieve(self, question, permission=None, top_k=4, candidates=30, threshold=0.35, user_id=None):
        return self._search(question, permission, top_k, candidates, threshold, user_id)[0]

    def _search(self, question, permission=None, top_k=4, candidates=30, threshold=0.35, user_id=None):
        user = self.user(user_id) if self.mode == "rbac" else None
        if self.mode == "sharepoint" and permission is None:
            raise ValueError("A live SharePoint permission checker is required.")
        rows = self.db.execute("SELECT c.*, d.metadata FROM chunks c JOIN documents d ON c.document_id=d.id").fetchall()
        if not rows:
            return [], False
        q = normalized(self.model.embed([question], query=True)[0])
        dense = {}
        for row in rows:
            vector = json.loads(row["embedding"])
            if len(q) != len(vector):
                raise ValueError("Query embedding dimension mismatch.")
            dense[row["id"]] = sum(a * b for a, b in zip(q, vector))
        query_terms = set(terms(question))
        sparse = []
        if query_terms:
            expression = " OR ".join('"' + t + '"' for t in sorted(query_terms))
            sparse = [r[0] for r in self.db.execute("SELECT rowid FROM keywords WHERE keywords MATCH ? ORDER BY bm25(keywords) LIMIT ?", (expression, candidates))]
        semantic = sorted(dense, key=dense.get, reverse=True)[:candidates]
        fusion = {}
        for ranking in (semantic, sparse):
            for rank, chunk_id in enumerate(ranking, 1):
                fusion[chunk_id] = fusion.get(chunk_id, 0) + 1 / (60 + rank)
        by_id = {row["id"]: row for row in rows}
        access, permitted, blocked_score = {}, [], 0.0
        for chunk_id in sorted(fusion, key=fusion.get, reverse=True):
            row = dict(by_id[chunk_id])
            metadata = json.loads(row["metadata"])
            doc_id = row["document_id"]
            if doc_id not in access:
                access[doc_id] = (self.allowed(user, metadata) if user else
                                  self.mode == "local" or permission(metadata))
            # Lightweight second-stage reranker, deliberately not a cross-encoder.
            coverage = len(query_terms & set(terms(row["text"]))) / max(1, len(query_terms))
            score = 0.7 * max(0, dense[chunk_id]) + 0.3 * coverage
            if not access[doc_id]:
                if self.mode == "rbac":
                    blocked_score = max(blocked_score, score)
                continue
            if score >= threshold:
                permitted.append({"chunk_id": chunk_id, "document_id": doc_id,
                    "page": row["page"], "text": row["text"], "metadata": metadata,
                    "score": score, "fusion_score": fusion[chunk_id]})
        # A weak restricted match must not override a stronger permitted source.
        best_permitted = max((hit["score"] for hit in permitted), default=0.0)
        blocked = blocked_score >= threshold and blocked_score > best_permitted + 0.1
        return sorted(permitted, key=lambda r: (r["score"], r["fusion_score"]), reverse=True)[:top_k], blocked

    def ask(self, question, graph=None, threshold=0.35, user_id=None):
        if not question.strip() or len(question) > 4000:
            raise ValueError("Question must be between 1 and 4000 characters.")
        started = time.monotonic()
        if self.mode == "sharepoint" and graph is None:
            raise ValueError("SharePoint queries require a delegated Graph token.")
        user_id = (self.user(user_id)["id"] if self.mode == "rbac" else
                   graph.identity() if self.mode == "sharepoint" else "local-demo")
        hits, blocked = self._search(question, graph.can_read if graph else None, threshold=threshold, user_id=user_id)
        result = {"query_id": str(uuid.uuid4()), "answer": ABSTAIN, "abstained": True,
                  "citations": [], "reason": "insufficient_evidence"}
        if hits:
            contexts = [{"source": n, "text": h["text"]} for n, h in enumerate(hits, 1)]
            try:
                generated = self.model.answer(question, contexts)
                claims = generated.get("claims", [])
                if not isinstance(claims, list):
                    claims = []
                valid = bool(claims)
                for claim in claims:
                    if not isinstance(claim, dict):
                        valid = False
                        break
                    n, quote, text = claim.get("source"), claim.get("quote"), claim.get("text")
                    if (type(n) is not int or not 1 <= n <= len(hits) or
                        not isinstance(quote, str) or not quote.strip() or
                        not isinstance(text, str) or not text.strip() or
                        source_quote(quote, hits[n - 1]["text"]) is None):
                        valid = False
                        break
                    claim["quote"] = source_quote(quote, hits[n - 1]["text"])
                if valid:
                    # Check again after generation so mid-request revocations fail closed.
                    used = sorted({c["source"] for c in claims})
                    current_access = True
                    if self.mode == "rbac":
                        for n in used:
                            hit = hits[n - 1]
                            current = self.db.execute("SELECT metadata FROM documents WHERE id=?", (hit["document_id"],)).fetchone()
                            current_access &= bool(current) and self.allowed(self.user(user_id), json.loads(current[0])) and json.loads(current[0]) == hit["metadata"]
                    if not current_access or (graph and not all(graph.can_read(hits[n - 1]["metadata"]) for n in used)):
                        result["reason"] = "access_or_version_changed"
                    else:
                        result.update(answer="\n".join(f'{c["text"]} [{c["source"]}]' for c in claims),
                                      abstained=False, reason="answered")
                        result["citations"] = [{"source": n, "document_id": hits[n - 1]["document_id"],
                            "chunk_id": hits[n - 1]["chunk_id"], "title": hits[n - 1]["metadata"]["title"],
                            "url": hits[n - 1]["metadata"]["source"], "page": hits[n - 1]["page"],
                            "quotes": [c["quote"] for c in claims if c["source"] == n]} for n in used]
                else:
                    result["reason"] = "unsupported_generation"
            except (ValueError, TypeError, AttributeError):
                result["reason"] = "invalid_generation"
        if self.mode == "rbac" and result["abstained"] and (blocked or result["reason"] == "access_or_version_changed"):
            result.update(answer=ACCESS_DENIED, reason="access_denied")
        latency = round((time.monotonic() - started) * 1000)
        # Keep audit server-side: it contains sensitive questions and permitted answers.
        audit = dict(result, retrieved_chunks=[h["chunk_id"] for h in hits])
        with self.db:
            self.db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)", (result["query_id"],
                datetime.now(timezone.utc).isoformat(), user_id, question, json.dumps(audit), latency))
        return result

    def feedback(self, query_id, rating, comment="", user_id=None):
        if type(rating) is not int or rating not in {-1, 1} or not isinstance(comment, str) or len(comment) > 2000:
            raise ValueError("Choose a rating of 1 or -1 and a comment up to 2000 characters.")
        if not isinstance(query_id, str):
            raise ValueError("A query ID is required.")
        row = self.db.execute("SELECT user_id FROM audit WHERE id=?", (query_id,)).fetchone()
        if self.mode == "rbac":
            user = self.user(user_id)
            if not row or row[0] != user['id']:
                raise PermissionError("You can only rate your own answers.")
        elif not row:
            raise ValueError("Query not found.")
        with self.db:
            existing = self.db.execute("SELECT id FROM feedback WHERE query_id=? ORDER BY id DESC LIMIT 1", (query_id,)).fetchone()
            if existing:
                self.db.execute("UPDATE feedback SET rating=?,comment=? WHERE id=?", (rating,comment,existing[0]))
                return existing[0]
            return self.db.execute("INSERT INTO feedback(query_id,rating,comment) VALUES (?,?,?)", (query_id, rating, comment)).lastrowid

    def stats(self):
        row = self.db.execute("SELECT count(*), avg(latency_ms) FROM audit").fetchone()
        abstentions = sum(json.loads(r[0])["abstained"] for r in self.db.execute("SELECT result FROM audit"))
        return {"queries": row[0], "mean_latency_ms": row[1], "abstentions": abstentions,
                "feedback_count": self.db.execute("SELECT count(*) FROM feedback").fetchone()[0]}
