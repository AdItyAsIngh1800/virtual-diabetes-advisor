"""Scheduled and on-demand jobs.

* :mod:`app.jobs.batch_update` - R1 batch update: risk scores, thresholds, advisories
  (scheduled, ``POST /admin/batch-run``).
* :mod:`app.jobs.dispatch_advisories` - R3 delivery of queued advisories (scheduled,
  ``POST /admin/dispatch-run``).
* :mod:`app.jobs.kb_upgrade` - R5 weekly knowledge-base drafts for practitioners
  (scheduled, ``POST /admin/kb-upgrade-run``).
* :mod:`app.jobs.retrain` - R5 risk-model retraining with a promotion guard. Manual
  only (``python -m app.jobs.retrain``, ``POST /admin/retrain-run``); never touches
  thresholds.
"""
