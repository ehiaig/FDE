import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app.pipeline import ABSTAIN, Pipeline, chunk_pages, pages
from app.providers import Ollama, SharePoint


class Model:
    embedding_model = "test-embeddings"

    def __init__(self):
        self.contexts = []
        self.bad = False

    def embed(self, texts, query=False):
        return [[float("leave" in t.lower()), float("secret" in t.lower()), 0.01] for t in texts]

    def answer(self, question, contexts):
        self.contexts = contexts
        return {"claims": [{"text": "Employees receive 25 days of leave.", "source": 99 if self.bad else 1,
                            "quote": contexts[0]["text"]}]}


class Graph:
    def __init__(self):
        self.allowed = True
        self.calls = 0

    def identity(self):
        return "employee-1"

    def can_read(self, metadata):
        self.calls += 1
        return self.allowed and metadata["item_id"] != "restricted"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "index.db"
        self.model = Model()
        self.rag = Pipeline(self.path, self.model)

    def tearDown(self):
        self.rag.close()
        self.temp.cleanup()

    def ingest(self, id="leave", text="Employees receive 25 days of annual leave."):
        return self.rag.ingest(id, text.encode(), {"kind": "local", "title": id + ".md", "source": "test://" + id})

    def test_pdf_list_quote_preserves_original_source(self):
        self.ingest(text="Leave equipment: A bar \x7f Rings \u25a0 Your hands!")
        with patch.object(self.model, "answer", return_value={"claims": [{
            "source": 1, "text": "You need a bar, rings and your hands.",
            "quote": "A bar Rings Your hands!"}]}):
            result = self.rag.ask("Leave equipment")
        self.assertFalse(result["abstained"])
        self.assertEqual(result["citations"][0]["quotes"], ["A bar \x7f Rings \u25a0 Your hands!"])

    def test_quote_normalization_does_not_accept_changed_facts(self):
        self.ingest()
        with patch.object(self.model, "answer", return_value={"claims": [{
            "source": 1, "text": "Employees receive 30 days.",
            "quote": "Employees receive 30 days of annual leave."}]}):
            result = self.rag.ask("leave")
        self.assertTrue(result["abstained"])
        self.assertEqual(result["citations"], [])

    def sharepoint(self):
        self.rag.close()
        self.rag = Pipeline(Path(self.temp.name) / "sp.db", self.model, "sharepoint")
        for id, text in [("allowed", "Employees receive 25 days of leave."), ("restricted", "Secret leave policy.")]:
            self.rag.ingest(id, text.encode(), {"kind": "sharepoint", "title": id + ".txt", "source": "https://example.test/" + id,
                "drive_id": "drive", "item_id": id, "etag": "v1"})

    def test_chunk_overlap_and_page_numbers(self):
        self.assertEqual(chunk_pages([(2, "a b c d e f")], 4, 1), [(2, "a b c d"), (2, "d e f")])
        with self.assertRaises(ValueError):
            chunk_pages([(1, "text")], 4, 4)

    def test_ingest_answer_audit_feedback(self):
        self.ingest()
        answer = self.rag.ask("How much annual leave?")
        self.assertFalse(answer["abstained"])
        self.assertEqual(answer["citations"][0]["document_id"], "leave")
        self.rag.feedback(answer["query_id"], 1, "Useful")
        self.assertEqual(self.rag.stats()["feedback_count"], 1)
        self.assertEqual(self.rag.stats()["queries"], 1)

    def test_updates_delete_sparse_and_dense_chunks_atomically(self):
        self.ingest(text="Old leave policy.")
        self.assertEqual(self.ingest(text="Old leave policy.")["status"], "unchanged")
        self.ingest(text="New leave policy.")
        self.assertEqual(self.rag.db.execute("SELECT count(*) FROM chunks").fetchone()[0], 1)
        self.assertEqual(self.rag.db.execute("SELECT count(*) FROM keywords WHERE keywords MATCH 'Old'").fetchone()[0], 0)
        self.rag.delete("leave")
        self.assertEqual(self.rag.retrieve("leave"), [])
        self.assertEqual(self.rag.db.execute("SELECT count(*) FROM keywords").fetchone()[0], 0)

    def test_failed_update_preserves_previous_document(self):
        self.ingest()
        with patch.object(self.model, "embed", return_value=[[0, 0]]):
            with self.assertRaises(ValueError):
                self.ingest(text="Updated leave.")
        self.assertIn("25", self.rag.retrieve("leave")[0]["text"])

    def test_low_confidence_and_empty_index_skip_generation(self):
        self.assertEqual(self.rag.ask("leave")["answer"], ABSTAIN)
        self.ingest()
        self.assertTrue(self.rag.ask("secret")["abstained"])
        self.assertEqual(self.model.contexts, [])

    def test_invalid_citations_and_fabricated_quotes_abstain(self):
        self.ingest()
        self.model.bad = True
        self.assertTrue(self.rag.ask("leave")["abstained"])
        with patch.object(self.model, "answer", return_value={"claims": [{"text": "Invented", "source": 1, "quote": "not present"}]}):
            self.assertTrue(self.rag.ask("leave")["abstained"])
        with patch.object(self.model, "answer", return_value={"claims": "invalid"}):
            self.assertTrue(self.rag.ask("leave")["abstained"])

    def test_index_model_and_mode_cannot_be_mixed(self):
        with self.assertRaises(ValueError):
            Pipeline(self.path, self.model, "sharepoint")
        self.model.embedding_model = "other"
        with self.assertRaises(ValueError):
            Pipeline(self.path, self.model)

    def test_live_acl_and_revocation_on_next_question(self):
        self.sharepoint()
        graph = Graph()
        answer = self.rag.ask("leave secret", graph)
        self.assertFalse(answer["abstained"])
        self.assertNotIn("Secret", json.dumps(self.model.contexts))
        self.assertNotIn("restricted", json.dumps(answer))
        graph.allowed = False
        self.model.contexts = []
        self.assertTrue(self.rag.ask("leave", graph)["abstained"])
        self.assertEqual(self.model.contexts, [])

    def test_revocation_during_generation(self):
        self.sharepoint()
        graph = Graph()
        original = self.model.answer
        def revoke(question, contexts):
            graph.allowed = False
            return original(question, contexts)
        with patch.object(self.model, "answer", side_effect=revoke):
            result = self.rag.ask("leave", graph)
        self.assertTrue(result["abstained"])
        self.assertEqual(result["citations"], [])

    def test_sharepoint_requires_auth(self):
        self.sharepoint()
        with self.assertRaises(ValueError):
            self.rag.ask("leave")
        with self.assertRaises(ValueError):
            self.rag.retrieve("leave")
        graph = Graph()
        with patch.object(graph, "identity", side_effect=ValueError("invalid token")):
            with self.assertRaises(ValueError):
                self.rag.ask("leave", graph)
        self.assertEqual(self.model.contexts, [])

    def test_keyword_branch_retrieves_exact_identifiers(self):
        self.ingest(text="Procedure ZX990 is used for approvals.")
        hits = self.rag.retrieve("ZX990", threshold=0)
        self.assertEqual(hits[0]["document_id"], "leave")
        self.assertGreater(hits[0]["fusion_score"], 1 / 61)

    def test_pdf_parsing(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("Install requirements.txt for the PDF integration test")
        # Minimal valid PDF with a real text content stream, no binary fixture needed.
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
                   b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
                   b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
        stream = b'BT /F1 12 Tf 72 720 Td (Annual leave is 25 days.) Tj ET'
        objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
        data, offsets = b'%PDF-1.4\n', [0]
        for n, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data += f'{n} 0 obj\n'.encode() + obj + b'\nendobj\n'
        start = len(data)
        data += b'xref\n0 6\n0000000000 65535 f \n'
        data += b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets[1:])
        data += f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF'.encode()
        self.assertIn("25 days", pages(data, "policy.pdf")[0][1])


