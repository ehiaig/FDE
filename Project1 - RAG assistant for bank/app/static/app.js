const el = id => document.getElementById(id);
let csrf = '', user = null, epoch = 0;
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'X-CSRF-Token': csrf, ...options.headers}});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login') await restore();
    throw new Error(data.error || 'Request failed. Please try again.');
  }
  return data;
}
function jsonPost(path, body) { return api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); }
function showSession(data) {
  epoch++; user = data.user; csrf = data.csrf || '';
  el('login-panel').hidden = !!user; el('workspace').hidden = !user; el('identity').hidden = !user;
  el('upload-panel').hidden = user?.role !== 'admin';
  el('quality-panel').hidden = user?.role !== 'admin';
  el('review-list').replaceChildren(); el('evaluation-cases').replaceChildren();
  el('quality-status').textContent = ''; reviewedCases = [];
  el('user-label').textContent = user ? `${user.username} · ${user.role}` : '';
  el('conversation').replaceChildren(); el('documents').replaceChildren();
  el('question').value = ''; el('password').value = '';
  for (const id of ['login-status','ask-status','upload-status','document-status']) el(id).textContent = '';
  if (user) { message('Ask a question about the documents available to your role.', 'assistant'); loadDocuments(); }
}
async function restore() { showSession(await api('/api/session')); }
function message(text, kind) {
  el('empty-chat')?.remove();
  const box = document.createElement('div'); box.className = `message ${kind}`; box.textContent = text;
  el('conversation').append(box); box.scrollIntoView({block:'nearest'}); return box;
}
async function loadDocuments() {
  const generation = epoch;
  try {
    const data = await api('/api/documents'); if (generation !== epoch) return;
    el('documents').replaceChildren();
    for (const doc of data.documents) {
      const li = document.createElement('li'), link = document.createElement('a');
      link.textContent = doc.title; link.href = doc.source; link.target = '_blank'; link.rel = 'noopener'; li.append(link);
      const badge = document.createElement('span'); badge.className = `badge ${doc.access}`;
      badge.textContent = doc.access === 'admin' ? 'Admin only' : 'All access'; li.append(badge);
      if (user.role === 'admin') {
        const select = document.createElement('select'); select.setAttribute('aria-label', `Access for ${doc.title}`);
        for (const [value, text] of [['admin','Admin only'],['all','All access']]) { const option = new Option(text,value); select.add(option); }
        select.value = doc.access; li.append(document.createElement('br'), select);
        select.onchange = async () => { select.disabled = true; try {
          await api(`/api/documents/${doc.id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({access:select.value})});
          await loadDocuments();
        } catch (error) { el('document-status').textContent = error.message; select.value = doc.access; select.disabled = false; } };
      }
      el('documents').append(li);
    }
    if (!data.documents.length) { const li = document.createElement('li'); li.className='no-docs'; li.textContent = user.role === 'admin' ? 'Upload your first PDF to get started.' : 'No documents are available to you yet. Please contact an admin.'; el('documents').append(li); }
  } catch (error) { if (generation === epoch) el('document-status').textContent = error.message; }
}
el('login-form').onsubmit = async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try { showSession(await jsonPost('/api/login',{username:el('username').value,password:el('password').value})); }
  catch(error) { el('login-status').textContent = error.message; } finally { button.disabled = false; }
};
el('logout').onclick = async () => { try { await jsonPost('/api/logout',{}); await restore(); } catch(error) { el('ask-status').textContent = error.message; } };
el('refresh').onclick = loadDocuments;
el('upload-form').onsubmit = async event => {
  event.preventDefault(); const button = event.submitter, generation = epoch; button.disabled = true; el('upload-status').textContent = 'Reading and indexing your PDF…';
  try { await api('/api/documents',{method:'POST',body:new FormData(event.target)}); if (generation !== epoch) return;
    el('upload-status').textContent = 'Document indexed. You can ask questions now.'; event.target.reset(); await loadDocuments();
  } catch(error) { if (generation === epoch) el('upload-status').textContent = error.message; } finally { button.disabled = false; }
};
el('ask-form').onsubmit = async event => {
  event.preventDefault(); const question = el('question').value.trim(); if (!question) return;
  const button = el('ask-form').querySelector('button[type="submit"]'), generation = epoch;
  if (button.disabled) return;
  button.disabled = true; message(question,'user'); el('question').value = ''; el('ask-status').textContent = '';
  const pending = message('Finding permitted sources and preparing your answer…', 'status');
  try {
    const result = await jsonPost('/api/ask',{question}); if (generation !== epoch) return;
    pending.remove();
    const box = message(result.answer, result.reason === 'access_denied' ? 'denied' : 'assistant');
    for (const cite of result.citations) {
      const details = document.createElement('details'), summary = document.createElement('summary'), link = document.createElement('a');
      summary.textContent = `[${cite.source}] ${cite.title} · Page ${cite.page}`; details.append(summary);
      for (const quote of cite.quotes) { const block = document.createElement('blockquote'); block.textContent = quote; details.append(block); }
      link.textContent = 'Open source PDF ↗'; link.href = `${cite.url}#page=${cite.page}`; link.target='_blank'; link.rel='noopener'; details.append(link); box.append(details);
    }
    attachFeedback(box, result.query_id);
    el('ask-status').textContent = '';
  } catch(error) { if (generation === epoch) { pending.textContent = error.message; } } finally { button.disabled = false; }
};
el('question').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!event.repeat) el('ask-form').requestSubmit();
  }
});


