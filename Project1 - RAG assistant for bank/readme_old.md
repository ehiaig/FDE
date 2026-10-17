# Bank RAG assistant

## Start the UI with two users

From this project directory:

```sh
source .venv/bin/activate
pip install -r requirements.txt
# Ollama must be running with nomic-embed-text and your chosen chat model installed.
export CHAT_MODEL=qwen3:30b
python -m app.web
```

Open **http://127.0.0.1:8000**. If you use your `fde_rag` virtual environment
instead, activate it and install `requirements.txt` there first.

The first startup seeds two users in `data/rbac.db`:

| Username | Demo password | Role |
| --- | --- | --- |
| `admin` | `admin-demo-123` | Upload PDFs, change labels, read both access levels |
| `employee` | `employee-demo-123` | Read and query All access documents only |

Passwords are stored as salted scrypt hashes. These are intentionally known local
demo credentials. Override `RAG_ADMIN_PASSWORD` and `RAG_EMPLOYEE_PASSWORD` **before
the first startup** to seed different passwords; startup never resets existing users.

### Try the exact RBAC scenario

1. Sign in as `admin`. Upload `examples/rbac-demo/admin-vault.pdf` with **Admin only**.
2. Upload `examples/rbac-demo/all-access-leave.pdf` with **All access**.
3. Ask **What is the vault access code?** The admin should receive the synthetic
   answer `ORCHID-742` with a citation. Expand it to read the quote or open the PDF.
4. Sign out, then sign in as `employee`. The restricted PDF is absent from the list.
5. Ask the same vault question. The answer should be: **You do not have access to
   the requested data. Please contact an admin.** There are no restricted citations.
6. Ask **What is the annual leave allowance?** Both users can receive `25 days`
   with a citation to the All access PDF.

The two PDFs are synthetic test fixtures, not real banking policies. You may upload
your own text-based PDF instead (maximum request size 20 MB). As admin, change a
document's access label in the document list; it applies on the next request
without re-embedding. All access means both **signed-in** roles, not anonymous access.
Uploads and classification are admin-only. Each upload creates a distinct document;
uploading the same filename again does not replace an earlier upload.

### How RBAC is enforced

- Browser sign-in uses a signed, HttpOnly, SameSite session cookie and CSRF tokens.
  Roles are loaded from the `users` table on each request. Client-supplied user IDs
  and roles are ignored. Signing out clears the displayed conversation.
- Documents store an `access` label (`admin` or `all`). The pipeline requires a valid
  database user for RBAC queries and an admin for ingestion/deletion.
- Candidate matching happens inside the trusted server. Restricted text is removed
  before model context is built; citations use permitted sources only. Read access
  and the indexed document version are checked again after generation.
- If no supported permitted answer is available and retrieval found relevant blocked
  candidates, the server returns the contact-admin message. Similarity is heuristic,
  so this is not proof a restricted document answers the question. A completely
  unrelated question normally gets the standard “I don't know” response. Mixed
  queries may return only the portion supported by permitted documents.
- Lists and direct PDF URLs also enforce the current user's role. PDF bytes are
  stored in SQLite, never in a publicly served uploads directory. Query logs record
  the signed-in user's ID, and no audit browsing endpoint is exposed.

The UI uses a separate **RBAC-mode** index at `data/rbac.db`. Existing CLI/SharePoint
indexes are unchanged and cannot be opened as RBAC indexes. Conversely the legacy
unauthenticated CLI refuses to open the RBAC index. Re-upload existing documents
through the UI to label them explicitly. Users, documents and labels survive a
server restart; login sessions expire on restart unless `RAG_SECRET_KEY` is set.
Use `RAG_DB` for a different UI database and `PORT` for a different port.

This is a localhost demo, not production authentication: it has no account recovery,
login rate limiting or centralized identity provider. The server binds to loopback.
Anyone with filesystem access to the database remains outside this app's RBAC
boundary. There is no cross-user answer cache or stored browser chat history.

Run the full suite with `python -m unittest discover -s tests -v`. The RBAC tests
exercise real PDF extraction through Flask's HTTP test client and use deterministic
models to assert the restricted text never reaches generation. They cover spoofed
roles, direct PDF URLs, relabeling, role revocation, CSRF, login/logout and audit IDs.
The original CLI and SharePoint regression tests remain included.

Implementation: `app/web.py` (HTTP/auth), `app/pipeline.py` (retrieval/RBAC),
`app/templates/index.html` and `app/static/` (UI), `tests/test_rbac.py` (tests).

---

## Original learning plan and CLI guide

Requirements:
Week 1:
- Upload PDFs
- Parse the pdfs
- Chunk the textx
- Generate embeddings
- store embeddings
- ask questions
- retrieve relevant chunks
- generate an anser
- return answers(chunks) with citations

week 2:
- Auth
- RBAC
- Sharepoint Integration
- Enterprise concerns

Week 3
- Tool calling
- AI evaluations
- Feedback loops

Week 4
- Cost optimization
- Latency
- observability
- production readiness

---

## Implemented MVP