class PermissionTests(unittest.TestCase):
    def test_download_rejects_concurrent_edit(self):
        import io
        graph = SharePoint("test-token")
        versions = [{"eTag": "v1", "@microsoft.graph.downloadUrl": "https://example.test/file"}, {"eTag": "v2"}]
        with patch.object(graph, "item", side_effect=versions), patch("app.providers.urlopen", return_value=io.BytesIO(b"document")) as download:
            with self.assertRaises(ValueError):
                graph.download("drive", "item")
            download.assert_called_once_with("https://example.test/file", timeout=120)

    def test_embedding_task_prefixes(self):
        model = Ollama()
        model.embedding_model = "nomic-embed-text"
        with patch("app.providers.request_json", return_value={"embeddings": [[1.0]]}) as request:
            model.embed(["policy"])
            self.assertEqual(request.call_args.args[1]["input"], ["search_document: policy"])
            model.embed(["question"], query=True)
            self.assertEqual(request.call_args.args[1]["input"], ["search_query: question"])

    def test_graph_fail_closed_and_stale_version(self):
        graph = SharePoint("test-token")
        meta = {"drive_id": "d", "item_id": "i", "etag": "v1"}
        for error in [HTTPError("", 403, "Forbidden", {}, None), URLError("offline"), TimeoutError()]:
            with patch.object(graph, "item", side_effect=error):
                self.assertFalse(graph.can_read(meta))
            if isinstance(error, HTTPError):
                error.close()
        with patch.object(graph, "item", return_value={"eTag": "v2", "@microsoft.graph.downloadUrl": "https://test"}):
            self.assertFalse(graph.can_read(meta))
        with patch.object(graph, "item", return_value={"eTag": "v1", "@microsoft.graph.downloadUrl": "https://test"}):
            self.assertTrue(graph.can_read(meta))
        with patch.object(graph, "item", return_value={"eTag": "v1"}):
            self.assertFalse(graph.can_read(meta))


if __name__ == "__main__":
    unittest.main()
