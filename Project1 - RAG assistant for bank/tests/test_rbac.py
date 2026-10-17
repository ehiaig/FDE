"""Exercise the HTTP boundary, real PDF extraction and role-aware model context."""
import io
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.pipeline import ACCESS_DENIED, Pipeline
from app.web import create_app


def pdf_bytes(text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    stream.set_data(f'BT /F1 12 Tf 50 700 Td ({escaped}) Tj ET'.encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class Model:
    embedding_model = 'rbac-test-model'

    def __init__(self):
        self.contexts = []
        self.calls = 0

    def embed(self, texts, query=False):
        return [[float('vault' in t.lower()), float('leave' in t.lower()), 0.01] for t in texts]

    def answer(self, question, contexts):
        self.contexts = contexts
        self.calls += 1
        return {'claims': [{'source': 1, 'text': contexts[0]['text'], 'quote': contexts[0]['text']}]}


class RBACTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'rbac.db')
        self.model = Model()
        self.app = create_app(self.db, self.model, testing=True)
        self.admin, self.employee = self.app.test_client(), self.app.test_client()
        self.admin_csrf = self.login(self.admin, 'admin', 'admin-demo-123')
        self.employee_csrf = self.login(self.employee, 'employee', 'employee-demo-123')
        self.secret = self.upload('vault.pdf', 'The vault access code is ORCHID-742.', 'admin')
        self.public = self.upload('leave.pdf', 'Annual leave allowance is 25 days.', 'all')

    def tearDown(self):
        self.temp.cleanup()

    def login(self, client, username, password):
        csrf = client.get('/api/session').json['csrf']
        response = client.post('/api/login', json={'username': username, 'password': password}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200)
        return response.json['csrf']

    def upload(self, name, text, access):
        response = self.admin.post('/api/documents', data={'file': (io.BytesIO(pdf_bytes(text)), name), 'access': access}, headers={'X-CSRF-Token': self.admin_csrf})
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['document_id']

    def ask(self, client, csrf, question, **extra):
        response = client.post('/api/ask', json={'question': question, **extra}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_irrelevant_restricted_match_does_not_turn_failure_into_denial(self):
        # Equal embeddings make every document a semantic candidate; permitted
        # leave has stronger lexical evidence than the unrelated vault document.
        with patch.object(self.model, 'embed', side_effect=lambda texts, query=False: [[1, 1, 0.01] for _ in texts]):
            with closing(Pipeline(self.db, self.model, 'rbac')) as pipeline:
                for row in pipeline.db.execute('SELECT id FROM chunks').fetchall():
                    pipeline.db.execute('UPDATE chunks SET embedding=? WHERE id=?',
                                        (json.dumps([0.707089, 0.707089, 0.007071]), row[0]))
                pipeline.db.commit()
            with patch.object(self.model, 'answer', return_value={'claims': []}):
                result = self.ask(self.employee, self.employee_csrf, 'Annual leave allowance')
        self.assertEqual(result['reason'], 'unsupported_generation')
        self.assertNotEqual(result['answer'], ACCESS_DENIED)

    def test_admin_receives_restricted_answer_and_citation(self):
        result = self.ask(self.admin, self.admin_csrf, 'What is the vault access code?')
        self.assertIn('ORCHID-742', result['answer'])
        self.assertEqual(result['citations'][0]['document_id'], self.secret)
        self.assertEqual(self.admin.get(f'/api/documents/{self.secret}/file').status_code, 200)

    def test_employee_denied_without_context_or_citation_leak(self):
        result = self.ask(self.employee, self.employee_csrf, 'What is the vault access code?')
        self.assertEqual(result['answer'], ACCESS_DENIED)
        self.assertEqual(result['citations'], [])
        self.assertEqual(self.model.calls, 0)
        self.assertNotIn('ORCHID-742', json.dumps(result))
        self.assertNotIn('vault.pdf', json.dumps(result))
        with closing(sqlite3.connect(self.db)) as db, db:
            audit = db.execute('SELECT user_id,result FROM audit').fetchone()
        self.assertEqual(audit[0], 'employee')
        self.assertEqual(json.loads(audit[1])['retrieved_chunks'], [])

    def test_both_roles_can_answer_public_question(self):
        for client, csrf in [(self.admin, self.admin_csrf), (self.employee, self.employee_csrf)]:
            result = self.ask(client, csrf, 'What is the annual leave allowance?')
            self.assertIn('25', result['answer'])
            self.assertEqual(result['citations'][0]['document_id'], self.public)

    def test_mixed_query_never_sends_restricted_text_to_model(self):
        self.ask(self.employee, self.employee_csrf, 'What about vault and leave?')
        self.assertNotIn('ORCHID', json.dumps(self.model.contexts))
        self.assertIn('25', json.dumps(self.model.contexts))

    def test_document_list_and_direct_file_url_are_filtered(self):
        documents = self.employee.get('/api/documents').json['documents']
        self.assertEqual([d['id'] for d in documents], [self.public])
        self.assertEqual(self.employee.get(f'/api/documents/{self.secret}/file').status_code, 404)
        self.assertEqual(self.employee.get(f'/api/documents/{self.public}/file').status_code, 200)
        self.assertEqual(self.app.test_client().get(f'/api/documents/{self.public}/file').status_code, 401)

    def test_browser_role_spoofing_does_not_grant_access(self):
        result = self.ask(self.employee, self.employee_csrf, 'vault', role='admin', user_id='admin')
        self.assertEqual(result['reason'], 'access_denied')
        response = self.employee.post('/api/documents', data={'file': (io.BytesIO(pdf_bytes('vault')), 'x.pdf'), 'access': 'all', 'role': 'admin'}, headers={'X-CSRF-Token': self.employee_csrf})
        self.assertEqual(response.status_code, 403)
        response = self.employee.patch(f'/api/documents/{self.secret}', json={'access': 'all'}, headers={'X-CSRF-Token': self.employee_csrf})
        self.assertEqual(response.status_code, 403)

    def test_relabel_takes_effect_on_next_request(self):
        response = self.admin.patch(f'/api/documents/{self.public}', json={'access': 'admin'}, headers={'X-CSRF-Token': self.admin_csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ask(self.employee, self.employee_csrf, 'leave')['reason'], 'access_denied')
        self.assertEqual(self.employee.get(f'/api/documents/{self.public}/file').status_code, 404)
        self.admin.patch(f'/api/documents/{self.public}', json={'access': 'all'}, headers={'X-CSRF-Token': self.admin_csrf})
        self.assertFalse(self.ask(self.employee, self.employee_csrf, 'leave')['abstained'])

    def test_role_is_reloaded_from_database(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE users SET role='employee' WHERE id='admin'")
        self.assertEqual(self.ask(self.admin, self.admin_csrf, 'vault')['reason'], 'access_denied')

    def test_permission_change_during_generation_blocks_response(self):
        original = self.model.answer
        def revoke(question, contexts):
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE users SET role='employee' WHERE id='admin'")
            return original(question, contexts)
        with patch.object(self.model, 'answer', side_effect=revoke):
            result = self.ask(self.admin, self.admin_csrf, 'vault')
        self.assertEqual(result['answer'], ACCESS_DENIED)
        self.assertEqual(result['citations'], [])

    def test_invalid_login_csrf_and_logout(self):
        client = self.app.test_client()
        token = client.get('/api/session').json['csrf']
        self.assertEqual(client.post('/api/login', json={'username':'admin','password':'bad'}, headers={'X-CSRF-Token':token}).status_code, 401)
        self.assertEqual(self.admin.post('/api/ask', json={'question':'vault'}).status_code, 403)
        self.assertEqual(self.employee.post('/api/logout', headers={'X-CSRF-Token':self.employee_csrf}).status_code, 200)
        self.assertEqual(self.employee.get('/api/documents').status_code, 401)

    def test_pipeline_itself_requires_identity_and_admin_for_writes(self):
        pipeline = Pipeline(self.db, self.model, 'rbac')
        try:
            with self.assertRaises(PermissionError):
                pipeline.ask('vault')
            with self.assertRaises(PermissionError):
                pipeline.retrieve('vault')
            with self.assertRaises(PermissionError):
                pipeline.delete(self.secret, user_id='employee')
            with self.assertRaises(PermissionError):
                pipeline.ingest('x', b'vault', {'kind':'rbac', 'access':'all', 'title':'x.txt'}, user_id='employee')
        finally:
            pipeline.close()

    def test_passwords_are_hashed_and_seeding_is_idempotent(self):
        create_app(self.db, self.model, testing=True)
        with closing(sqlite3.connect(self.db)) as db, db:
            rows = db.execute('SELECT username,password_hash FROM users').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(value.startswith('scrypt:') for _, value in rows))

    def test_invalid_upload_and_label_do_not_index(self):
        for content, name, access in [(b'not a PDF','bad.pdf','all'), (pdf_bytes('text'),'bad.txt','all'), (pdf_bytes('text'),'bad.pdf','public')]:
            result = self.admin.post('/api/documents', data={'file':(io.BytesIO(content),name),'access':access}, headers={'X-CSRF-Token':self.admin_csrf})
            self.assertEqual(result.status_code, 400)
        self.assertEqual(len(self.admin.get('/api/documents').json['documents']), 2)


    def feedback_for(self, client, csrf, query_id):
        return client.post('/api/feedback', json={'query_id':query_id,'rating':-1,'comment':'Please review'}, headers={'X-CSRF-Token':csrf})

    def save_case(self, feedback_id, user_id, expectation):
        return self.admin.post('/api/evaluations', json={'feedback_id':feedback_id,'user_id':user_id,'expectation':expectation}, headers={'X-CSRF-Token':self.admin_csrf})

    def test_feedback_ownership_validation_and_update(self):
        answer = self.ask(self.employee, self.employee_csrf, 'leave')
        response = self.feedback_for(self.employee,self.employee_csrf,answer['query_id'])
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.feedback_for(self.admin,self.admin_csrf,answer['query_id']).status_code,403)
        self.assertEqual(self.feedback_for(self.employee,self.employee_csrf,'missing').status_code,403)
        update=self.employee.post('/api/feedback',json={'query_id':answer['query_id'],'rating':1,'comment':'Corrected'},headers={'X-CSRF-Token':self.employee_csrf})
        self.assertEqual(update.json['id'],response.json['id'])
        bad=self.employee.post('/api/feedback',json={'query_id':answer['query_id'],'rating':True},headers={'X-CSRF-Token':self.employee_csrf})
        self.assertEqual(bad.status_code,400)
        queue=self.admin.get('/api/quality').json['feedback']
        self.assertEqual(len(queue),1)
        self.assertEqual(queue[0]['comment'],'Corrected')
        self.assertEqual(self.employee.get('/api/quality').status_code,403)

    def test_reviewed_cases_run_both_roles_and_are_idempotent(self):
        answer=self.ask(self.admin,self.admin_csrf,'vault')
        feedback_id=self.feedback_for(self.admin,self.admin_csrf,answer['query_id']).json['id']
        expected={'outcome':'answered','contains':['ORCHID-742'],'sources':['vault.pdf']}
        admin_case=self.save_case(feedback_id,'admin',expected)
        self.assertEqual(admin_case.status_code,200)
        self.assertEqual(self.save_case(feedback_id,'admin',expected).json['id'],admin_case.json['id'])
        employee_case=self.save_case(feedback_id,'employee',{'outcome':'access_denied','forbidden':['ORCHID-742','vault.pdf']})
        for response in (admin_case,employee_case):
            cid=response.json['id']
            report=self.admin.post(f'/api/evaluations/{cid}/run',json={},headers={'X-CSRF-Token':self.admin_csrf})
            self.assertEqual(report.status_code,200,report.json)
            self.assertEqual(report.json['passed'],1,report.json)
            self.assertEqual(self.employee.post(f'/api/evaluations/{cid}/run',json={},headers={'X-CSRF-Token':self.employee_csrf}).status_code,403)
        self.assertEqual(len(self.admin.get('/api/quality').json['cases']),2)
        # Saved expectations survive app recreation; originals are never auto-generated.
        create_app(self.db,self.model,testing=True)
        self.assertEqual(len(self.admin.get('/api/quality').json['cases']),2)

    def test_evaluation_failure_and_role_drift(self):
        answer=self.ask(self.admin,self.admin_csrf,'leave')
        fid=self.feedback_for(self.admin,self.admin_csrf,answer['query_id']).json['id']
        cid=self.save_case(fid,'employee',{'outcome':'answered','contains':['wrong answer'],'sources':['missing.pdf'],'forbidden':['25']}).json['id']
        report=self.admin.post(f'/api/evaluations/{cid}/run',json={},headers={'X-CSRF-Token':self.admin_csrf}).json
        self.assertEqual(report['passed'],0)
        self.assertEqual(len(report['cases'][0]['failed_checks']),3)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE users SET role='admin' WHERE id='employee'")
        report=self.admin.post(f'/api/evaluations/{cid}/run',json={},headers={'X-CSRF-Token':self.admin_csrf}).json
        self.assertIn('role changed',report['cases'][0]['failed_checks'][0])

    def test_review_requires_explicit_valid_expected_results(self):
        answer=self.ask(self.admin,self.admin_csrf,'leave')
        fid=self.feedback_for(self.admin,self.admin_csrf,answer['query_id']).json['id']
        for expected in ({'outcome':'answered'}, {'outcome':'access_denied','sources':['vault.pdf']}, {'outcome':'other'}, {'outcome':'answered','contains':'25'}):
            self.assertEqual(self.save_case(fid,'admin',expected).status_code,400)
        self.assertEqual(self.admin.post('/api/evaluations/999/run',json={},headers={'X-CSRF-Token':self.admin_csrf}).status_code,400)
        self.assertEqual(self.employee.post('/api/evaluations',json={},headers={'X-CSRF-Token':self.employee_csrf}).status_code,403)

    def test_rbac_evaluation_cli_authenticates_and_reports_failures(self):
        import os
        from contextlib import redirect_stdout, redirect_stderr
        from app.__main__ import main
        answer=self.ask(self.admin,self.admin_csrf,'leave')
        fid=self.feedback_for(self.admin,self.admin_csrf,answer['query_id']).json['id']
        self.save_case(fid,'employee',{'outcome':'answered','contains':['25'],'sources':['leave.pdf']})
        args=['app','--mode','rbac','--db',self.db,'evaluate']
        with patch('sys.argv',args), patch('app.__main__.Ollama',return_value=self.model), patch.dict(os.environ,{'RAG_LOGIN_PASSWORD':'admin-demo-123'}), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(),0)
        self.assertEqual(json.loads(output.getvalue())['passed'],1)
        with patch('sys.argv',args+['--username','employee']), patch('app.__main__.Ollama',return_value=self.model), patch.dict(os.environ,{'RAG_LOGIN_PASSWORD':'employee-demo-123'}), redirect_stderr(io.StringIO()):
            self.assertEqual(main(),1)


if __name__ == '__main__':
    unittest.main()
