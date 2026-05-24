// HVGL KSNB service worker
// Chỉ cache tài nguyên tĩnh an toàn. Không cache dữ liệu nghiệp vụ, API, phiếu, chat, họp, tài liệu upload.

const HVGL_CACHE_NAME = "hvgl-ksnb-static-v3";
const HVGL_STATIC_ASSETS = [
  "/static/style.css",
  "/static/chat/chat.css",
  "/static/chat/chat.js",
  "/static/meetings/meeting.js",
  "/static/images/logo.png",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/maskable-192.png",
  "/static/icons/maskable-512.png",
  "/static/icons/apple-touch-icon-180.png",
  "/static/icons/favicon-32.png",
  "/static/icons/favicon-16.png",
  "/static/manifest.webmanifest"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(HVGL_CACHE_NAME).then(function (cache) {
      return cache.addAll(HVGL_STATIC_ASSETS).catch(function () {
        return Promise.resolve();
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys
          .filter(function (key) {
            return key !== HVGL_CACHE_NAME && key.indexOf("hvgl-ksnb-") === 0;
          })
          .map(function (key) {
            return caches.delete(key);
          })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", function (event) {
  const request = event.request;
  const url = new URL(request.url);

  if (request.method !== "GET") {
    return;
  }

  if (url.origin !== self.location.origin) {
    return;
  }

  if (!url.pathname.startsWith("/static/")) {
    return;
  }

  event.respondWith(
    caches.match(request).then(function (cached) {
      if (cached) {
        return cached;
      }

      return fetch(request).then(function (response) {
        if (!response || !response.ok) {
          return response;
        }

        const copy = response.clone();
        caches.open(HVGL_CACHE_NAME).then(function (cache) {
          cache.put(request, copy);
        });

        return response;
      });
    })
  );
});