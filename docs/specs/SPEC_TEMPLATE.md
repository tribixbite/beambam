# <Feature> — Specification

**Status**: Planning | In progress | **Complete** | Deprecated
**Area**: daemon | slice | web UI | print | HA | …
**Primary source**: `path/to/main_module.py` (+ others)
**Tests**: `tests/test_*.py`, `site/src/lib/.../ *.test.ts`

## Summary

One or two sentences: what this does and for whom.

## Motivation

The problem it solves; why it exists.

## How it works

The real mechanism, end to end. Name the functions/files at each step. A short data-flow block helps:

```
input ──(function/file)──▶ intermediate ──(…)──▶ output
```

## Key types & functions

- `module.function(args) -> ret` — what it does, the contract, gotchas.
- `Type` — the fields that matter and what they mean.

## Invariants & gotchas

Numbered list of the things that must stay true, and the traps a maintainer will hit. Each with the
failure mode ("if X is wrong, the symptom is Y").

## Verification

How to prove a change is correct (the exact commands / a real end-to-end check, not only unit tests).

## Known limitations / future work

What is deliberately out of scope, and the follow-ups.

## History

Dated notes for non-obvious decisions and the bugs that shaped this (keep terse).
