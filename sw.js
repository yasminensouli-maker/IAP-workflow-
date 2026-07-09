const CACHE = 'iap-static-v1';
const ASSETS = [
  '/manifest.json',
  '/logo.png',
  '/icon-192.png',
  '/icon-512.png'
];

self.addEventListener('install', function(e){
  e.waitUntil(
    caches.open(CACHE).then(function(cache){
      return cache.addAll(ASSETS);
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function(e){
  e.waitUntil(
    caches.keys().then(function(keys){
      return Promise.all(
        keys.filter(function(k){ return k !== CACHE; })
            .map(function(k){ return caches.delete(k); })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', function(e){
  // Navigation requests (index.html, the app root) are NEVER intercepted —
  // they go straight to the network, every time, no exceptions. This is the
  // actual root-cause fix: customHttp.yml now tells the browser not to cache
  // index.html at the HTTP level at all, so there is nothing left for this
  // service worker to manage here. Every previous version of this file tried
  // to solve staleness with cleverer JS logic (cache-first, then network-
  // first-with-fallback, then auto-reload-on-update) — all of that was
  // working around the browser caching this exact file. Removing the
  // interception entirely removes the whole class of bug.
  if(e.request.mode === 'navigate' || e.request.url.endsWith('/index.html') || e.request.url.endsWith('/')){
    e.respondWith(fetch(e.request));
    return;
  }

  // Always go to network for API calls — never cache these
  if(e.request.url.includes('execute-api') || e.request.url.includes('amazonaws.com')){
    e.respondWith(fetch(e.request));
    return;
  }

  // Everything else (logo, icons, manifest) — cache-first is fine, these
  // rarely change and aren't the source of any staleness complaint.
  e.respondWith(
    caches.match(e.request).then(function(cached){
      return cached || fetch(e.request).then(function(response){
        return caches.open(CACHE).then(function(cache){
          cache.put(e.request, response.clone());
          return response;
        });
      });
    })
  );
});
