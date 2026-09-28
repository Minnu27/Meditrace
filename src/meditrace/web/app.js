const $ = selector => document.querySelector(selector);
const list = $('#document-list');
const count = $('#document-count');
const form = $('#upload-form');
const statusLine = $('#form-status');
const fileInput = $('#file');
const timelineForm = $('#timeline-form');
const timelineList = $('#timeline-list');
const flagsForm = $('#flags-form');
const flagsList = $('#flags-list');
const askForm = $('#ask-form');
const askResult = $('#ask-result');
const verifyForm = $('#verify-form');
const verifyResult = $('#verify-result');
const ledgerBar = $('#ledger-bar');
const ledgerStatus = $('#ledger-status');
const anchorButton = $('#anchor-button');
const loginOverlay = $('#login-overlay');
const loginForm = $('#login-form');
const loginStatus = $('#login-status');
const sessionUser = $('#session-user');
const logoutButton = $('#logout-button');

const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const fileType = name => (name.split('.').pop() || 'DOC').toUpperCase().slice(0, 4);
const short = id => escapeHtml(String(id).slice(0, 8));
const factChip = id => `<button type="button" class="fact-chip" data-fact="${escapeHtml(id)}" title="Verify fact ${escapeHtml(id)}">${short(id)}</button>`;
const answerChip = id => `<button type="button" class="answer-chip" data-answer="${escapeHtml(id)}" title="Verify answer ${escapeHtml(id)}">${short(id)}</button>`;

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

// Set from /api/health. With REQUIRE_LOGIN off on the server there is no
// sign-in screen and requests need no token (and act as an admin).
let loginRequired = false;
const isSignedIn = () => !loginRequired || Boolean(session.token);
const isAdmin = () => !loginRequired || session.role === 'admin';

function updateSessionChrome() {
  loginOverlay.classList.toggle('visible', !isSignedIn());
  logoutButton.hidden = !loginRequired || !session.token;
  sessionUser.textContent = loginRequired && session.token ? `${session.email} · ${session.role.toUpperCase()}` : '';
  ledgerBar.hidden = !(isSignedIn() && isAdmin());
}

function signOut() {
  session.clear();
  // Wipe anything patient-related that was rendered for the previous user.
  [list, timelineList, flagsList, askResult, verifyResult].forEach(el => { el.innerHTML = '<p class="empty">Signed out.</p>'; });
  count.textContent = '000';
  updateSessionChrome();
}

async function authFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (session.token) headers.set('Authorization', `Bearer ${session.token}`);
  const response = await fetch(path, {...options, headers});
  if (response.status === 401) signOut();
  return response;
}

async function getJson(path, options) {
  const response = await authFetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
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
    loadLedger();
    routeFromHash();
  } catch (error) {
    loginStatus.textContent = error.message;
  }
});

logoutButton.addEventListener('click', signOut);

// ------------------------------------------------------------- documents

async function loadDocuments() {
  if (!isSignedIn()) return;
  try {
    const data = await getJson('/api/documents');
    count.textContent = String(data.total).padStart(3, '0');
    list.innerHTML = data.items.length ? data.items.map(doc => `
      <div class="document">
        <div class="doc-icon">${fileType(escapeHtml(doc.filename))}</div>
        <div><h3>${escapeHtml(doc.filename)}</h3><p>${escapeHtml(doc.patient_id)} · ${(doc.size_bytes / 1024).toFixed(1)} KB · ${doc.facts.length} facts · <span class="hash" title="${escapeHtml(doc.sha256)}">sha256 ${escapeHtml(doc.sha256.slice(0, 12))}…</span></p></div>
        <div class="doc-actions"><span class="badge">${escapeHtml(doc.document_type || 'unknown')}</span><button type="button" class="extract" data-id="${doc.id}">${doc.status === 'ready' ? 'RE-EXTRACT' : 'EXTRACT'}</button></div>
      </div>`).join('') : '<p class="empty">No documents yet. Ingest the first synthetic source to begin.</p>';
  } catch (error) {
    list.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

fileInput.addEventListener('change', () => {
  $('#file-name').textContent = fileInput.files[0]?.name || 'No source selected';
});
$('#refresh').addEventListener('click', loadDocuments);
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
    const result = await getJson('/api/documents', {method: 'POST', body: new FormData(form)});
    statusLine.textContent = `Source registered as ${result.id.slice(0, 8)} · sha256 ${result.sha256.slice(0, 12)}…`;
    form.reset(); $('#file-name').textContent = 'No source selected';
    await loadDocuments();
  } catch (error) { statusLine.textContent = error.message; }
  finally { button.disabled = false; }
});

