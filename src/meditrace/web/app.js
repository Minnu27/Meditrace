const list = document.querySelector('#document-list');
const count = document.querySelector('#document-count');
const form = document.querySelector('#upload-form');
const statusLine = document.querySelector('#form-status');
const fileInput = document.querySelector('#file');
const timelineForm = document.querySelector('#timeline-form');
const timelineList = document.querySelector('#timeline-list');
const flagsForm = document.querySelector('#flags-form');
const flagsList = document.querySelector('#flags-list');
const askForm = document.querySelector('#ask-form');
const askResult = document.querySelector('#ask-result');
const loginOverlay = document.querySelector('#login-overlay');
const loginForm = document.querySelector('#login-form');
const loginStatus = document.querySelector('#login-status');
const sessionUser = document.querySelector('#session-user');
const logoutButton = document.querySelector('#logout-button');

const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const fileType = name => (name.split('.').pop() || 'DOC').toUpperCase().slice(0, 4);

// Session token lives only in this tab (sessionStorage), never localStorage:
// it is cleared when the tab closes and is not sent anywhere except this
// API's own Authorization header.
const session = {
  get token() { return sessionStorage.getItem('meditrace_token'); },
  get email() { return sessionStorage.getItem('meditrace_email'); },
  get role() { return sessionStorage.getItem('meditrace_role'); },
  set(token, email, role) {
    sessionStorage.setItem('meditrace_token', token);
    sessionStorage.setItem('meditrace_email', email);
    sessionStorage.setItem('meditrace_role', role);
  },
  clear() {
    sessionStorage.removeItem('meditrace_token');
    sessionStorage.removeItem('meditrace_email');
    sessionStorage.removeItem('meditrace_role');
  },
};

function updateSessionChrome() {
  const authenticated = Boolean(session.token);
  loginOverlay.classList.toggle('visible', !authenticated);
  logoutButton.hidden = !authenticated;
  sessionUser.textContent = authenticated ? `${session.email} · ${session.role.toUpperCase()}` : '';
}

async function authFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (session.token) headers.set('Authorization', `Bearer ${session.token}`);
  const response = await fetch(path, {...options, headers});
  if (response.status === 401) {
    session.clear();
    updateSessionChrome();
  }
  return response;
}

loginForm.addEventListener('submit', async event => {
  event.preventDefault();
  const data = new FormData(loginForm);
  loginStatus.textContent = 'Signing in…';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: data.get('email'), password: data.get('password')}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Sign-in failed');
    session.set(result.access_token, data.get('email'), result.role);
    loginStatus.textContent = '';
    loginForm.reset();
    updateSessionChrome();
    await loadDocuments();
  } catch (error) {
    loginStatus.textContent = error.message;
  }
});

logoutButton.addEventListener('click', () => { session.clear(); updateSessionChrome(); });

