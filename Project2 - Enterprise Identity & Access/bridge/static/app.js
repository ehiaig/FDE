const form = document.querySelector('#query-form');
if (form) {
  function refreshIdentity() {
    return fetch('/api/me').then(r => r.json()).then(user => {
      document.querySelector('#identity-role').textContent = user.role ? `${user.tenant} / ${user.role}` : 'Session ended';
    }).catch(() => {});
  }
  refreshIdentity();
  document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => {
    document.querySelector('#question').value = button.dataset.question;
    form.requestSubmit();
  }));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const result = document.querySelector('#result');
    const answer = document.querySelector('#answer');
    const sources = document.querySelector('#sources');
    const buttons = [document.querySelector('#ask'), ...document.querySelectorAll('[data-question]')];
    buttons.forEach(b => b.disabled = true);
    result.hidden = false;
    answer.className = '';
    answer.textContent = 'Searching your authorized documents…';
    sources.replaceChildren();
    try {
      const response = await fetch('/api/query', {method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('[name="csrf-token"]').content
      }, body: JSON.stringify({question: document.querySelector('#question').value})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'The query failed.');
      document.querySelector('#answer-heading').textContent = data.mode === 'extractive' ? 'Matching knowledge' : 'Generated answer';
      answer.textContent = data.answer;
      data.sources.forEach(source => {
        const card = document.createElement('article'); card.className = 'source';
        const title = document.createElement('h4'); title.textContent = `${source.title} · ${source.role}`;
        const excerpt = document.createElement('p'); excerpt.textContent = source.body;
        card.append(title, excerpt); sources.append(card);
      });
    } catch (error) { answer.className = 'error'; answer.textContent = error.message; }
    finally { buttons.forEach(b => b.disabled = false); refreshIdentity(); }
  });
}
