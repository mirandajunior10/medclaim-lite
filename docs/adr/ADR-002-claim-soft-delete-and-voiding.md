# ADR-002: Claim soft-delete and the tri-state `is_deleted` flag

**Status:** Accepted
**Date:** 2022

## Context

Claims are never hard-deleted (audit requirement). Era 1 had no delete concept
at all; the `is_deleted` column was added in era 2. That means three states
exist in the wild:

- `is_deleted = 1` — voided, hide from statements.
- `is_deleted = 0` — explicitly active.
- `is_deleted IS NULL` — an era-1 row written before the column existed. These
  are legitimate, active claims and **must still appear** on statements.

An earlier version of the statement-history query filtered `WHERE is_deleted = 0`
and silently dropped every era-1 (NULL) claim from statements. That was a real
incident. The fix was to filter **`WHERE is_deleted IS NOT 1`** instead, so NULL
and 0 rows are both shown and only explicitly-voided rows are hidden.

## Decision

- Statement and history queries filter `is_deleted IS NOT 1`.
- The **modern** void path (`claims.void_claim`) sets `is_deleted = 1` and writes
  a `void_log` row.
- Voids are always recorded in `void_log` regardless of path.

## Consequences

Correctly voided claims disappear from statements. The correctness of this
depends on the void path actually setting the flag — see the void code in
`medclaim/claims/`.

## Amendment (T-103)

The legacy path (`claims.void_claim_legacy`) predated the flag and wrote only
`void_log`, so its voids stayed on statements. Both paths now go through one
helper that sets `is_deleted = 1` and writes `void_log` in the same
transaction. Claims voided the legacy way before the fix are repaired by
`claims.backfill_legacy_voids()` (`make backfill-voids`), which flags every
claim listed in `void_log`; it is idempotent. The legacy audit line no longer
logs patient name or MRN.
