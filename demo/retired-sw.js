// Retires a service worker that is no longer used.
//
// Publish this file at the URL of the old worker's script. Browsers that
// still have the old worker fetch it when they next check for updates (on a
// visit within its scope), install it in place of the old one, and it then
// unregisters itself. It handles no requests.
//
// The old demo site (d-v-b/vzip-demo, see demo/pages_moved.sh) serves it at
// each place that site served a worker: /vzip-demo/vzip-sw.js (its first
// deploy put the TIFF-to-Zarr demo, and its worker, at the root),
// tiff-to-zarr/vzip-sw.js and image-to-zarr/vzip-sw.js, the demo's later homes
// before the site moved to d-v-b/refkv-demo.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil(self.registration.unregister());
});
