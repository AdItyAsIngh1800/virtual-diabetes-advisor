---
name: whatsapp-voice-advisory
description: Use when sending WhatsApp messages or voice notes, generating advisory speech (TTS), handling WhatsApp Cloud API webhooks, or transcribing incoming WhatsApp voice notes (STT). Covers R3 delivery and the transport side of R4.
---

# WhatsApp voice advisory (R3) and voice I/O (R4) - Meta WhatsApp Cloud API

No Twilio. Call Meta's Graph API directly with `httpx` from `app/services/whatsapp.py`.

## Setup facts

- Base URL: `https://graph.facebook.com/v{GRAPH_API_VERSION}` (version in config, not inline).
- Auth header: `Authorization: Bearer {WHATSAPP_ACCESS_TOKEN}`.
- Env vars (via `app/core/config.py` only): `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
  `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`, `GRAPH_API_VERSION`.
- Free **test number** can message at most **5 verified recipients** - demo users must be added in the Meta app.
- The Getting Started token expires in 24h; demo uses a permanent **System User** token.

## 24-hour customer service window - design around it

- Free-form messages (text, audio) are allowed only within 24h of the user's last inbound message.
- Outside the window only an **approved template** can be sent. Templates cannot carry audio.
- So R3 delivery logic:
  1. If window open -> send text advisory + voice note.
  2. If window closed -> send the approved utility template (`glucose_advisory`, text variables:
     name, reading, threshold) with a quick-reply button "Hear advisory". When the user taps it,
     the window opens -> send the voice note.
- Track `last_inbound_at` per user to decide. Demo tip: have each user message the bot daily.

## Sending a voice advisory

1. Advisory text from a **fixed template** (never free LLM text for alerts): reading, threshold,
   direction, one safe action, "contact your doctor if ...". Under ~25 s spoken. Languages: en, ta, hi (gTTS `lang`).
2. TTS -> mp3 (gTTS) -> convert to **OGG/Opus** so it plays as a voice note:
   `ffmpeg -y -i in.mp3 -c:a libopus -b:a 32k out.ogg` (run via `subprocess`, check return code).
3. Upload: `POST /{PHONE_NUMBER_ID}/media` multipart, fields `messaging_product=whatsapp`,
   `type=audio/ogg`, `file=...` -> returns `{"id": media_id}`.
4. Send: `POST /{PHONE_NUMBER_ID}/messages` JSON
   `{"messaging_product":"whatsapp","to":"<E.164 without +>","type":"audio","audio":{"id":media_id}}`.
   Send a text message alongside so the alert is readable if audio fails.
5. Store returned `messages[0].id` (wamid) with the advisory id. Status updates arrive on the webhook
   (`sent -> delivered -> read | failed`); update the advisory row.
6. Retry 5xx / rate-limit errors with backoff (max 3); mark failed and surface on the dashboard.
   Parse Graph errors (`error.code`, `error.message`) into `WhatsAppDeliveryError`.

## Webhook (`app/api/webhooks.py`)

- `GET /webhooks/whatsapp`: verification - if `hub.mode == "subscribe"` and `hub.verify_token`
  matches config, return `hub.challenge` as plain text; else 403.
- `POST /webhooks/whatsapp`: **verify `X-Hub-Signature-256`** = `sha256=` + HMAC-SHA256(app_secret, raw body),
  compare with `hmac.compare_digest`. Reject on mismatch. Read the raw body before JSON parsing.
- Parse `entry[].changes[].value` with Pydantic: `messages[]` (inbound) and `statuses[]` (delivery).
- Inbound text -> chat service. Inbound audio -> `GET /{media_id}` to get a URL -> download it with the
  Bearer token -> transcribe with faster-whisper (`small`, CPU int8) -> chat service.
- Return 200 immediately; do slow work in a background task. Meta retries on failure, so
  de-duplicate by message id.
- Dev: expose with ngrok and set the callback URL + verify token in the Meta app dashboard.

## Safety rules for message content

- No medication dosing advice, ever. No diagnosis. Describe the reading, suggest general safe actions,
  and point to a practitioner.
- Low-glucose guidance only if a cited clinical source exists in `clinical_limits.py`; otherwise
  "contact your doctor / emergency services".
- Phone numbers are PII: store E.164, never log full numbers (mask middle digits).