// Source documents need the bearer token, so a plain link would always get
// 401. Fetch with auth and open the bytes as a short-lived local blob URL.
async function openSource(documentId, link) {
  const viewer = window.open('', '_blank');
  const response = await authFetch(`/api/documents/${encodeURIComponent(documentId)}/content`);
  if (!response.ok) { viewer?.close(); if (link) link.textContent = 'Source unavailable'; return; }
  const url = URL.createObjectURL(await response.blob());
  if (viewer) viewer.location = url; else window.location = url;
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// ------------------------------------------------------ timeline + flags

function flagIndex(flags) {
  const index = {};
  const add = (id, cls, label) => { (index[id] ||= []).push(`<em class="flag-tag ${cls}">${escapeHtml(label)}</em>`); };
  flags.trends.forEach(t => { add(t.to_fact_id, 'flag-trend', `TREND ${t.direction}`); });
  flags.contradictions.forEach(c => c.fact_ids.forEach(id => add(id, 'flag-contradiction', c.kind.replace('_', ' '))));
  flags.gaps.forEach(g => g.fact_ids.forEach(id => add(id, 'flag-gap', g.kind.replace(/_/g, ' '))));
  return index;
}

timelineForm.addEventListener('submit', async event => {
  event.preventDefault();
  const patient = encodeURIComponent(new FormData(timelineForm).get('patient_id'));
  timelineList.innerHTML = '<p class="empty">Loading record…</p>';
  try {
    const [data, flags] = await Promise.all([
      getJson(`/api/patients/${patient}/timeline`),
      getJson(`/api/patients/${patient}/flags`),
    ]);
    const tags = flagIndex(flags);
    timelineList.innerHTML = data.total ? Object.entries(data.groups).map(([period, entries]) => `
      <section class="time-group"><h3>${escapeHtml(period)}</h3><div>${entries.map(item => `
        <article>
          <div><b>${escapeHtml(item.test_or_finding)}</b><span>${escapeHtml(item.fact_type)} · ${escapeHtml(item.observed_date)} · reliability <em class="tier tier-${escapeHtml(item.reliability_tier)}">${escapeHtml(item.reliability_tier)}</em>${item.details?.ocr ? ' · OCR' : ''}${item.details?.date_source === 'upload_date_fallback' ? ' · date = upload' : ''}</span>${(tags[item.id] || []).join('')}</div>
          <strong>${escapeHtml(item.value || 'Documented')} ${escapeHtml(item.unit || '')}${item.status ? ` <small>${escapeHtml(item.status)}</small>` : ''}</strong>
          ${item.numeric_delta !== null ? `<small>Δ ${item.numeric_delta > 0 ? '+' : ''}${item.numeric_delta}</small>` : '<span></span>'}
          <div class="row-foot">${factChip(item.id)}<a href="#" class="evidence-link" data-id="${escapeHtml(item.source_document_id)}">${escapeHtml(item.source_filename)} · p.${item.evidence_location.page}${item.evidence_location.line_start ? ` l.${item.evidence_location.line_start}` : ''}</a>${item.evidence_location.quote ? `<span class="hash">“${escapeHtml(item.evidence_location.quote)}”</span>` : ''}</div>
        </article>`).join('')}</div></section>`).join('') : '<p class="empty">No evidence facts found for this patient.</p>';
  } catch (error) {
    timelineList.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
});

timelineList.addEventListener('click', event => {
  const link = event.target.closest('.evidence-link');
  if (!link) return;
  event.preventDefault();
  openSource(link.dataset.id, link);
});

flagsForm.addEventListener('submit', async event => {
  event.preventDefault();
  const patient = encodeURIComponent(new FormData(flagsForm).get('patient_id'));
  try {
    const data = await getJson(`/api/patients/${patient}/flags`);
    const trendItems = data.trends.map(t => `<article><div><b>${escapeHtml(t.test_or_finding)}</b><span>TREND · ${escapeHtml(t.direction)}</span></div><strong>${escapeHtml(t.from_value)} → ${escapeHtml(t.to_value)}</strong><small>${escapeHtml(t.from_date)} → ${escapeHtml(t.to_date)}</small><div class="chips">${factChip(t.from_fact_id)}${factChip(t.to_fact_id)}</div></article>`).join('');
    const contradictionItems = data.contradictions.map(c => `<article><div><b>${escapeHtml(c.kind.replace('_', ' ').toUpperCase())}</b></div><strong>${escapeHtml(c.summary)}</strong><small>${c.fact_ids.length} source facts, never auto-resolved</small><div class="chips">${c.fact_ids.map(factChip).join('')}</div></article>`).join('');
    const gapItems = data.gaps.map(g => `<article class="gap"><div><b>${escapeHtml(g.kind.replace(/_/g, ' ').toUpperCase())}</b><span>${escapeHtml(g.rule_id)} · rules ${escapeHtml(g.rule_version)}</span></div><strong>${escapeHtml(g.summary)}</strong><small>Due by ${escapeHtml(g.due_by)} (as of ${escapeHtml(data.as_of)}). A gap in the record, not a clinical finding — a document may simply be missing.</small><div class="chips">${g.fact_ids.map(factChip).join('')}${g.later_fact_ids.map(factChip).join('')}</div></article>`).join('');
    flagsList.innerHTML = (trendItems + contradictionItems + gapItems) || '<p class="empty">No trends, contradictions, or gaps detected for this patient.</p>';
  } catch (error) {
    flagsList.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
});

// -------------------------------------------------------------------- ask

askForm.addEventListener('submit', async event => {
  event.preventDefault();
  const data = new FormData(askForm);
  askResult.innerHTML = '<p class="empty">Retrieving evidence…</p>';
  try {
    const result = await getJson(`/api/patients/${encodeURIComponent(data.get('patient_id'))}/ask`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: data.get('question')}),
    });
    askResult.innerHTML = `<article><strong>${escapeHtml(result.answer)}</strong>
      <p>${result.insufficient_evidence ? 'No supporting facts were retrieved.' : `Cited facts: ${result.cited_fact_ids.map(factChip).join('')}`}</p>
      <div class="answer-meta">Answer ${answerChip(result.answer_id)} · ${escapeHtml(result.answerer || '')}${result.prompt_version ? ` · prompt ${escapeHtml(result.prompt_version)}` : ''} · sealed <span class="hash">${escapeHtml((result.content_hash || '').slice(0, 16))}…</span></div></article>`;
  } catch (error) {
    askResult.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
});

// ----------------------------------------------------------------- verify

const VERDICTS = {
  verified: ['✓', 'Verified', 'Every link recomputes from stored data and the record sits under a valid signed anchor.'],
  verified_pending_anchor: ['…', 'Verified · awaiting anchor', 'Every link recomputes; the audit entry will be covered by the next periodic anchor.'],
  failed: ['✗', 'Verification failed', 'At least one link does not match. See the failing step below.'],
};
const statusOf = (report, ids) => {
  const found = report.checks.filter(c => ids.includes(c.id));
  if (!found.length) return 'pending';
  if (found.some(c => c.status === 'fail')) return 'fail';
  if (found.some(c => c.status === 'pending')) return 'pending';
  return found.every(c => c.status === 'skipped') ? 'skipped' : 'pass';
};
const kv = pairs => `<dl class="kv">${pairs.filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v, raw]) => `<dt>${escapeHtml(k)}</dt><dd${raw ? ' class="hash"' : ''}>${raw ? escapeHtml(v) : v}</dd>`).join('')}</dl>`;

function proofPath(report) {
  const entry = report.audit_entry;
  const anchor = report.anchor;
  const proof = report.merkle_proof;
  const steps = [];
  if (report.kind === 'fact') {
    steps.push(['Source document', `${report.provenance.source_filename} · sha256`, report.provenance.source_sha256, statusOf(report, ['source_hash', 'source_binding'])]);
    const quoteCheck = report.checks.find(c => c.id === 'quote_in_source');
    steps.push(['Evidence quote', quoteCheck ? quoteCheck.detail : 'quote check unavailable', report.evidence.quote || '(no text quote)', statusOf(report, ['quote_in_source'])]);
  } else {
    steps.push(['Cited facts', `${report.cited_facts.length} facts, each re-verified`, report.cited_facts.map(f => f.fact_id.slice(0, 8)).join(' · ') || '(none)', statusOf(report, report.checks.filter(c => c.id.startsWith('cited_')).map(c => c.id))]);
  }
  steps.push([`${report.kind} commitment`, report.provenance.commitment_version, report.provenance.content_hash, statusOf(report, ['fact_hash', 'answer_hash'])]);
  steps.push([entry ? `Audit entry #${entry.seq}` : 'Audit entry', entry ? `${entry.action} by ${entry.actor} · ${entry.occurred_at}` : 'missing', entry ? entry.entry_hash : '—', statusOf(report, ['audit_entry', 'audit_digest', 'audit_entry_hash', 'audit_chain_link'])]);
  steps.push(['Merkle inclusion', proof ? `${proof.path.length}-step path to the anchored root` : 'not anchored yet', proof ? proof.root : 'next periodic anchor', statusOf(report, ['merkle_inclusion', 'anchor_log_consistent', 'anchor'])]);
  steps.push([anchor ? `Anchor #${anchor.id}` : 'Signed anchor', anchor ? `${anchor.method} · key ${anchor.key_id} · ${anchor.anchored_at}` : 'pending', anchor ? anchor.signature : '—', statusOf(report, ['anchor_signature', 'anchor_chain', 'anchor_timestamp', 'anchor'])]);
  return `<ol class="proof-path">${steps.map(([title, sub, hash, st]) => `<li class="st-${st}"><b>${escapeHtml(title)}</b><span>${escapeHtml(sub)}</span><span class="hash">${escapeHtml(hash)}</span></li>`).join('')}</ol>`;
}

function renderVerify(report) {
  const [mark, title, note] = VERDICTS[report.verdict] || ['?', report.verdict, ''];
  const left = report.kind === 'fact' ? `
      <h3>Fact ${short(report.id)}</h3>
      ${report.evidence.quote ? `<blockquote class="evidence-quote">${escapeHtml(report.evidence.quote)}</blockquote>` : ''}
      ${kv([
        ['Finding', escapeHtml(`${report.fact.test_or_finding}${report.fact.normalized_code ? ` (${report.fact.normalized_code})` : ''}`)],
        ['Value', escapeHtml(`${report.fact.value ?? 'Documented'} ${report.fact.unit ?? ''} ${report.fact.status ? `· ${report.fact.status}` : ''}`)],
        ['Observed', escapeHtml(`${report.fact.observed_date}${report.evidence.details?.date_source === 'upload_date_fallback' ? ' (upload date — none found in source)' : ''}`)],
        ['Location', escapeHtml(`page ${report.evidence.location?.page}${report.evidence.location?.line_start ? `, line ${report.evidence.location.line_start}` : ''}`)],
        ['Patient', escapeHtml(report.fact.patient_id)],
        ['Source', escapeHtml(report.provenance.source_filename)],
        ['Source SHA-256', report.provenance.source_sha256, true],
        ['Extractor', escapeHtml(report.provenance.extractor)],
        ['Version', report.provenance.extractor_version ? escapeHtml(report.provenance.extractor_version) : null],
        ['Prompt', report.provenance.prompt_version ? escapeHtml(report.provenance.prompt_version) : null],
        ['OCR', report.evidence.details?.ocr ? escapeHtml(`${report.evidence.details.ocr.engine} · line confidence ${report.evidence.details.ocr.line_confidence}`) : null],
        ['Content hash', report.provenance.content_hash, true],
      ])}` : `
      <h3>Answer ${short(report.id)}</h3>
      <blockquote class="evidence-quote">Q: ${escapeHtml(report.answer.question)}\nA: ${escapeHtml(report.answer.answer)}</blockquote>
      ${kv([
        ['Patient', escapeHtml(report.answer.patient_id)],
        ['Answerer', escapeHtml(report.provenance.answerer)],
        ['Version', report.provenance.answerer_version ? escapeHtml(report.provenance.answerer_version) : null],
        ['Prompt', report.provenance.prompt_version ? escapeHtml(report.provenance.prompt_version) : null],
        ['Cited facts', report.cited_facts.map(f => factChip(f.fact_id)).join('') || '—'],
        ['Content hash', report.provenance.content_hash, true],
      ])}`;
  verifyResult.innerHTML = `
    <div class="verdict verdict-${escapeHtml(report.verdict)}" role="status"><span class="mark">${mark}</span><div><b>${escapeHtml(title)}</b><p>${escapeHtml(note)}</p></div></div>
    <div class="verify-grid">
      <div>${left}</div>
      <div><h3>Proof path</h3>${proofPath(report)}</div>
    </div>
    <ul class="checks">${report.checks.map(c => `<li class="st-${escapeHtml(c.status)}"><span>${escapeHtml(c.status)}</span><div>${escapeHtml(c.label)}<p>${escapeHtml(c.detail)}</p></div></li>`).join('')}</ul>
    <div class="verify-actions">
      <button type="button" data-bundle="${report.kind}s/${escapeHtml(report.id)}">DOWNLOAD PROOF BUNDLE</button>
      ${report.kind === 'fact' ? `<button type="button" data-source="${escapeHtml(report.provenance.source_document_id)}">OPEN SOURCE DOCUMENT</button>` : ''}
    </div>`;
}

async function openVerify(kind, id, {scroll = true} = {}) {
  verifyForm.elements.kind.value = kind;
  verifyForm.elements.record_id.value = id;
  if (location.hash !== `#verify/${kind}/${id}`) history.replaceState(null, '', `#verify/${kind}/${id}`);
  if (scroll) $('#verify').scrollIntoView({behavior: 'smooth'});
  verifyResult.innerHTML = '<p class="empty">Recomputing every link…</p>';
  try {
    renderVerify(await getJson(`/api/verify/${kind}s/${encodeURIComponent(id)}`));
  } catch (error) {
    verifyResult.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
  loadLedger();
}

verifyForm.addEventListener('submit', event => {
  event.preventDefault();
  const data = new FormData(verifyForm);
  openVerify(data.get('kind'), String(data.get('record_id')).trim(), {scroll: false});
});

verifyResult.addEventListener('click', async event => {
  const bundle = event.target.closest('[data-bundle]');
  const source = event.target.closest('[data-source]');
  if (source) { openSource(source.dataset.source); return; }
  if (!bundle) return;
  const response = await authFetch(`/api/verify/${bundle.dataset.bundle}/bundle`);
  if (!response.ok) { bundle.textContent = 'BUNDLE UNAVAILABLE'; return; }
  const url = URL.createObjectURL(await response.blob());
  const link = Object.assign(document.createElement('a'), {href: url, download: `meditrace-proof-${bundle.dataset.bundle.replace('/', '-').slice(0, 20)}.json`});
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
});

// Any fact or answer ID anywhere on the page opens the verify page.
document.addEventListener('click', event => {
  const fact = event.target.closest('.fact-chip');
  const answer = event.target.closest('.answer-chip');
  if (fact) openVerify('fact', fact.dataset.fact);
  else if (answer) openVerify('answer', answer.dataset.answer);
});

function routeFromHash() {
  const match = location.hash.match(/^#verify\/(fact|answer)\/([0-9a-f-]{36})$/i);
  if (match && isSignedIn()) openVerify(match[1], match[2]);
}
window.addEventListener('hashchange', routeFromHash);

// ----------------------------------------------------------------- ledger

async function loadLedger() {
  if (!isSignedIn() || !isAdmin()) return;
  try {
    const chain = await getJson('/api/audit/verify');
    const anchor = chain.latest_anchor;
    ledgerStatus.innerHTML = `${chain.ok ? '✓ Audit chain intact' : '✗ Audit chain problems found'} · ${chain.length} entries · head <span class="hash">${escapeHtml((chain.head_hash || '').slice(0, 12))}…</span> · ${anchor ? `last anchor #${anchor.id} at ${escapeHtml(anchor.anchored_at)}, ${chain.unanchored_entries} entries since` : 'never anchored'}`;
  } catch (error) {
    ledgerStatus.textContent = error.message;
  }
}

anchorButton.addEventListener('click', async () => {
  anchorButton.disabled = true;
  try {
    const result = await getJson('/api/anchors', {method: 'POST'});
    anchorButton.textContent = result.anchored ? `ANCHORED ${result.anchor.leaf_count} ENTRIES` : 'NOTHING NEW TO ANCHOR';
    const current = new FormData(verifyForm);
    if (current.get('record_id') && verifyResult.querySelector('.verdict')) openVerify(current.get('kind'), current.get('record_id'), {scroll: false});
    else loadLedger();
  } catch (error) {
    anchorButton.textContent = error.message.slice(0, 40);
  } finally {
    setTimeout(() => { anchorButton.textContent = 'ANCHOR NOW'; anchorButton.disabled = false; }, 2500);
  }
});

(async () => {
  try {
    const health = await (await fetch('/api/health')).json();
    loginRequired = health.login_required === true;
  } catch { /* server unreachable: loadDocuments shows the error */ }
  updateSessionChrome();
  await loadDocuments();
  loadLedger();
  routeFromHash();
})();

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
}
