/* Service worker da Câmera Topográfica.
 *
 * VERSION é monotônica — BUMPE em QUALQUER mudança nos arquivos servidos
 * (index.html, vendor, manifest, ícones), senão usuários ficam com o shell
 * antigo em cache (convenção da casa, ver CLAUDE.md do workspace).
 *
 * Estratégia:
 * - Shell (index, vendor, manifest, ícones): stale-while-revalidate — abre
 *   instantâneo (e offline), atualiza por baixo; a próxima visita pega o novo.
 * - Tiles (/{z}/{x}/{y}.png, /ee/…), /stats e hosts externos (OSM, Esri,
 *   Nominatim, EE): SÓ rede — têm cache HTTP próprio (Cache-Control de 7 dias
 *   + v= de cache-buster) e são grandes demais pra duplicar no CacheStorage.
 */
"use strict";

const VERSION = "8";
const CACHE = `cameratopo-v${VERSION}`;

const SHELL = [
  "./",
  "manifest.json",
  "icon-192.png",
  "icon-512.png",
  "vendor/leaflet/leaflet.css",
  "vendor/leaflet/leaflet.js",
  "vendor/leaflet-rotate/leaflet-rotate.js",
  "vendor/fonts/ibm-plex-mono-400.woff2",
  "vendor/fonts/ibm-plex-mono-500.woff2",
  "vendor/fonts/ibm-plex-mono-600.woff2",
  "vendor/fonts/ibm-plex-mono-700.woff2",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
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
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;               // externos: só rede
  if (/^\/(\d+\/\d+\/\d+\.png|ee\/|osm\/|stats)/.test(url.pathname)) return;  // tiles/stats: só rede

  // Shell: stale-while-revalidate (navegações caem no "./")
  const key = req.mode === "navigate" ? "./" : req;
  e.respondWith(
    caches.open(CACHE).then(async (c) => {
      const cached = await c.match(key);
      const refresh = fetch(req)
        .then((res) => { if (res.ok) c.put(key, res.clone()); return res; })
        .catch(() => cached);
      return cached || refresh;
    })
  );
});
