* High level Architecture:
```
Documents → Ingestion → Cleaning → Chunking → Metadata + ACL tagging → Embeddings + keyword index → Indexed storage → Permission-aware retrieval → Reranking → LLM → Answer with citations → Logging + feedback → Evaluation
```

Say the compliance team says employees are only allowed to access docuemnts that they already have permissions to view in SharePoint, we can't duplicate permissions manually because those change everyday. So we'd integrate sharepoint.

Now The architecture now looks like this:
```
SharePoint
↓
Document ingestion
↓
Read SharePoint metadata + permissions
↓
Generate embeddings
↓
Store embedding + SharePoint document ID
↓
User asks question
↓
Authenticate user
↓
Retrieve candidate chunks
↓
Filter by current SharePoint permissions
↓
LLM
↓
Answer with citations
```

* Questions
Why I choose the embedding model
why the vectore store
how chunking works
limitations of the mvp
how would you scale to 1m docs
why the chunk size?
What I'd build next

## Local role-based UI

The local demo now also supports database-backed `admin` and `employee` users.
It uses a separate RBAC index (`data/rbac.db`), without requiring SharePoint.

```text
Admin signs in → Upload PDF → Choose Admin only / All access
    → Extract pages → Chunk → Store access label + embeddings + keyword index

User signs in → Signed session → Load current role from users table
    → Match candidates → Exclude documents the role cannot read
    → Rerank permitted context → LLM → Validate citations → Recheck access
    → Answer + permitted citations → Audit with authenticated user ID
```

When relevant blocked candidates exist but there is no supported permitted answer,
return the contact-admin message without restricted filenames, text or citations.
Document lists and PDF downloads enforce the same access labels. Only admins can
upload or reclassify documents. See the README for demo users and browser testing.

## Feedback and evaluation loop

```text
Signed-in user rates their own answer → Persist feedback against audit query ID
→ Admin reviews question, answer and comment → Writes expected results per test user
→ Save reviewed cases → Run each question with that user's current permissions
→ Check outcome, answer text, source names, forbidden text and citation access
→ Show pass/fail → Human updates documents, retrieval or reviewed cases as needed
```

The browser and authenticated admin CLI run the same saved RBAC cases. Expectations
are human-authored; feedback does not automatically train the model. A changed test
user role invalidates its case until an admin reviews and saves it again.
