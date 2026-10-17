// MoviePilot 应用版替身：保留注册以兼容前端的 PWA 逻辑，但不做任何缓存。
//
// 前端自带的 workbox 会把同源接口响应写进 CacheStorage，于是每次冷启动第一屏
// 读到的是上一次运行留下的状态：刚注册完管理员，重启后又看到首次设置页。
// 本机的服务端就在这个应用里，永远在线，缓存换不来任何可用性，只换来过期。
self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  const emptied = typeof caches === "undefined"
    ? Promise.resolve()
    : caches.keys().then((names) => Promise.all(names.map((name) => caches.delete(name))));
  event.waitUntil(emptied.then(() => self.clients.claim()));
});
