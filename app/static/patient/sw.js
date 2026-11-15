// Service worker for the patient app (scope /app/): shows Web Push alerts (R3).
"use strict";

const FALLBACK_URL = "/app/";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

/** Only ever open pages on this site, whatever the payload says. */
function sameOriginUrl(raw) {
  try {
    const url = new URL(raw || FALLBACK_URL, self.location.origin);
    return url.origin === self.location.origin ? url.href : new URL(FALLBACK_URL, self.location.origin).href;
  } catch {
    return new URL(FALLBACK_URL, self.location.origin).href;
  }
}

// Payload: {advisory_id, title, body, url} (app/services/notifications.py).
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  const title = data.title || "Diabetes Advisor";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || "",
      // Same tag = a repeat of the same advisory replaces the earlier notification.
      tag: data.advisory_id || "diabetes-advisor",
      renotify: true,
      icon: "/app/icon-192.png",
      badge: "/app/icon-192.png",
      data: { url: sameOriginUrl(data.url) },
    }),
  );
});

// Focus an open app tab (navigating it to the advisory) or open a new one.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = sameOriginUrl(event.notification.data && event.notification.data.url);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (client.url.startsWith(`${self.location.origin}/app`)) {
        await client.focus();
        return client.navigate ? client.navigate(target) : undefined;
      }
    }
    return self.clients.openWindow(target);
  })());
});
