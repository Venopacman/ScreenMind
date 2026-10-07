# ScreenMind collection status: all machines

Generated from the other files in this folder by `scripts/e2e_collect.py --summary`
(also run by `--write-status`). Don't edit by hand. On a merge conflict, run
`python scripts/e2e_collect.py --summary` and commit the result.

| Data point | [macos](macos.md) | [windows](windows.md) |
|---|---|---|
| *State* | ran | ran from a Claude session |
| *Date* | 2026-10-07 18:19 | 2026-10-07 18:21 |
| *Git* | custom ea9510c | claude/win-data-quality-03db0d 49f56cd |
| Screenshots, every display | PASS | PASS |
| Screen grab backend | PASS | PASS |
| App name | PASS | PASS |
| Window title | PASS | PASS |
| Screen text from accessibility | PASS | PASS |
| OCR text | PASS | PASS |
| Browser URL (chrome) | - | PASS |
| Browser URL (firefox) | - | PASS |
| Browser URL (google chrome) | PASS | - |
| Browser URL (msedge) | - | PASS |
| UI event: click with element role/name | PASS | SKIP |
| UI event: app switch | PASS | PASS |
| UI event: typed text (opt-in) | PASS | SKIP |
| UI event: clipboard (opt-in) | PASS | PASS |
| UI events linked to frames | PASS | PASS |
| Typed secret is redacted | PASS | SKIP |
| Password field is not stored | PASS | SKIP |
| Call detection | SKIP | SKIP |
| Analysis (Gemma) | PASS | PASS |
| Retention cleanup at startup | PASS | PASS |

`-` means this machine has no result for that row (not run, or no such browser there).
