"""Run from the project directory: python -m app --help."""
import argparse
import getpass
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.error import URLError

from .pipeline import Pipeline
from .providers import Ollama, SharePoint


def main():
    parser = argparse.ArgumentParser(description="Local-first, permission-aware bank RAG")
    parser.add_argument("--db", default="data/rag.db")
    parser.add_argument("--mode", choices=["local", "sharepoint", "rbac"], default="local")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Index local PDF/TXT/Markdown files")
    ingest.add_argument("files", nargs="+", type=Path)
    sp = commands.add_parser("ingest-sharepoint", help="Download and index a Graph drive item")
    sp.add_argument("drive_id")
    sp.add_argument("item_id")
    ask = commands.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--threshold", type=float, default=0.35)
    delete = commands.add_parser("delete")
    delete.add_argument("document_id")
    feedback = commands.add_parser("feedback")
    feedback.add_argument("query_id")
    feedback.add_argument("rating", type=int, choices=[-1, 1])
    feedback.add_argument("--comment", default="")
    commands.add_parser("stats")
    evaluate = commands.add_parser("evaluate", help="Run JSON evaluation cases")
    evaluate.add_argument("dataset", type=Path, nargs="?")
    evaluate.add_argument("--username", default="admin", help="Admin login for saved RBAC evaluations")
    args = parser.parse_args()
    if args.mode == "rbac" and (args.command != "evaluate" or args.dataset):
        parser.error("RBAC CLI supports only evaluate (saved cases, no dataset argument). Use the web UI for other operations.")
    if args.command == "evaluate" and args.mode != "rbac" and not args.dataset:
        parser.error("A dataset file is required for local/SharePoint evaluation.")
    if args.command == "ingest" and args.mode != "local":
        parser.error("Local files require --mode local.")
    if args.command == "ingest-sharepoint" and args.mode != "sharepoint":
        parser.error("Use --mode sharepoint and a separate --db for SharePoint.")
    pipeline = None
    try:
        pipeline = Pipeline(args.db, Ollama(), args.mode)
        graph = SharePoint(os.getenv("GRAPH_TOKEN")) if args.mode == "sharepoint" and args.command in {"ask", "ingest-sharepoint", "evaluate"} else None
        if args.command == "ingest":
            result = []
            for path in args.files:
                path = path.resolve()
                result.append(pipeline.ingest(str(path), path.read_bytes(), {
                    "kind": "local", "title": path.name, "source": path.as_uri(),
                    "modified_at": path.stat().st_mtime,
                }))
        elif args.command == "ingest-sharepoint":
            content, metadata = graph.download(args.drive_id, args.item_id)
            result = pipeline.ingest(f"{args.drive_id}/{args.item_id}", content, metadata)
        elif args.command == "ask":
            if not 0 <= args.threshold <= 1:
                parser.error("Threshold must be between 0 and 1.")
            result = pipeline.ask(args.question, graph, args.threshold)
        elif args.command == "delete":
            pipeline.delete(args.document_id)
            result = {"deleted": args.document_id}
        elif args.command == "feedback":
            pipeline.feedback(args.query_id, args.rating, args.comment)
            result = {"saved": True}
        elif args.command == "stats":
            result = pipeline.stats()
        elif args.mode == "rbac":
            from werkzeug.security import check_password_hash
            from . import quality
            row = pipeline.db.execute("SELECT id,password_hash FROM users WHERE username=?", (args.username,)).fetchone()
            password = os.getenv("RAG_LOGIN_PASSWORD") or getpass.getpass("Admin password: ")
            if not row or not check_password_hash(row['password_hash'], password):
                raise PermissionError("Invalid credentials.")
            pipeline.require_admin(row['id'])
            quality.setup(pipeline.db)
            result = quality.evaluate(pipeline, row['id'])
        else:
            cases = json.loads(args.dataset.read_text())
            checks = []
            for case in cases:
                answer = pipeline.ask(case["question"], graph)
                titles = {c["title"] for c in answer["citations"]}
                passed = answer["abstained"] == case.get("abstain", False)
                passed &= all(s.lower() in answer["answer"].lower() for s in case.get("contains", []))
                passed &= set(case.get("sources", [])).issubset(titles)
                checks.append({"question": case["question"], "passed": passed, "result": answer})
            result = {"passed": sum(c["passed"] for c in checks), "total": len(checks), "cases": checks}
        print(json.dumps(result, indent=2))
        if args.command == "evaluate" and result["passed"] != result["total"]:
            return 1
        return 0
    except (OSError, URLError, ValueError, KeyError, ImportError, sqlite3.Error) as exc:
        # Never echo HTTP bodies or URLs that could include credentials/download tokens.
        print(f"Unable to complete {args.command} ({type(exc).__name__}). Check configuration, input files, model availability and access.", file=sys.stderr)
        return 1
    finally:
        if pipeline:
            pipeline.close()


if __name__ == "__main__":
    raise SystemExit(main())
