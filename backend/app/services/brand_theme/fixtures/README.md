# Brand theme shared fixtures

Test data for the `cinna.brand-theme/1` contract. Core's tests run the validator
and the SVG sanitiser against it (`backend/tests/unit/test_brand_theme_fixtures.py`);
Cinna Desktop copies this folder so its parser and safety checks are tested
against what core actually produces. Do not edit by hand: change the code, then
regenerate (below).

## Layout

```
valid/<case>/
  theme.json                 a package core accepts
  assets/...                 the files theme.json references (omitted if none)
  served/served.json         what GET /api/v1/brand-theme returns for it
  served/assets/<sha256>     only the bytes core rewrote (sanitised SVGs); the
                             name is the sha256 that served.json carries

invalid/<rule>/
  theme.json, assets/...     a package core refuses
  expect.json                {"tag": "unsafe" | "incompatible",
                              "errors": [{"path": "...", "code": "..."}]}
  served/served.json         what a server that skipped validation would send
                             (raw bytes; a missing asset gets an all-zero sha256)

svg/<case>/
  input.svg                  an upload
  expect.json                {"core": "sanitised", "must_not_contain": [...],
                              "must_contain": [...]}   or
                             {"core": "refused", "code": "<error code>"}
  sanitised.svg              golden output (only when core: "sanitised")
```

## Tags

- `unsafe`: Cinna Desktop must also reject the package (syntax, sizes, allowed
  content), because the server is untrusted input.
- `incompatible`: only core refuses it (contrast, readability, distinctness and
  similar); the desktop does not re-judge it.

An `invalid/` case lists every error core reports, as `path` and `code`; messages
are not part of the contract. The `served/served.json` revision is a fixed
number, not a real one.

For SVG cases the desktop re-applies the same allowlists as a check: it rejects
any input where core removes something, instead of repairing it.

## Regenerating

After an intended change in core:

```
BRAND_THEME_REGEN_FIXTURES=1 python -m pytest tests/unit/test_brand_theme_fixtures.py
```

(run inside the backend container), review the diff, then re-copy this folder
into Cinna Desktop.
