// Minimal service worker: caches the app shell so the driver page opens with poor signal.
const CACHE = "rx-driver-v1";
self.addEventListener("install", e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(["/driver", "/driver-static/manifest.json"]))));
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET" || e.request.url.includes("/api/")) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
