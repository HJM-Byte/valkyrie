/* 水镜的离线壳。只缓存界面；记忆和对话永远从电脑上的服务取。 */
const CACHE = "valkyrie-pocket-shell-v8";
const ASSETS = [
  "/",
  "/assets/app.css",
  "/assets/app.js",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  // 网络优先：电脑上的新版本立刻拿到；断网时才退回上一次的壳。
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && url.origin === location.origin) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/")))
  );
});
