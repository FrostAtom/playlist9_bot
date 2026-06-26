/* playlist9 service worker — minimal offline shell.
 *
 * Purpose is installability ("Add to Home Screen") + a fast, offline-tolerant
 * shell, NOT caching audio. Search and download always hit the network (those
 * requests are bypassed below) so results are never stale and large MP3 blobs
 * never land in the cache.
 */
const CACHE = "playlist9-shell-v1";
const SHELL = [".", "manifest.webmanifest", "icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;                       // never cache POST /api/download
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;        // let fonts/CDN go straight to network
  if (url.pathname.startsWith("/api/")) return;           // search/download are always live

  // Navigations: network-first so a fresh shell wins, fall back to cache offline.
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => caches.match(".")));
    return;
  }
  // Static assets (icons, manifest): cache-first, fill the cache on first hit.
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return res;
    }))
  );
});
