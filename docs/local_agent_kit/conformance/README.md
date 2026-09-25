# Conformance set

Manifests with the findings every validator of this contract must report for
them. `kit.py` and Cinna Desktop's validator both run the whole set in their
tests, so the two cannot disagree about a manifest without a test failing. Part
of the contract since 1.5.0, and shipped in the contract bundle.

The set covers **manifest-level checks only**: each case is a `cinna-agent.json`
document on its own, with no agent folder around it. Checks that need a folder —
the slug matching the folder name, a handover target existing next to it, prompt
files, secrets — are out of scope here.

## File format

`manifests/<case>.json`, one case per file:

```json
{
  "description": "What the case pins, in one sentence.",
  "manifest": { "contract_version": "1.5.0", "id": "…", "name": "…", "slug": "…", "description": "…" },
  "expect": {
    "errors": ["runtime.engine"],
    "warnings": ["runtime.complexity"],
    "no_warnings": ["handovers.target_kind"]
  }
}
```

Every key of `expect` is optional; an absent key checks nothing.

## Matching rule

A validator reports findings with a severity and a message. Each finding names
the field it is about as a **field path**:

- dotted, from the manifest root: `runtime.engine`, `handovers.target_kind`;
- **array indices dropped**: a finding about `handovers[0].target_kind` has the
  path `handovers.target_kind`.

`kit.py` writes that path in backticks in the message (`` `handovers[0].target_kind` ``),
and its test takes every backticked token shaped like a field path as a path the
finding reports. Another host may derive paths however it likes — from a finding
code, for instance — as long as it applies the rule below to the same paths.

Given the set of error paths **E** and warning paths **W** a validator reported
for `manifest`:

| Key | Passes when |
|-----|-------------|
| `errors` | **E** equals this set exactly. `[]` means no error at all. |
| `warnings` | every path listed is in **W**. Other warnings may appear. |
| `no_warnings` | no path listed is in **W**. |

Informational findings are never matched.

`errors` is exact because an error decides whether a host will operate the
folder at all, and two hosts must agree on that. Warnings are matched loosely
because hosts legitimately warn about different things — a host may add a
warning of its own, but may not drop one the contract pins, nor warn where a
case says it must not.

## Adding a case

One behaviour per case, named for it (`runtime-engine-unknown.json`). Start
from `minimal-valid.json`, which reports nothing, so that the only finding is
the one the case is about. A new validation rule in the contract lands with at
least one case that fires it and, where it has a boundary, one that does not.
