"""Human-reviewed feedback cases and role-aware regression evaluation."""
import json
from datetime import datetime, timezone


def setup(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS evaluation_cases(
            id INTEGER PRIMARY KEY, feedback_id INTEGER NOT NULL REFERENCES feedback(id),
            user_id TEXT NOT NULL REFERENCES users(id), expected_role TEXT NOT NULL,
            expectation TEXT NOT NULL, reviewed_by TEXT NOT NULL REFERENCES users(id),
            reviewed_at TEXT NOT NULL, UNIQUE(feedback_id,user_id));
    ''')


def review_queue(pipeline, admin_id):
    pipeline.require_admin(admin_id)
    rows = pipeline.db.execute('''SELECT f.id,f.query_id,f.rating,f.comment,a.user_id,a.question,a.result
        FROM feedback f JOIN audit a ON a.id=f.query_id ORDER BY f.id DESC''')
    return [dict(r, result=json.loads(r['result'])) for r in rows]


def validate_expectation(value):
    if not isinstance(value, dict) or value.get('outcome') not in {'answered', 'abstained', 'access_denied'}:
        raise ValueError('Choose answered, abstained or access_denied.')
    result = {'outcome': value['outcome']}
    for key in ('contains', 'sources', 'forbidden'):
        entries = value.get(key, [])
        if not isinstance(entries, list) or len(entries) > 30 or any(not isinstance(x, str) or not x.strip() or len(x) > 500 for x in entries):
            raise ValueError(f'{key} must be a list of nonempty strings (maximum 30).')
        result[key] = [x.strip() for x in entries]
    if result['outcome'] == 'answered' and not (result['contains'] and result['sources']):
        raise ValueError('Answered cases require expected answer text and at least one source filename.')
    if result['outcome'] != 'answered' and (result['contains'] or result['sources']):
        raise ValueError('Abstention/denial cases must not require answer text or sources.')
    return result


def save_case(pipeline, admin_id, feedback_id, user_id, expectation):
    pipeline.require_admin(admin_id)
    target = pipeline.user(user_id)
    expected = validate_expectation(expectation)
    if type(feedback_id) is not int or not pipeline.db.execute('SELECT 1 FROM feedback WHERE id=?', (feedback_id,)).fetchone():
        raise ValueError('Feedback not found.')
    with pipeline.db:
        pipeline.db.execute('''INSERT INTO evaluation_cases
            (feedback_id,user_id,expected_role,expectation,reviewed_by,reviewed_at) VALUES (?,?,?,?,?,?)
            ON CONFLICT(feedback_id,user_id) DO UPDATE SET expected_role=excluded.expected_role,
            expectation=excluded.expectation,reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at''',
            (feedback_id, user_id, target['role'], json.dumps(expected), admin_id, datetime.now(timezone.utc).isoformat()))
    return pipeline.db.execute('SELECT id FROM evaluation_cases WHERE feedback_id=? AND user_id=?', (feedback_id,user_id)).fetchone()[0]


def cases(pipeline, admin_id):
    pipeline.require_admin(admin_id)
    rows = pipeline.db.execute('''SELECT e.*,a.question FROM evaluation_cases e
        JOIN feedback f ON f.id=e.feedback_id JOIN audit a ON a.id=f.query_id ORDER BY e.id''')
    return [dict(r, expectation=json.loads(r['expectation'])) for r in rows]


def run_case(pipeline, admin_id, case):
    pipeline.require_admin(admin_id)
    failed = []
    target = pipeline.user(case['user_id'])
    if target['role'] != case['expected_role']:
        return {'id': case['id'], 'user_id': target['id'], 'passed': False,
                'failed_checks': ['User role changed; review this case again.']}
    expected = validate_expectation(case['expectation'])
    answer = pipeline.ask(case['question'], user_id=target['id'])
    outcome = 'access_denied' if answer['reason'] == 'access_denied' else 'abstained' if answer['abstained'] else 'answered'
    if outcome != expected['outcome']:
        failed.append(f"Expected {expected['outcome']}; received {outcome}.")
    if not all(x.casefold() in answer['answer'].casefold() for x in expected['contains']):
        failed.append('Expected answer text missing.')
    if not set(expected['sources']).issubset({c['title'] for c in answer['citations']}):
        failed.append('Expected source filename missing.')
    if any(x.casefold() in json.dumps(answer).casefold() for x in expected['forbidden']):
        failed.append('Forbidden text appeared in the response.')
    if answer['abstained'] and answer['citations']:
        failed.append('Abstention included citations.')
    if not answer['abstained'] and not answer['citations']:
        failed.append('Answer has no citations.')
    # Check current permissions independently of the model's claims.
    target = pipeline.user(target['id'])
    for citation in answer['citations']:
        row = pipeline.db.execute('SELECT metadata FROM documents WHERE id=?', (citation['document_id'],)).fetchone()
        if not row or not pipeline.allowed(target, json.loads(row[0])):
            failed.append('Citation permission violation.')
    return {'id': case['id'], 'user_id': target['id'], 'passed': not failed,
            'failed_checks': failed, 'result': answer}


def evaluate(pipeline, admin_id, selected=None):
    reviewed = cases(pipeline, admin_id)
    if selected is not None:
        reviewed = [c for c in reviewed if c['id'] == selected]
    if not reviewed:
        raise ValueError('No reviewed evaluation cases found.')
    results = []
    for case in reviewed:
        try:
            results.append(run_case(pipeline, admin_id, case))
        except (OSError, ValueError, KeyError) as error:
            results.append({'id':case['id'], 'user_id':case['user_id'], 'passed':False,
                            'failed_checks':[f'Evaluation could not complete ({type(error).__name__}).']})
    return {'passed':sum(r['passed'] for r in results), 'total':len(results), 'cases':results}
