/* TrendAlert service worker -- the app shell only, never the data.

   Published beside the dashboard (gs://BUCKET/next/sw.js), so its scope is
   /BUCKET/next/ and nothing else in the bucket.

   It handles ONE kind of request: navigations to the dashboard page. Those go
   network-first -- online you always get the deploy that is live right now
   (the object is no-cache for the same reason); the copy kept in the cache is
   only shown when the network fails, so an installed app opens to its own
   screen instead of Safari's "you are offline" page.

   Everything else -- data.json, chart.json, the notes API -- is NOT intercepted
   and goes straight to the network exactly as it does in a browser tab. A
   cached price list is a wrong price list; the dashboard's staleness badge is
   the only thing allowed to speak about old data. */

const CACHE = "trendalert-shell-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith((async () => {
    try {
      const res = await fetch(event.request);
      if (res.ok) {
        const copy = res.clone();
        event.waitUntil(caches.open(CACHE).then((c) => c.put(event.request.url.split("?")[0], copy)));
      }
      return res;
    } catch (err) {
      const hit = await caches.match(event.request.url.split("?")[0]);
      if (hit) return hit;
      throw err;
    }
  })());
});
