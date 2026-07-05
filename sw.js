// まちの冒険手帳 - シンプルなサービスワーカー
// アプリの外枠(HTML/CSS/JS)をキャッシュし、オフラインでも起動できるようにします。
// イベントデータ自体はオンライン時に読み込む想定です。

const CACHE_NAME = "machi-bouken-cache-v1";
const APP_SHELL = [
  "./machi-no-bouken-techou.html",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // アプリの外枠はキャッシュ優先、それ以外(天気APIやFirestoreなど)は通常通りネットワークへ
  const isAppShell = APP_SHELL.some((path) => event.request.url.endsWith(path.replace("./", "")));
  if (isAppShell) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
  }
});
