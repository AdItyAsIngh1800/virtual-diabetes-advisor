---
name: advisor-chat-audit
description: Use when building the two-way text/voice help chat, practitioner connect/handoff (R4), conversation audit logging, or the knowledge-upgrade loop (R5).
---

# Two-way advisor, practitioner connect, audit and knowledge upgrade (R4, R5)

## Chat service (R4)

Pipeline per incoming message (text or transcribed voice):
1. **Intent + risk classify** first: `reading_report | question | symptom_red_flag | practitioner_request | other`.
   Red-flag keywords (chest pain, confusion, unconscious, very high/low reading) bypass the LLM ->
   fixed emergency template + immediate practitioner escalation.
2. `reading_report` -> parse the number, store it, run the threshold engine, reply with status.
3. `question` -> retrieve from the **approved knowledge base** (RAG over `kb_entries`), answer with an
   LLM constrained to that context, cite the KB entry id. If retrieval confidence is low, say so and
   offer practitioner connect.
4. `practitioner_request` -> create a `handoff` record (status `open -> assigned -> resolved`),
   notify the practitioner (WhatsApp/email), relay messages while the handoff is open.
5. Reply in the user's channel: text, plus voice if the user wrote in voice.

System prompt for the LLM must forbid dosing, diagnosis and anything outside the KB context.

## Audit (R5)

Every turn is written to `conversation_turns` (append-only):
`id, user_id, channel, direction, raw_text, transcript, intent, kb_ids_used, model, model_version,
response_text, latency_ms, escalated, created_at`.
- Append-only: no UPDATE/DELETE grants on this table for the app role (enforce in the migration).
- Practitioners/admins can attach a **review**: `correct | incorrect | unsafe | needs_kb_update` + note.

## Knowledge upgrade (R5) - human in the loop

- Weekly job: pull turns reviewed `incorrect`/`needs_kb_update` + low-confidence answers ->
  propose new/edited `kb_entries` as **drafts**.
- A practitioner approves drafts in the dashboard; only approved entries are embedded and served.
- Version the KB (`kb_version`); store which version answered each turn so improvements are measurable.
- Track on the dashboard: % turns reviewed correct, escalation rate, unanswered rate, per KB version.

Never let the system auto-publish knowledge it generated itself.
