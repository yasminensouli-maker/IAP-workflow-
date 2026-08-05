/* Self-destructing service worker.
   Any browser that still has an old worker installed will fetch this file
   on its next update check, install it, and it immediately unregisters
   itself and deletes every cache. After that the app is served straight
   from the network with the no-cache headers in customHttp.yml, and the
   whole class of "I deployed but I can't see it" problems is gone. */
self.addEventListener('install', function(){ self.skipWaiting(); });
self.addEventListener('activate', function(e){
  e.waitUntil(
    caches.keys()
      .then(function(keys){ return Promise.all(keys.map(function(k){ return caches.delete(k); })); })
      .then(function(){ return self.registration.unregister(); })
      .then(function(){ return self.clients.matchAll({type:'window'}); })
      .then(function(clients){ clients.forEach(function(c){ c.navigate(c.url); }); })
  );
});
/* No fetch handler at all. Nothing is intercepted, nothing is cached. */
