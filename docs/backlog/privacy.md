# Privacy: PII in captured text

Person names were added to the sensitive-data filter on 2026-10-08 (`privacy/person_ner.py`, type `person`). Choices made with the user:

- Method: a small local ONNX NER model, not rules and not spaCy. Up to 0.5 GB RAM was OK. Used: `Xenova/distilbert-base-multilingual-cased-ner-hrl` int8 (afl-3.0), pinned revision and sha256. `Babelscape/wikineural-multilingual-ner` handles Russian better, but its license is non-commercial. `openai/privacy-filter` (1.4B parameters) is too big.
- Marker: `[REDACTED:person]`, like the other types.
- Postal addresses: skipped. Only 7 of 1,593 snapshot frames had street-like text.

The model ships inside the app (user, 2026-10-12). A dev checkout downloads it once.

Measured on the EDA snapshot (all 1,593 frames, Mac, 1 thread, 2026-10-12): about +120 MB footprint at load, +165-200 MB in use (levels off), CPU 27-46 ms per frame, p90 50-115 ms, max 0.65 s per call (capped at about 1 s). The tokenizer is plain Python (`privacy/wordpiece.py`), so there is no new dependency. On a dev instance with real screens (Chrome meeting notes, Slack), names were replaced and the lines kept their meaning. On a synthetic set of 20 name lines and 21 non-name lines (`tests/test_person_ner.py`): recall 1.00, precision 1.00.

## Items

### Terminal a11y text is the whole scrollback
Status: open

Activity 327 (Terminal) stored 253,001 characters of a11y text, 2,131 lines: the whole scrollback, not the visible screen. That is mostly not on screen, it makes the DB and the export bigger, and without the per-call cap the name model needed 27 s for it. Keep only the visible part, or cap a11y text per frame.

Refs: `MacOSAdapter` a11y text, `analysis_worker._process()` step 3a.

### Rows stored before the name filter keep names
Status: open, decide with the user

Rows from before this change still hold names in `ocr_text`, `ocr_boxes`, `organized_text`, `window_title`, `ui_events` and `meetings`. The export removes them (it runs the filter again). Retention deletes them over time. A one-off backfill of the main DB is possible but must not run from a dev session.

### Names the filter misses or adds
Status: idea

- A first name alone that is also a common word stays ("Mark", "Will", "Anna"). Full names are caught.
- Product names the model tags as people are kept by a stoplist (`person_ner._NOT_NAMES`). Unknown products ("Truto", 126 lines in the snapshot), project names ("Distillery") and OCR typos ("Outlcok", "Ladded") are redacted as names. Idea: a user keep-list setting, or learn product names from window titles and URLs.
- Lines over the per-call cap get only names already found. The model checks them on a later frame, but the row of this frame keeps that weaker result.

### Same placeholder for every person
Status: idea

Every name becomes `[REDACTED:person]`, so workflow extraction can't tell that two frames talk to the same colleague. A local, salted hash per name (`[REDACTED:person:3f2a]`) would keep that without the name. The salt must stay on the device. A name list could reverse unsalted hashes.

### Measure the filter on a labelled PII dataset
Status: blocked on the dataset

The only accuracy numbers are a small synthetic set (41 lines) and a look at real screens. Russian is the least proven: the model is not trained on it. A labelled set is being built by the "PII data processing P0" story. It shares our label names (`person`, `email`, `phone`, `iban`, `credit_card`, `ssn`, `api_key`, `jwt`, `password`, plus labels we don't detect) and a JSONL format: `id`, `lang`, `source`, `tool`, `text`, `spans` (code-point offsets, end exclusive), `negatives`. v1 covers text attachments, EN/RU (2026-10-12). Its OCR/a11y slice is parked. It has 27 labels; the full list is in `services/workflows/evals/pii_dataset/scenarios.json` (worktree `angry-raman-1e0189`). `person` spans can carry `"public": true` (public figures), so score with and without them. When a file exists, report precision and recall per label and per language, and the latency.

Refs: `data_filter.filter_sensitive_text()`, `tests/test_person_ner.py` (`POSITIVE`, `NEGATIVE`).
