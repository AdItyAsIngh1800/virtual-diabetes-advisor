---
name: web-push-voice-advisory
description: Use when sending advisory notifications (R3), generating or playing voice advisories (TTS), handling the in-app text/voice chat transport (R4), push subscriptions, service workers, or Supabase Realtime alerts. The project does NOT use WhatsApp.
---

# Web Push voice advisory (R3) and in-app voice I/O (R4)

WhatsApp was removed: Meta requires a registered, verified business, which this student project is not.
Notifications are **Web Push** (works with the tab closed) plus **Supabase Realtime** (instant in-app alert
while the app is open). Voice is delivered inside the web app.

## Web Push

- Server: `pywebpush` with VAPID keys from settings (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`,
  `VAPID_SUBJECT=mailto:...`). Never log keys or full subscription endpoints.
- Subscriptions live in `push_subscriptions` (user_id, endpoint, p256dh, auth, user_agent, created_at,
  revoked_at). A push answered with 404/410 means the subscription is dead: set revoked_at.
- Payload is small JSON: `{advisory_id, title, body, url}`. No health details beyond the reading and
  the user's limit; never names, phone numbers or other users' data.
- Set `TTL` (default 3600 s): an hour-old glucose alert is no longer useful. Urgency `high` for
  breach and below-54 advisories, `normal` for pre-breach.
- Browser: `sw.js` service worker handles `push` (showNotification with tag = advisory_id so repeats
  replace) and `notificationclick` (focus or open `/advisory/{id}`).
- Requires https: test on `localhost` or the ngrok https URL. iPhone only after "Add to Home Screen"
  (iOS 16.4+), so ship a `manifest.webmanifest`.

## Voice advisory

1. Text from a **fixed template** (never free LLM text for alerts): reading, the user's personal limit,
   direction, one safe action, "contact your doctor if ...". Under ~25 s spoken. Breach and below-54 add
   the practitioner/emergency line. No dosing, no diagnosis.
2. TTS behind a `SpeechSynthesizer` Protocol: gTTS -> mp3 (browsers play mp3 everywhere), stored in a
   private Supabase Storage bucket and served by a short-lived signed URL from `GET /api/advisories/{id}`.
   Browser `speechSynthesis` (en-IN / hi-IN / ta-IN voices) is a fallback when the file is missing.
3. Autoplay is blocked until the user interacts: the advisory page shows a large Play button.

## Dispatch rules

- Only notify for advisories whose reading was measured within `ADVISORY_MAX_AGE_MINUTES` (default 60);
  older queued ones become `expired` and are never sent.
- Delivery status on the advisory row: queued -> sent -> opened (notificationclick / page view) | failed | expired.
- Retry transient push errors (5xx, 429) with backoff, max 3 attempts. Respect the cooldown, except
  below-54 readings.
- Recipient guard: demo users are only notified if they are the demo patient linked to the developer's
  own login/subscription.

## In-app chat (R4 transport)

- `POST /api/chat` accepts text or an audio upload (MediaRecorder, webm/ogg). Audio is transcribed with
  faster-whisper (small, int8, CPU, loaded lazily once), then passed to the chat service.
- Replies return text and, if the user spoke, an audio URL of the spoken reply.
- Every turn goes to `conversation_turns` (append-only) with channel = 'web'.

## Safety

- Advisory and chat endpoints only return the logged-in patient's own data (Supabase auth + RLS).
- Practitioners see assigned patients only. Practitioner connect creates a `handoffs` row and notifies the
  practitioner by Web Push / Realtime.