A small Python CLI follows [architecture.md](architecture.md) and the
[design article](https://medium.com/@ehiagheaigg/ai-system-design-build-a-rag-assistant-for-a-bank-fde-interview-prep-71898bd46547).
The CLI remains available alongside the Flask RBAC UI above. There is no agent framework, Redis or orchestration service.
The original phased requirements above are preserved. Local mode implements the
first-week workflow; optional SharePoint mode implements the live permission check
in the revised architecture without maintaining a second permission database.

### Run locally

Requires Python 3.11+ with SQLite FTS5 and [Ollama](https://ollama.com).
Run these commands from `Project1 - RAG assistant for bank`:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Start Ollama in a separate terminal if its desktop app is not already running:
ollama serve
```

In a terminal with the virtual environment activated:

```sh
ollama pull nomic-embed-text
ollama pull llama3.2:3b
python -m app ingest examples/leave-policy.md examples/incident-policy.md
python -m app ask "How many days of annual leave do full-time employees receive?"
python -m app evaluate examples/old_evaluation.json
python -m app stats
python -m unittest discover -s tests -v
```

Use `python -m app ingest /path/to/policy.pdf` for your own text-based PDF.
TXT and Markdown are also supported. Scans need OCR outside this MVP. Local mode
has **no authentication**: anyone with access to the database can read every
indexed local document. Use synthetic or explicitly approved local documents.

Answers are JSON containing the answer, numbered citations, source location,
page number, exact supporting quotes, abstention reason and query ID. Record
feedback with `python -m app feedback QUERY_ID 1 --comment "Useful"` (or `-1`).
Re-ingesting the same document updates its chunks atomically; unchanged files
skip embedding. Remove a source with `python -m app delete DOCUMENT_ID` using
the ID returned by ingestion. Deleted source files are not detected automatically.

Configuration is via shell environment variables, not an automatically loaded `.env`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OLLAMA_URL` | `http://localhost:11434` | Embedding and generation server |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Dense embedding model |
| `CHAT_MODEL` | `llama3.2:3b` | Answer generation model |
| `GRAPH_TOKEN` | unset | Delegated Graph token for SharePoint mode |

For example, `CHAT_MODEL=qwen3:30b python -m app ask "Who approves leave?"`
uses an already installed alternative model. Changing the embedding model requires
a fresh database and re-ingestion; changing only the chat model does not.
Global flags (`--db`, `--mode`) go before the command.

### How the pipeline maps to the architecture

| Stage | Implementation |
| --- | --- |
| Ingestion and cleaning | PDF page extraction or UTF-8 text, whitespace normalization |
| Chunking | 250 words, 40-word overlap, bounded by PDF pages |
| Metadata | Stable document ID, source, title, page, modification and ingestion timestamps |
| Embeddings and storage | Ollama embeddings, normalized vectors stored in SQLite |
| Keyword index | SQLite FTS5 with BM25 ranking |
| Hybrid candidates | Union of top 30 dense and top 30 keyword results, reciprocal rank fusion |
| Permissions | Local single-user mode, or current SharePoint delegated access per candidate document |
| Reranking | Simple 70% cosine similarity + 30% query-term coverage, best four chunks |
| Guardrail | Default rerank threshold 0.35; insufficient evidence returns an abstention |
| Generation | Ollama structured claims with source numbers and verbatim evidence |
| Citation checks | Reject missing/invalid source IDs or quotes absent from the cited chunk |
| Audit and feedback | SQLite query, permitted retrieval IDs, answer, latency and feedback records |
| Evaluation | JSON cases with expected answer terms, source titles and abstention checks |

The reranker is deliberately a cheap heuristic, not a cross-encoder. Its score is
not a calibrated probability. Tune `ask --threshold` against representative questions.
Exact quote checks validate attribution syntax and evidence presence, **not** that
every generated claim logically follows from its quote. Human-reviewed answer
quality evaluation is still needed. Documents can contain prompt injections;
the system prompt treats them as data but is not a complete injection defense.

### Optional SharePoint flow

Use a separate database so local documents cannot accidentally enter a
permission-controlled query. Obtain a short-lived **delegated Microsoft Graph
access token** through your organization's approved Entra sign-in flow and set
`GRAPH_TOKEN` in the environment. Do not put the token in a command-line argument,
source file or committed config. OAuth sign-in/refresh and an HTTP authentication
layer are deliberately not implemented in this CLI.

The registered application needs consented delegated permissions sufficient to
read the intended files, plus `User.Read` for `/me`. See Microsoft's
[driveItem access documentation](https://learn.microsoft.com/en-us/graph/api/driveitem-get?view=graph-rest-1.0).
At query time the token must represent the employee, never a crawler/service
identity. `/me` rejects app-only tokens before retrieval runs.

```sh
python -m app --mode sharepoint --db data/sharepoint.db ingest-sharepoint DRIVE_ID ITEM_ID
python -m app --mode sharepoint --db data/sharepoint.db ask "Who approves leave?"
```

Ingestion downloads one explicitly selected drive item, stores its ID and eTag,
and verifies that the version did not change during download. It never persists
the preauthenticated download URL. At query time, Graph is asked for each candidate
item using the employee's delegated token. A readable-content download URL and
matching eTag are required before the chunk reaches the reranker or LLM. Denials,
missing items, network errors and version changes exclude the document. Selected
sources are checked again after generation. A version mismatch requires re-ingestion.
No permission or answer cache is used. As with any remote access check, revocation
can race with a request; this is not an atomic transaction with SharePoint.

Candidate search occurs inside a trusted local process; only allowed candidates
reach the model and returned citations. Protect the SQLite file and audit records
as sensitive server-side data. The CLI is not a multi-user security boundary, and
has no tenant isolation or user-facing audit endpoint. Live SharePoint behavior
requires verification against your tenant before deployment.

### Design choices and limits

- **Embedding model:** `nomic-embed-text` is a practical small local starting point.
  The [Ollama embedding API](https://docs.ollama.com/api/embed) keeps this adapter
  simple. Model choice must ultimately be evaluated on the bank's terminology.
- **Vector store:** SQLite keeps text, embeddings, FTS and audit data together with
  atomic updates and no extra server. Dense search is a linear Python scan, suitable
  for a small demo corpus rather than a large deployment.
- **Chunk size:** 250 words generally preserves a policy paragraph while keeping
  the context compact; overlap reduces loss at boundaries. Page boundaries are
  preserved for citations. Word counts are not tokenizer counts; tables, headings,
  very long tokens and cross-page clauses need more careful parsing later.
- **Freshness:** explicit re-ingestion replaces documents; SharePoint queries fail
  closed for changed versions. There is no scheduled crawl, delta sync or cache.
- **Scale to a million documents:** use a vector service with ANN search and a
  dedicated keyword index, batched asynchronous ingestion and SharePoint delta
  updates; enforce live access before returning candidates and before generation.
  Increase candidate recall carefully when many top results are inaccessible.
- **Next:** delegated sign-in/refresh, production authorization boundaries,
  approved-source synchronization, stronger reranking, claim-level evidence
  evaluation, representative bank questions and managed audit retention.

The four included evaluation cases and synthetic policies are smoke tests, not
banking guidance or a production acceptance set. Unit tests use deterministic model
and Graph substitutes, so security and update behavior can be tested offline.
Real model quality and live SharePoint permissions need separate integration checks.
Tessaract is optional and is not needed for this implementation.

Code entry points: `app/__main__.py` (CLI), `app/pipeline.py` (pipeline and storage),
`app/providers.py` (Ollama and Graph adapters), `tests/test_pipeline.py` (regressions).

### Ingest and test your own PDFs

Ingestion does **not** generate an evaluation dataset. An evaluation JSON file is a
manually curated answer key: questions, expected answer fragments and expected
source filenames. Keep it separate from indexed documents. If you rename it, pass
the new path to `evaluate`. The sample file was renamed to `examples/old_evaluation.json`:

```sh
python -m app evaluate examples/old_evaluation.json
```

For your own text-based PDF, activate the virtual environment and use a separate
index to keep the synthetic examples out of your answers:

```sh
source .venv/bin/activate
# Use this already installed model, or pull the default llama3.2:3b first.
export CHAT_MODEL=qwen3:30b
python -m app --db data/my-pdfs.db ingest "/absolute/path/to/your-policy.pdf"
python -m app --db data/my-pdfs.db ask "What does the policy say about annual leave?"
```

Replace the path and question with your actual document and topic. You can supply
multiple file paths to `ingest`; directories are not supported. Use the same `--db`
for both ingestion and questions. Re-run ingestion after editing a document.
Ollama must be running and both configured models must be installed.

First ask a specific question whose answer you can find in the PDF. Check the
answer, cited page and exact quote against the original. Then ask something the
PDF does not cover and verify that the assistant abstains. Extraction may lose
table structure; scanned/image-only PDFs need OCR, and mixed PDFs may omit scanned
pages. A successful ingestion does not guarantee every answer will be correct.

For a repeatable check, create a JSON file such as `examples/my-pdf-evaluation.json`:

```json
[
  {
    "question": "How many days of annual leave are provided?",
    "contains": ["25"],
    "sources": ["your-policy.pdf"]
  },
  {
    "question": "What is the policy for a topic absent from this document?",
    "abstain": true
  }
]
```

Replace these examples with facts and filenames from your actual PDF; `25` is only
an illustration. Then run:

```sh
python -m app --db data/my-pdfs.db evaluate examples/my-pdf-evaluation.json
```

The evaluator checks answer substrings, cited source filenames and whether the
assistant abstained. It prints pass/fail results and exits with a nonzero status
on failures. These checks do not replace reviewing whether the evidence actually
supports the answer. Generating expected answers from the same model would not
provide an independent correctness check.


Test Users:

| User | Password | Access |
|---|---|---|
| `admin` | `admin-demo-123` | Upload PDFs, change labels, query everything |
| `employee` | `employee-demo-123` | Query All access documents |


Run it:
`CHAT_MODEL=qwen3:30b .venv/bin/python -m app.web`