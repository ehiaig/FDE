# Bank RAG assistant

A small Python RAG app with PDF upload, two user roles, cited answers, feedback,
and human-reviewed evaluation cases. Flask serves the UI; SQLite stores data;
Ollama provides embeddings and answer generation. All commands below run from
`Project1 - RAG assistant for bank`.

## Run

Requires Python 3.11+, SQLite with FTS5, and a running [Ollama](https://ollama.com).

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ollama pull nomic-embed-text
ollama pull llama3.2:3b
python -m app.web
```

Open **http://127.0.0.1:8001** (the default port). Start `ollama serve` separately
if the Ollama app/server is not running. To use your installed Qwen model and port 8000:

```sh
PORT=8000 CHAT_MODEL=qwen3:30b python -m app.web
```

If using the `fde_rag` environment instead, activate it and install the same
requirements there. Restart the Python server after backend code changes; refresh
the browser after JavaScript changes.

## Users and documents

The first web startup creates these accounts in `data/rbac.db`:

| Username | Demo password | Permissions |
| --- | --- | --- |
| `admin` | `admin-demo-123` | Upload PDFs, change access labels, read all documents, review feedback and run evaluations |
| `employee` | `employee-demo-123` | Read/query All access documents and rate their own answers |

Passwords are hashed. These are known **local-demo credentials**, not production
accounts. `RAG_ADMIN_PASSWORD` and `RAG_EMPLOYEE_PASSWORD` override initial passwords
only before account creation; changing them later does not reset existing passwords.

Admins upload text-based PDFs and select **Admin only** or **All access**. All access
means both signed-in roles. The maximum HTTP upload request is 20 MB. Every upload
creates a new document, even if the filename matches an existing one. Changing an
access label takes effect without re-embedding. Scanned PDFs need OCR beforehand.

To test RBAC, upload the synthetic fixtures:

| PDF | Label | Question | Expected behavior |
| --- | --- | --- | --- |
| `examples/rbac-demo/admin-vault.pdf` | Admin only | What is the vault access code? | Admin: `ORCHID-742` with citation. Employee: contact-admin message, no citation. |
| `examples/rbac-demo/all-access-leave.pdf` | All access | What is the annual leave allowance? | Both users: `25 days` with citation. |

Restricted PDFs are also hidden from employee document lists and blocked at direct
PDF URLs. Questions use the signed-in user's database role, never a browser-supplied
role. **Enter** submits; **Shift+Enter** adds a line. Citations expand to show evidence
and a link to the PDF page. Signing out clears the displayed conversation.

## Feedback → review → evaluation

1. After an answer (including an abstention), choose **Helpful** or **Not helpful**,
   optionally add a comment, then **Save feedback**. A user can rate only their own
   answer. Saving again updates that response's feedback.
2. As admin, open **Feedback & evaluation → Load feedback**. Expand an entry to see
   the question, original answer, citations and comment.
3. Select the user to test and write an expected outcome:
   - `answered`: require answer fragments and source filenames, one per line.
   - `access_denied`: expect the contact-admin outcome and no citations.
   - `abstained`: expect insufficient/unsupported evidence, not an access denial.
   Optional **Forbidden text** checks for strings that must not appear anywhere in
   the response, including citations.
4. **Save reviewed case**. Save once for admin and once for employee to test the same
   question under both accounts. Saving again updates that feedback/user pair.
5. **Run saved cases** to see per-case results. Tests use current documents and roles;
   a changed user role requires reviewing/saving the case again.

Expected results are written by a human; ratings do not train the model or modify
retrieval automatically. Feedback and reviewed cases persist in SQLite. Browser
reports are temporary; evaluation calls also create normal audit records.

Run the saved RBAC cases from the CLI (prompts for the admin password):

```sh
python -m app --mode rbac --db data/rbac.db evaluate --username admin
```

For automation, supply `RAG_LOGIN_PASSWORD` through the environment. The command
requires an admin login, prints a JSON report and exits nonzero on failed cases.
Use shell redirection to retain a report. The CLI accepts no external dataset file
in RBAC mode; it runs the reviewed cases saved through the UI.

Evaluation checks outcomes, answer substrings, required source filenames, forbidden
text, citation presence and current citation permissions. These checks **do not prove
semantic correctness**. Independently review the source evidence. Regression tests
also verify that unauthorized text is excluded from model context.

## Pipeline
The architecture being implemented:

```text
Documents → extraction → whitespace cleaning → overlapping chunks
→ metadata + document access label → embeddings + keyword index → SQLite
→ candidate retrieval → permission filtering → heuristic reranking
→ LLM → validated citations → audit + feedback → reviewed evaluation
```

- PDF pages use `pypdf`; CLI inputs also support TXT/Markdown.
- Chunks contain up to 250 words with 40-word overlap, bounded by page.
- Dense cosine search and FTS5/BM25 retrieve candidates; reciprocal rank fusion combines rankings.
- Reranking uses 70% cosine similarity + 30% query-term coverage. Up to four chunks
  above a default score of 0.35 enter the model context. This is not a cross-encoder
  or a calibrated confidence score.
- Citations require a valid source ID and a matching quote in its chunk.
  Matching tolerates PDF bullet/control characters and whitespace; citations retain
  the original source text. It does not permit changed words or numbers. Access and
  document metadata are checked again after generation.
- Completed queries log user ID, question, permitted retrieved chunk IDs, result and
  latency. Exceptions that exit early do not have comprehensive audit coverage.

## Modes and CLI

Modes describe document/access policy, not the interface or deployment location.
Each database is tied to a mode and embedding model; mixing them is rejected.

| Mode | Access policy | Interface |
| --- | --- | --- |
| `local` | No authentication or document permission filtering | CLI |
| `rbac` | Database users and admin/all labels | Web UI; authenticated CLI evaluation only |
| `sharepoint` | Current access through the user's delegated Graph token | CLI |

The web database is separate from CLI indexes. Re-upload CLI documents through the
UI to assign access labels. Changing the embedding model requires a new index and
re-ingestion. Changing only the chat model does not.

Local CLI example (unrestricted, synthetic documents):

```sh
python -m app ingest examples/leave-policy.md examples/incident-policy.md
python -m app ask "Who approves annual leave?"
python -m app evaluate examples/old_evaluation.json
python -m app feedback QUERY_ID 1 --comment "Useful"
python -m app stats
python -m app delete DOCUMENT_ID
```

Global flags go before the command. Use `--db data/my-pdfs.db` consistently to work
with another local index. Re-ingesting the same local path replaces its chunks;
unchanged content/metadata skips embeddings. Deleted source files are not detected
automatically. `examples/old_evaluation.json` is a manually written sample test set;
ingestion never generates it.

Optional SharePoint ingestion requires `GRAPH_TOKEN` containing a delegated token
with appropriate file access and `User.Read` for `/me`:

```sh
python -m app --mode sharepoint --db data/sharepoint.db ingest-sharepoint DRIVE_ID ITEM_ID
python -m app --mode sharepoint --db data/sharepoint.db ask "Who approves leave?"
```

This adapter downloads individual items and checks live readability/eTags. Changed
versions fail closed until re-ingested. There is no automatic sync, OAuth login or
token refresh. Live tenant verification is still needed; tests mock Graph responses.

## Configuration

Shell environment variables are read directly; `.env` is not loaded automatically.

| Variable | Default / purpose |
| --- | --- |
| `OLLAMA_URL` | `http://localhost:11434` |
| `EMBEDDING_MODEL` | `nomic-embed-text` |
| `CHAT_MODEL` | `llama3.2:3b` |
| `PORT` | `8001` for the web server |
| `RAG_DB` | `data/rbac.db` for the web server |
| `RAG_SECRET_KEY` | Random per server startup; set a stable secret to retain sessions across restarts |
| `RAG_ADMIN_PASSWORD`, `RAG_EMPLOYEE_PASSWORD` | First-time account seeding only |
| `RAG_LOGIN_PASSWORD` | Admin password for CLI RBAC evaluation; otherwise prompted |
| `GRAPH_TOKEN` | Delegated Graph token for SharePoint commands |

## Verification and remaining work

```sh
python -m unittest discover -s tests -v
```

Tests exercise PDF parsing, indexing, retrieval, citations, authentication, RBAC,
feedback ownership, case review, role-aware evaluation and CLI authentication using
deterministic model substitutes. Live answers depend on the selected model and data.

Learning-plan status: core RAG, demo auth/RBAC, basic SharePoint, feedback review and
basic evaluations are implemented. LLM tool calling, SSO/account management,
advanced answer-quality metrics, automated sync, dashboards/tracing, cost accounting
and production deployment remain out of scope.

Known limits: whitespace-only cleaning; no OCR or table reconstruction; linear vector
scans; candidate selection before permission filtering can miss lower-ranked allowed
documents; access-denial relevance is heuristic: a restricted match must score at least 0.1
  above the best permitted match before an abstention becomes an access denial. Prompt instructions and quote checks
are not a complete defense against unsupported claims or prompt injection.

This is a loopback-only demo with known seed credentials, no login rate limiting or
account recovery, and Flask's development server. Database/filesystem access bypasses
the app boundary. Original PDFs, audit records and feedback are stored in SQLite;
protect that file. There is no automatic feedback-to-training or answer cache.

Files: `app/pipeline.py` (RAG/storage), `app/providers.py` (Ollama/Graph),
`app/web.py` (HTTP/auth), `app/quality.py` (review/evaluation), `app/__main__.py`
(CLI), `app/templates/` and `app/static/` (UI), `tests/` (regressions).