let reviewedCases = [];
function element(tag, text, className) {
  const node = document.createElement(tag); if (text) node.textContent = text;
  if (className) node.className = className; return node;
}
function attachFeedback(box, queryId) {
  const form = element('form', '', 'feedback-form'), rating = document.createElement('select');
  rating.setAttribute('aria-label', 'Rate this answer');
  rating.add(new Option('Choose rating', '')); rating.add(new Option('Helpful', '1')); rating.add(new Option('Not helpful', '-1')); rating.required = true;
  const comment = document.createElement('input'); comment.placeholder = 'Optional feedback or correction'; comment.maxLength = 2000; comment.setAttribute('aria-label', 'Feedback comment');
  const button = element('button','Save feedback'), status = element('span','','status'); status.setAttribute('role','status');
  form.append(rating,comment,button,status); box.append(form);
  form.onsubmit = async event => { event.preventDefault(); button.disabled = true;
    try { await jsonPost('/api/feedback',{query_id:queryId,rating:Number(rating.value),comment:comment.value}); status.textContent='Feedback saved. You can update it here.'; }
    catch(error) { status.textContent=error.message; } finally { button.disabled=false; }
  };
}
function field(form, title, control) {
  const label = element('label', title); label.append(control); form.append(label); return control;
}
function lines(value) { return value.split('\n').map(x=>x.trim()).filter(Boolean); }
async function loadQuality() {
  const generation = epoch; el('quality-status').textContent='Loading feedback…';
  try {
    const data = await api('/api/quality'); if (generation !== epoch) return;
    reviewedCases = data.cases; el('review-list').replaceChildren(); el('evaluation-cases').replaceChildren();
    for (const entry of data.feedback) {
      const details = document.createElement('details'); details.className='review-item';
      details.append(element('summary',`${entry.rating === 1 ? 'Helpful' : 'Not helpful'} · ${entry.user_id} · ${entry.question}`));
      details.append(element('p',entry.result.answer,'review-answer'),element('p',`Comment: ${entry.comment || 'None'}`));
      for (const cite of entry.result.citations) details.append(element('p',`Source: ${cite.title} · page ${cite.page} · ${cite.quotes.join(' / ')}`));
      const form = document.createElement('form'), target = document.createElement('select'), outcome=document.createElement('select');
      for (const u of data.users) target.add(new Option(`${u.username} (${u.role})`,u.id)); target.value=entry.user_id;
      for (const name of ['answered','access_denied','abstained']) outcome.add(new Option(name.replace('_',' '),name));
      field(form,'Test as user',target); field(form,'Expected outcome',outcome);
      const contains=field(form,'Required answer text (one fragment per line)',document.createElement('textarea'));
      const sources=field(form,'Required source filenames (one per line)',document.createElement('textarea'));
      const forbidden=field(form,'Forbidden text (optional, one fragment per line)',document.createElement('textarea'));
      for (const input of [contains,sources,forbidden]) input.rows=2;
      const restoreCase = () => { const saved=data.cases.find(c=>c.feedback_id===entry.id && c.user_id===target.value);
        outcome.value=saved?.expectation.outcome || 'answered';
        contains.value=(saved?.expectation.contains || []).join('\n'); sources.value=(saved?.expectation.sources || []).join('\n'); forbidden.value=(saved?.expectation.forbidden || []).join('\n');
      }; target.onchange=restoreCase; restoreCase();
      const save=element('button','Save reviewed case'), status=element('p','','status'); status.setAttribute('role','status'); form.append(save,status);
      form.onsubmit=async event=> { event.preventDefault(); save.disabled=true;
        try { await jsonPost('/api/evaluations',{feedback_id:entry.id,user_id:target.value,expectation:{outcome:outcome.value,contains:lines(contains.value),sources:lines(sources.value),forbidden:lines(forbidden.value)}});
          if (generation===epoch) await loadQuality();
        } catch(error) {status.textContent=error.message;} finally {save.disabled=false;}
      }; details.append(form); el('review-list').append(details);
    }
    if (!data.feedback.length) el('review-list').append(element('p','No feedback yet. Rate an answer in the chat first.','muted'));
    for (const c of reviewedCases) el('evaluation-cases').append(element('p',`#${c.id} · ${c.user_id} (${c.expected_role}) · ${c.question} → ${c.expectation.outcome}`));
    el('quality-status').textContent=`${reviewedCases.length} reviewed cases. Saving again updates the case for that feedback and user.`;
  } catch(error) {if(generation===epoch) el('quality-status').textContent=error.message;}
}
el('review-refresh').onclick=loadQuality;
el('run-evaluations').onclick=async () => {
  const generation=epoch, button=el('run-evaluations'); button.disabled=true;
  try {
    if (!reviewedCases.length) {el('quality-status').textContent='Load feedback and save reviewed cases first.';return;}
    const casesToRun=[...reviewedCases]; let passed=0; el('evaluation-cases').replaceChildren();
    for (const [index,c] of casesToRun.entries()) {
      if(generation!==epoch) return;
      el('quality-status').textContent=`Running ${index+1} of ${casesToRun.length} as ${c.user_id}…`;
      const report=await jsonPost(`/api/evaluations/${c.id}/run`,{}); if(generation!==epoch)return;
      const result=report.cases[0]; passed+=Number(result.passed);
      const row=element('details','','review-item'); row.append(element('summary',`${result.passed?'PASS':'FAIL'} · ${c.user_id} · ${c.question}`));
      row.append(element('p',result.failed_checks.join(' ') || 'All configured checks passed.'));
      if(result.result) row.append(element('p',result.result.answer,'review-answer'));
      el('evaluation-cases').append(row);
    }
    el('quality-status').textContent=`${passed}/${casesToRun.length} cases passed. These checks do not replace reviewing answer correctness.`;
  } catch(error) {if(generation===epoch)el('quality-status').textContent=error.message;} finally {button.disabled=false;}
};
restore().catch(error => el('login-status').textContent = error.message);
