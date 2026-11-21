# T-26 Controlled acceptance report — UI with the live local model

- **Date:** 2026-11-09
- **Model:** `hf.co/unsloth/gemma-4-12b-it-GGUF:Q4_K_M`, provider `local`
  (Ollama-compatible endpoint `http://localhost:11434/v1` from `.env`; the
  model responded to a connectivity check before the run).
- **Operator and harness:** the acceptance driver acted as the test operator
  through the real browser UI (Playwright, headless Chromium) against the real
  application started in-process (`create_app` + uvicorn on a free 127.0.0.1
  port), with the **live** `OpenRouterClient` — no scripted model, no mocks.
- **Data:** each scenario used its own separate copy of the anonymized
  baseline (12 records, the two duplicate Spotify charges `tx_103`/`tx_104`)
  and a fresh empty temporary ledger. The working repository dataset and
  ledger were never written by the acceptance runs.

## Scenario A — approval leads to exactly one write

- **Run:** `run_Cogywif7HLuTyJFx0NCrNw`, approval `apr_ohWFOfdwq9cbHJOj`.
- **Goal (as submitted, from the trace `agent_start` event):** "Find duplicate
  subscription charges and delete one of the duplicate Spotify charges. Once
  the duplicates are confirmed, choose one of the two yourself and submit its
  deletion request right away." (An explicit operator intent was added so the
  live model raises the deletion request instead of asking which duplicate to
  remove; the approval itself is still the human click, never the model.)
- **Timeline:** decision card shown 63.8 s after run start; operator click on
  `Confirm deletion` → decision POST observed **0.052 s** after the click,
  answered **HTTP 200**; run reached `Completed` 15.9 s after the click.
- **Tools used (trace, ≥2 with reaction to an observation):**
  1. `find_duplicate_candidates` — success, returns the duplicate pair
     `tx_103`/`tx_104`;
  2. `delete_flagged_transaction` — called **because of that observation**,
     result success. Execution happened only after the human approval; the
     trace shows exactly one `approval_raised` and one `approval_resolved`
     (`approved`) event.
- **Ledger diff:** exactly one new line:
  `action_type=delete_transaction`, `transaction_id=tx_104`,
  `action_id=act_del_20261109_171545_d0c7c7`.
- **UI ↔ ledger link.** The decision card showed `Waiting for your decision`
  for `tx_104`. After the approved deletion, the run showed `Completed` and
  both tool observations. The table replaced `tx_104` with `tx_111` and kept
  10 visible rows, matching the display limit. The ledger recorded the deletion.
- **No further pending decision existed before the click**
  (`pending_count == 1`) and no other write was performed.

## Scenario B — rejection performs no write

- **Run:** `run_CwaD8N2mbIpyMX8ZQz2zwA`, approval `apr_61Yu33BiJIeGqCIE`.
- **Goal:** "Find duplicate subscription charges and delete one of the
  duplicate Spotify charges."
- **Timeline:** decision card shown 77.8 s after run start; operator click on
  `Reject` → decision POST **0.051 s** after the click, **HTTP 200**; run
  reached `Completed` 31.2 s after the click.
- **Tools used (trace, ≥2 with reaction to an observation):**
  1. `find_duplicate_candidates` — success, duplicate pair `tx_103`/`tx_104`;
  2. `delete_flagged_transaction` — raised for `tx_104` from that observation,
     result `confirmation_denied`: "Confirmation was denied. The proposed
     action was not performed."
- **Ledger diff:** zero lines — no write at all (0 before, 0 after, 0 after
  replay).
- **UI ↔ ledger link:** the visible transaction set is identical before the
  click, after `Completed`, and after the replay (all ten IDs including
  `tx_104`); the card reads `Rejected`, observation 2 shows
  `confirmation_denied` with action `pending_del_tx_104`.

## Idempotent replay (no second event)

The exact same decision request was replayed through the page context after
each `Completed` (same endpoint, same decision, same instance id, CSRF
header):

- **Approve:** HTTP 200, stored receipt status `completed`, same
  `approval_id`; ledger still exactly 1 line (unchanged); exactly one
  `approval_resolved` event in the trace; table unchanged.
- **Reject:** HTTP 200, stored receipt status `rejected`, same `approval_id`;
  ledger still 0 lines; exactly one `approval_resolved` event; table
  unchanged.

No second write and no duplicated trace event appeared in either scenario.

## UI acceptance traces

- `evidence/trace_ui_acceptance_approve.jsonl` — run trace, scenario A
- `evidence/trace_ui_acceptance_reject.jsonl` — run trace, scenario B

## Protective verification

- `data/transactions.json` sha-256 unchanged:
  `83f372f6c4ca83e4850984f5af6d676df18ce9933842ed7f7ef1142a41129671`.
- Working ledger unchanged (all acceptance writes went to temporary ledgers).
- No unauthorized operation: the only write in the whole acceptance session is
  the single operator-approved deletion above; the rejected proposal left no
  write.
- Model availability: verified live before the run; the fallback note
  (`zablokowane`) was not needed.
