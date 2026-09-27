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

// Set from /api/health. With REQUIRE_LOGIN off on the server there is no
// sign-in screen and requests need no token.
let loginRequired = false;
const isSignedIn = () => !loginRequired || Boolean(session.token);

function updateSessionChrome() {
  loginOverlay.classList.toggle('visible', !isSignedIn());
  logoutButton.hidden = !loginRequired || !session.token;
  sessionUser.textContent = loginRequired && session.token ? `${session.email} · ${session.role.toUpperCase()}` : '';
}

function signOut() {
  session.clear();
  // Wipe anything patient-related that was rendered for the previous user.
  [list, timelineList, flagsList, askResult].forEach(el => { el.innerHTML = '<p class="empty">Signed out.</p>'; });
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

logoutButton.addEventListener('click', signOut);

async function loadDocuments() {
  if (!isSignedIn()) return;
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
// Updated from /api/health. Vercel rejects request bodies over ~4.5 MB before
// they reach the app, so the default matches the Vercel limit.
let maxUploadBytes = 4000000;
const DIRECT_IMAGE_TYPES = ['image/png', 'image/jpeg'];
const MAX_IMAGE_SIDE = 2400;

// Phone photos are often 5–12 MB or HEIC/WebP. Re-encode any image that is
// too big or not PNG/JPEG as a JPEG that fits the limit. Returns the original
// file when it can be sent as is, or null if the browser cannot decode it.
async function prepareImage(file) {
  if (!file.type.startsWith('image/') && !/\.(heic|heif|webp|gif|bmp)$/i.test(file.name)) return file;
  if (DIRECT_IMAGE_TYPES.includes(file.type) && file.size <= maxUploadBytes) return file;
  let bitmap;
  try { bitmap = await createImageBitmap(file); } catch { return null; }
  let scale = Math.min(1, MAX_IMAGE_SIDE / Math.max(bitmap.width, bitmap.height));
  let blob = null;
  // Try a few quality levels, then shrink the image further, until it fits.
  for (let attempt = 0; attempt < 5; attempt++, scale *= 0.7) {
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    for (const quality of [0.88, 0.75, 0.6]) {
      blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', quality));
      if (blob && blob.size <= maxUploadBytes) break;
    }
    if (blob && blob.size <= maxUploadBytes) break;
  }
  if (!blob) return null;
  return new File([blob], file.name.replace(/\.[^.]+$/, '') + '.jpg', {type: 'image/jpeg'});
}

async function readError(response) {
  // Vercel's own "payload too large" and gateway errors are not JSON.
  if (response.status === 413) return `File is too large (max ${(maxUploadBytes / 1e6).toFixed(0)} MB).`;
  try { return (await response.json()).detail || `Upload failed (${response.status})`; }
  catch { return `Upload failed (${response.status} ${response.statusText})`; }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  statusLine.textContent = 'Preparing document…';
  try {
    const original = fileInput.files[0];
    if (!original) throw new Error('Choose a file first.');
    const file = await prepareImage(original);
    if (!file) throw new Error('This image format cannot be read by your browser. Save it as JPG or PNG and try again.');
    if (file.size > maxUploadBytes) throw new Error(`File is ${(file.size / 1e6).toFixed(1)} MB; the limit is ${(maxUploadBytes / 1e6).toFixed(0)} MB.`);
    const body = new FormData();
    body.append('patient_id', new FormData(form).get('patient_id'));
    body.append('file', file, file.name);
    statusLine.textContent = 'Uploading…';
    const response = await authFetch('/api/documents', {method: 'POST', body});
    if (!response.ok) throw new Error(await readError(response));
    const result = await response.json();
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
  timelineList.innerHTML = data.total ? Object.entries(data.groups).map(([period, entries]) => `<section class="time-group"><h3>${escapeHtml(period)}</h3><div>${entries.map(item => `<article><div><b>${escapeHtml(item.test_or_finding)}</b><span>${escapeHtml(item.fact_type)} · <em class="tier tier-${escapeHtml(item.reliability_tier)}">${escapeHtml(item.reliability_tier)}</em></span></div><strong>${escapeHtml(item.value || 'Documented')} ${escapeHtml(item.unit || '')}</strong>${item.numeric_delta !== null ? `<small>Δ ${item.numeric_delta > 0 ? '+' : ''}${item.numeric_delta}</small>` : ''}<a href="#" class="evidence-link" data-id="${escapeHtml(item.source_document_id)}">${escapeHtml(item.source_filename)} · evidence p.${item.evidence_location.page}</a></article>`).join('')}</div></section>`).join('') : '<p class="empty">No evidence facts found for this patient.</p>';
});

// Source documents need the bearer token, so a plain link would always get
// 401. Fetch with auth and open the bytes as a short-lived local blob URL.
timelineList.addEventListener('click', async event => {
  const link = event.target.closest('.evidence-link');
  if (!link) return;
  event.preventDefault();
  const viewer = window.open('', '_blank');
  const response = await authFetch(`/api/documents/${encodeURIComponent(link.dataset.id)}/content`);
  if (!response.ok) { viewer?.close(); link.textContent = 'Source unavailable'; return; }
  const url = URL.createObjectURL(await response.blob());
  if (viewer) viewer.location = url; else window.location = url;
  setTimeout(() => URL.revokeObjectURL(url), 60000);
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

(async () => {
  try {
    const health = await (await fetch('/api/health')).json();
    loginRequired = health.login_required === true;
    if (health.max_upload_bytes) maxUploadBytes = health.max_upload_bytes;
  } catch { /* server unreachable: loadDocuments shows the error */ }
  updateSessionChrome();
  loadDocuments();
})();

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
}
