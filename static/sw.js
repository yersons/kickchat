const V = "kc-v1";
const SHELL = ["/", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== "GET" || u.origin !== location.origin) return;
  if (/^\/(api|ws|auth|callback|health)/.test(u.pathname)) return;   // siempre en vivo
  if (r.mode === "navigate") {                                       // la página: red primero
    e.respondWith(fetch(r).then(res => {
      const copy = res.clone();
      if (res.ok && u.pathname === "/") caches.open(V).then(c => c.put("/", copy));
      return res;
    }).catch(() => caches.match("/")));
    return;
  }
  if (/^\/(icons|badges)\//.test(u.pathname)) {                      // estáticos: caché primero
    e.respondWith(caches.match(r).then(hit => hit || fetch(r).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(V).then(c => c.put(r, copy)); }
      return res;
    })));
  }
});
