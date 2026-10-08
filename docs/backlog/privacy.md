# Privacy: PII in captured text

Person names were added to the sensitive-data filter on 2026-10-08 (`privacy/person_ner.py`, type `person`). Choices made with the user:

- Method: a small local ONNX NER model, not rules and not spaCy. Up to 0.5 GB RAM was OK. Used: `Xenova/distilbert-base-multilingual-cased-ner-hrl` int8 (afl-3.0), pinned revision and sha256. `Babelscape/wikineural-multilingual-ner` handles Russian better, but its license is non-commercial. `openai/privacy-filter` (1.4B parameters) is too big.
- Marker: `[REDACTED:person]`, like the other types.
- Postal addresses: skipped. Only 7 of 1,593 snapshot frames had street-like text.

Measured on the EDA snapshot (400 frames, Mac, 1 thread): about +120 MB footprint, CPU mean 33 ms per frame, median 32 ms, p90 65 ms, max 0.8 s per call (capped at about 1 s). On a dev instance with real screens (Chrome meeting notes, Slack), names were replaced and the lines kept their meaning. On a synthetic set of 20 name lines and 21 non-name lines (`tests/test_person_ner.py`): recall 1.00, precision 1.00.

## Items

### Gemma output is stored without the filter
Status: open, closed by the Gemma removal

Gemma reads the raw screenshot, so `summary`, `details`, `scene_description` and `visible_text` can repeat what the filter removed from the text. Of 1,593 snapshot frames, 14 had an email there. The branch "Prepare removal of Gemma processing" drops these fields. If any LLM output is still stored, run `filter_sensitive()` on it.

Refs: `analysis_worker._process()`, `Database.update_activity_analysis()`.

### The filter blocks the analysis event loop
Status: fixed on branch `claude/practical-dijkstra-f83cbe` (`b5a8da5`, `analysis_worker._redact()`), not merged yet

`analysis_worker._process()` calls `filter_sensitive()` and `filter_ocr_boxes()` directly in the coroutine. With names on, a call takes up to about 1 s on a screen full of new text, and the loop waits. Run both in the executor, like OCR. Wait for the Gemma removal rewrite of `analysis_worker.py`, so the two changes don't conflict.

Refs: `analysis_worker._process()` step 3c, `person_ner._MAX_WINDOWS_PER_CALL`.

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
