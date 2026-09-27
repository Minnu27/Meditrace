// Installable-app shell. Only the static UI is cached so the app opens
// instantly; /api/* responses (patient data, tokens) are never cached or
// intercepted, so nothing sensitive is left on the device.
const CACHE = 'meditrace-shell-v1';
const SHELL = ['/', '/styles.css', '/app.js', '/manifest.json', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/icon.svg'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  // Network first so a new deployment is picked up; fall back to the cached shell offline.
  event.respondWith(
    fetch(event.request)
      .then(response => {
        if (response.ok && SHELL.includes(url.pathname)) {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put(url.pathname, copy));
        }
        return response;
      })
      .catch(() => caches.match(url.pathname).then(hit => hit || caches.match('/'))),
  );
});