async function loadDocuments() {
  if (!session.token) return;
  try {
    const response = await authFetch('/api/documents');
    if (!response.ok) throw new Error('Document register is unavailable');
    const data = await response.json();
    count.textContent = String(data.total).padStart(3, '0');
    list.innerHTML = data.items.length ? data.items.map(doc => `
      <div class="document">
        <div class="doc-icon">${fileType(escapeHtml(doc.filename))}</div>
        <div><h3>${escapeHtml(doc.filename)}</h3><p>${escapeHtml(doc.patient_id)} · ${(doc.size_bytes / 1024).toFixed(1)} KB · ${new Date(doc.created_at).toLocaleString()}</p></div>
        <div class="doc-actions"><span class="badge">${escapeHtml(doc.document_type || 'unknown')}</span><button type="button" class="extract" data-id="${doc.id}">EXTRACT</button></div>
      </div>`).join('') : '<p class="empty">No documents yet. Ingest the first synthetic source to begin.</p>';
  } catch (error) {
    list.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

fileInput.addEventListener('change', () => {
  document.querySelector('#file-name').textContent = fileInput.files[0]?.name || 'No source selected';
});
document.querySelector('#refresh').addEventListener('click', loadDocuments);
list.addEventListener('click', async event => {
  if (!event.target.matches('.extract')) return;
  const response = await authFetch(`/api/documents/${event.target.dataset.id}/extract`, {method: 'POST'});
  const data = await response.json();
  event.target.textContent = response.ok ? 'QUEUED' : (data.detail || 'FAILED');
  event.target.disabled = response.ok;
});
form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  statusLine.textContent = 'Securing source document…';
  try {
    const response = await authFetch('/api/documents', {method: 'POST', body: new FormData(form)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Upload failed');
    statusLine.textContent = `Source registered as ${result.id.slice(0, 8)}.`;
    form.reset(); document.querySelector('#file-name').textContent = 'No source selected';
    await loadDocuments();
  } catch (error) { statusLine.textContent = error.message; }
  finally { button.disabled = false; }
});

timelineForm.addEventListener('submit', async event => {
  event.preventDefault();
  const patient = new FormData(timelineForm).get('patient_id');
  const response = await authFetch(`/api/patients/${encodeURIComponent(patient)}/timeline`);
  const data = await response.json();
  if (!response.ok) { timelineList.innerHTML = `<p class="empty">${escapeHtml(data.detail || 'Timeline unavailable')}</p>`; return; }
  timelineList.innerHTML = data.total ? Object.entries(data.groups).map(([period, entries]) => `<section class="time-group"><h3>${escapeHtml(period)}</h3><div>${entries.map(item => `<article><div><b>${escapeHtml(item.test_or_finding)}</b><span>${escapeHtml(item.fact_type)} · <em class="tier tier-${escapeHtml(item.reliability_tier)}">${escapeHtml(item.reliability_tier)}</em></span></div><strong>${escapeHtml(item.value || 'Documented')} ${escapeHtml(item.unit || '')}</strong>${item.numeric_delta !== null ? `<small>Δ ${item.numeric_delta > 0 ? '+' : ''}${item.numeric_delta}</small>` : ''}<a href="/api/documents/${item.source_document_id}/content" target="_blank">${escapeHtml(item.source_filename)} · evidence p.${item.evidence_location.page}</a></article>`).join('')}</div></section>`).join('') : '<p class="empty">No evidence facts found for this patient.</p>';
});

flagsForm.addEventListener('submit', async event => {
  event.preventDefault();
  const patient = new FormData(flagsForm).get('patient_id');
  const response = await authFetch(`/api/patients/${encodeURIComponent(patient)}/flags`);
  const data = await response.json();
  if (!response.ok) { flagsList.innerHTML = `<p class="empty">${escapeHtml(data.detail || 'Flags unavailable')}</p>`; return; }
  const trendItems = data.trends.map(t => `<article><div><b>${escapeHtml(t.test_or_finding)}</b><span>TREND · ${escapeHtml(t.direction).toUpperCase()}</span></div><strong>${escapeHtml(t.from_value)} → ${escapeHtml(t.to_value)}</strong><small>${escapeHtml(t.from_date)} → ${escapeHtml(t.to_date)}</small></article>`).join('');
  const contradictionItems = data.contradictions.map(c => `<article><div><b>${escapeHtml(c.kind.replace('_', ' ').toUpperCase())}</b></div><strong>${escapeHtml(c.summary)}</strong><small>${c.fact_ids.length} source facts, never auto-resolved</small></article>`).join('');
  flagsList.innerHTML = (trendItems + contradictionItems) || '<p class="empty">No trends or contradictions detected for this patient.</p>';
});

askForm.addEventListener('submit', async event => {
  event.preventDefault();
  const data = new FormData(askForm);
  const patient = data.get('patient_id');
  askResult.innerHTML = '<p class="empty">Retrieving evidence…</p>';
  const response = await authFetch(`/api/patients/${encodeURIComponent(patient)}/ask`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({question: data.get('question')}),
  });
  const result = await response.json();
  if (!response.ok) { askResult.innerHTML = `<p class="empty">${escapeHtml(result.detail || 'Question could not be answered')}</p>`; return; }
  const citations = result.cited_fact_ids.map(id => `<span class="badge">${id.slice(0, 8)}</span>`).join(' ');
  askResult.innerHTML = `<article><strong>${escapeHtml(result.answer)}</strong><p>${result.insufficient_evidence ? 'No supporting facts were retrieved.' : `Cited facts: ${citations}`}</p></article>`;
});

updateSessionChrome();
loadDocuments();
