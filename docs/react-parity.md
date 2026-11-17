# React rebuild: feature parity with the classic pages

The React apps (`web/`, built to `app/static/web/`) replace the classic pages in
`app/static/patient/`. The classic pages stay reachable at `/app/classic/` and
`/app/practitioner-classic` until parity is confirmed on a real phone.

Legend: [x] at parity and checked in a browser (local instance, see "How this was
checked"); [~] at parity in code and tests, but only a real browser or phone can
prove it.

## Patient app (`/app/`)

- [x] Sign-in (email and password, optional account creation); practitioners are
  sent to `/app/practitioner`
- [x] Chat with text; quick-reply chips (fasting reading, after-meal reading, Talk
  to a doctor)
- [~] Hold-to-record voice: MediaRecorder, 60 s cap, slide away to cancel, timer,
  upload, transcript shown (needs a real microphone)
- [~] Voice-reply playback with the "Tap ▶ Play to hear the reply." fallback when
  autoplay is blocked; the Play button comes from each message's `audio_url`, so it
  survives a reload
- [x] Day separators in local (India) time (`format.test.ts` runs in Asia/Kolkata)
- [x] KB answers show the "Source:" line with the not-yet-reviewed notice (split
  off, never dropped; spoken text drops the Source line, the backend adds the
  spoken notice)
- [x] Red-flag / emergency replies: a red alert card with a tap-to-call 112 button;
  text exactly as sent
- [x] Reading replies shown exactly as sent
- [x] "Talk to a doctor" behind an AlertDialog confirm; handoff banners (waiting /
  "<name> is in this chat"); "has joined" arrives as the server's system message
- [x] Advisory page `/app/advisory/:id`: reading, limit, fixed advice text, large
  Play button (mp3, or the browser voice), status; old `/app/?advisory=<id>` links
  redirect
- [~] Push alerts: VAPID subscribe / unsubscribe, iPhone "Add to Home Screen" hint
  (needs HTTPS and a real device)
- [~] Realtime in-app advisory banner (`advisories:<uid>`; needs a real Supabase
  session)
- [x] "Not for emergencies… call 112" line always visible, with tap-to-call
- [x] New: My limits card (fasting / after-meal / low, plain words, no risk score),
  Readings and Alerts pages (`GET /api/me/health`, read-only)
- [x] Never shows the risk score or tier

## Practitioner app (`/app/practitioner`)

- [x] Overview: KPI tiles (every rate as "x of y"), last batch run, delivery table
- [x] Patients: filters and sort (remembered), table on desktop, cards on phones
- [x] Patient detail page with Readings, Limits, Alerts and Conversation tabs (tab
  in the URL, last tab remembered)
- [x] Glucose chart (Recharts): step limit lines, hatched 5 % pre-breach band,
  ringed breaches, dashed "before first limit" stretch, legend, readings table
- [x] "Why is this the limit?": base + age/health + survey risk (always 0) + trend,
  allowed range, citation, last change
- [x] Risk tier always labelled "Survey-based diabetes risk (information only, does
  not change limits)" with a link to the model card
- [x] Alerts: filters, Load more, deep links (`/alerts?state=breach_high`)
- [x] Handoffs: list and thread, claim / resolve behind AlertDialog confirms, reply;
  20 s poll
- [~] Handoffs Realtime (`handoffs:<uid>`) and push (needs a real Supabase session)
- [x] Review: filters, verdict buttons, note required unless Correct, current
  verdict and review count kept
- [x] Knowledge base: drafts (priority first, "Marked unsafe by a reviewer"),
  staff-only hint, edit / save / approve (needs body and citation) / reject,
  approved entries with Edit, the seed-account line
- [x] Audit: summary and per-KB-version tables, KB status, model card (precision,
  recall and F1 before ROC-AUC, 2x2 confusion matrix, the share of flagged people
  without diabetes, full limitations list, features, last retrain)
- [x] First names only; no e-mail, phone number or push endpoint is shown
- [x] New: collapsible sidebar, phone tab bar, alerts bell, Cmd/Ctrl+K palette, "?"
  shortcuts, "last updated" with auto-refresh, offline banner

## How this was checked

- `npm run typecheck`, `npm run lint`, `npm test` (vitest: chat and recording
  state, day separators, band maths at every boundary, step lines, scaling, sorting,
  URLs, review and KB rules), `npm run build`.
- `uv run ruff check .`, `uv run mypy .`, `RUN_DB_TESTS=1 uv run pytest`
  (including `tests/test_web_app_serving.py` and `tests/test_patient_health_api.py`).
- `npm run test:a11y`: axe on every route of both apps, light and dark, at 1280 px
  and 390 px; no serious or critical violations.
- Screenshots with agent-browser of every page at 1280 px and 390 px, light and
  dark, against a local instance on 127.0.0.1 with the auth dependency overridden
  and the scheduler off (no demo passwords were available to sign in for real).
  Write actions (claim, reply, resolve, verdicts, approve, reject) were not
  exercised against the live database.
