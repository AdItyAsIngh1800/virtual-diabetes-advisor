/**
 * Web Push (R3, R4): subscribe this browser with the server's VAPID key and register
 * it with POST /api/push/subscribe, or revoke it. Ported from the classic common.js.
 */
import { api } from "@/shared/api/client";
import { getSupabase } from "@/shared/auth/supabase";

/** Decode a base64url VAPID key into the bytes pushManager.subscribe expects. */
export function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

/** True when two key buffers hold the same bytes. */
export function sameKey(a: ArrayBuffer | null | undefined, b: Uint8Array): boolean {
  if (!a || a.byteLength !== b.byteLength) return false;
  const x = new Uint8Array(a);
  return x.every((v, i) => v === b[i]);
}

/** Whether push can work here at all (iPhone: only from the Home Screen app). */
export function pushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

/** True on an iPhone or iPad browser tab (push needs "Add to Home Screen" first). */
export function needsHomeScreen(): boolean {
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent);
  const standalone = window.matchMedia?.("(display-mode: standalone)").matches ?? false;
  return ios && !standalone;
}

/** Is this browser subscribed right now? */
export async function pushEnabled(): Promise<boolean> {
  if (!pushSupported()) return false;
  const registration = await navigator.serviceWorker.getRegistration("/app/");
  if (!registration) return false;
  return (await registration.pushManager.getSubscription()) !== null;
}

/** Ask for permission, subscribe this browser and register it with the API. */
export async function enableAlerts(): Promise<void> {
  if (!pushSupported()) {
    throw new Error("This browser cannot receive push alerts here. On iPhone, add the app to the Home Screen first.");
  }
  const { config } = await getSupabase();
  if (!config.vapid_public_key) {
    throw new Error("The server has no VAPID key yet (run scripts/gen_vapid_keys.py).");
  }
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Notifications are blocked. Allow them in the browser's site settings.");
  }
  const registration = await navigator.serviceWorker.ready;
  const key = base64UrlToBytes(config.vapid_public_key);
  let sub = await registration.pushManager.getSubscription();
  // A subscription made with an older VAPID key cannot be reused: replace it.
  if (sub && !sameKey(sub.options.applicationServerKey, key)) {
    await sub.unsubscribe();
    sub = null;
  }
  sub = sub ?? (await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key }));
  await api("/api/push/subscribe", "POST", sub.toJSON());
}

/** Revoke this browser's subscription on the server and in the browser. False if none. */
export async function disableAlerts(): Promise<boolean> {
  if (!pushSupported()) return false;
  const registration = await navigator.serviceWorker.ready;
  const sub = await registration.pushManager.getSubscription();
  if (!sub) return false;
  await api("/api/push/subscribe", "DELETE", { endpoint: sub.endpoint });
  await sub.unsubscribe();
  return true;
}
