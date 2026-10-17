"""Local RBAC demo: python -m app.web. Server-side roles, never client roles."""
import io
import json
import os
import secrets
import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request, send_file, session
from pypdf.errors import PdfReadError
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .pipeline import Pipeline
from .providers import Ollama
from . import quality

DEMO_USERS = (("admin", "admin-demo-123", "admin"),
              ("employee", "employee-demo-123", "employee"))


def create_app(db_path=None, model=None, testing=False):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("RAG_SECRET_KEY") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8), TESTING=testing,
    )
    path = db_path or os.getenv("RAG_DB", "data/rbac.db")
    model = model or Ollama()
    pipeline = Pipeline(path, model, "rbac")
    quality.setup(pipeline.db)
    with pipeline.db:
        pipeline.db.execute("CREATE TABLE IF NOT EXISTS pdf_files(document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE, content BLOB NOT NULL)")
        for username, password, role in DEMO_USERS:
            if not pipeline.db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                password = os.getenv(f"RAG_{role.upper()}_PASSWORD", password)
                pipeline.db.execute("INSERT INTO users VALUES (?,?,?,?)",
                    (username, username, generate_password_hash(password), role))
    pipeline.close()

    def rag():
        if "rag" not in g:
            g.rag = Pipeline(path, model, "rbac")
        return g.rag

    @app.teardown_appcontext
    def close(_error):
        if "rag" in g:
            g.rag.close()

    @app.before_request
    def authorize():
        if not request.path.startswith("/api/"):
            return
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = session.get("csrf")
            if not csrf or not secrets.compare_digest(csrf, request.headers.get("X-CSRF-Token", "")):
                return jsonify(error="Refresh the page and try again."), 403
        if request.path not in {"/api/session", "/api/login"}:
            g.user = rag().user(session.get("user_id"))

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(PermissionError)
    def permission_error(error):
        return jsonify(error=str(error)), 403 if session.get("user_id") else 401

    @app.errorhandler(ValueError)
    @app.errorhandler(PdfReadError)
    def bad_request(error):
        return jsonify(error=str(error) if isinstance(error, ValueError) else "Upload a readable, text-based PDF."), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(OSError)
    def unavailable(_error):
        return jsonify(error="The model service is unavailable. Check Ollama and the configured models, then retry."), 503

    @app.errorhandler(sqlite3.Error)
    def database_error(_error):
        return jsonify(error="Database operation failed. Please retry."), 503

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/session")
    def current_session():
        session.setdefault("csrf", secrets.token_hex(32))
        user = None
        if session.get("user_id"):
            try:
                user = rag().user(session["user_id"])
            except PermissionError:
                session.pop("user_id", None)
        return jsonify(user=user, csrf=session["csrf"])

    @app.post("/api/login")
    def login():
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get("username"), str) or not isinstance(body.get("password"), str):
            return jsonify(error="Enter a username and password."), 400
        row = rag().db.execute("SELECT id,password_hash FROM users WHERE username=?", (body["username"],)).fetchone()
        if not row or not check_password_hash(row["password_hash"], body["password"]):
            return jsonify(error="Invalid username or password."), 401
        session.clear()
        session.update(user_id=row["id"], csrf=secrets.token_hex(32))
        session.permanent = True
        return jsonify(user=rag().user(row["id"]), csrf=session["csrf"])

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/documents")
    def documents():
        return jsonify(documents=rag().documents(g.user["id"]))

    @app.post("/api/documents")
    def upload():
        rag().require_admin(g.user["id"])
        file = request.files.get("file")
        access = request.form.get("access")
        if not file or access not in {"all", "admin"}:
            raise ValueError("PDF and access label required.")
        name = secure_filename(file.filename or "")
        content = file.read()
        if Path(name).suffix.lower() != ".pdf" or not content.startswith(b"%PDF-"):
            raise ValueError("PDF required.")
        document_id = str(uuid.uuid4())
        result = rag().ingest(document_id, content, {
            "kind": "rbac", "access": access, "title": name,
            "source": f"/api/documents/{document_id}/file", "uploaded_by": g.user["id"],
        }, user_id=g.user["id"])
        with rag().db:
            rag().db.execute("INSERT INTO pdf_files VALUES (?,?)", (document_id, content))
        return jsonify(result), 201

    @app.patch("/api/documents/<document_id>")
    def change_access(document_id):
        rag().require_admin(g.user["id"])
        body = request.get_json()
        if not isinstance(body, dict) or body.get("access") not in {"all", "admin"}:
            raise ValueError("Choose an access label.")
        row = rag().db.execute("SELECT metadata FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            return jsonify(error="Document unavailable."), 404
        metadata = dict(json.loads(row[0]), access=body["access"])
        with rag().db:
            rag().db.execute("UPDATE documents SET metadata=? WHERE id=?", (json.dumps(metadata), document_id))
        return jsonify(ok=True)

    @app.get("/api/documents/<document_id>/file")
    def document_file(document_id):
        row = rag().db.execute("SELECT d.metadata,f.content FROM documents d JOIN pdf_files f ON d.id=f.document_id WHERE d.id=?", (document_id,)).fetchone()
        if not row or not rag().allowed(g.user, json.loads(row["metadata"])):
            return jsonify(error="Document unavailable. Please contact an admin."), 404
        metadata = json.loads(row["metadata"])
        return send_file(io.BytesIO(row["content"]), mimetype="application/pdf", download_name=metadata["title"])

    @app.post("/api/ask")
    def ask():
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get("question"), str):
            return jsonify(error="Enter a question."), 400
        # Ignore any user_id/role supplied by a client. Identity comes from the signed session.
        return jsonify(rag().ask(body["question"], user_id=g.user["id"]))

    @app.post("/api/feedback")
    def feedback():
        body = request.get_json()
        if not isinstance(body, dict):
            raise ValueError("Provide a query, rating and optional comment.")
        feedback_id = rag().feedback(body.get('query_id'), body.get('rating'), body.get('comment', ''), user_id=g.user['id'])
        return jsonify(id=feedback_id)

    @app.get("/api/quality")
    def review():
        rag().require_admin(g.user['id'])
        return jsonify(feedback=quality.review_queue(rag(), g.user['id']),
                       cases=quality.cases(rag(), g.user['id']),
                       users=[dict(r) for r in rag().db.execute('SELECT id,username,role FROM users ORDER BY username')])

    @app.post("/api/evaluations")
    def save_evaluation():
        rag().require_admin(g.user['id'])
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get('user_id'), str):
            raise ValueError("Select a test user and enter expected results.")
        case_id = quality.save_case(rag(), g.user['id'], body.get('feedback_id'), body['user_id'], body.get('expectation'))
        return jsonify(id=case_id)

    @app.post("/api/evaluations/<int:case_id>/run")
    def run_evaluation(case_id):
        return jsonify(quality.evaluate(rag(), g.user['id'], selected=case_id))

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.getenv("PORT", "8001")), debug=False)
