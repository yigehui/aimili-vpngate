# Proxy Pool Priority Rotation Design

**Date:** 2026-07-28  
**Status:** Drafted from approved brainstorming  
**Project:** aimili-vpngate (AimiliVPN)

## 1. Goal

Change routine pool refresh so newly tested-good nodes enter the formal proxy pool through a **deterministic sequential rotation** instead of empty-slot refill or age-based selection.

The approved behavior is:

- after a refresh/test round finishes, only nodes that passed the current test pipeline may be used for replacement;
- replacement candidates are ordered **non-hosting first**, then **hosting** as fallback;
- the pool replaces a fixed **10% of formal slots per round**;
- replacement walks the public slot indexes **from 0 upward in sequence** and wraps at the end of the pool;
- service restart resets the rotation and the next tested round starts replacement again from slot `0`;
- active public slots must still stay online until a replacement shadow has passed startup and health validation.

This keeps the previous shadow cutover safety model while making the pool converge toward residential or non-hosting exits whenever they are available.

## 2. Non-Goals

- No full-pool destructive rebuild for routine refresh.
- No `max_slot_age_minutes` or TTL-based forced eviction.
- No random slot selection for routine refresh.
- No requirement to preserve the rotation cursor across service restarts.
- No change to list/random proxy API shapes.
- No new upstream source integrations in this change.

## 3. Current Gap

Current routine refresh behavior already does shadow replacement safely, but slot selection and candidate selection do not match the new policy:

1. tested-good nodes update the available candidate list;
2. routine rolling replacement selects READY slots by `updated_at` age;
3. candidate choice is mainly latency-ordered with duplicate avoidance;
4. there is no deterministic public-slot cursor;
5. restart does not intentionally define a fresh sequential replacement wave from slot `0`.

That means healthy slots may remain stable for a long time, and replacement order is not tied to the user's desired `0 -> 199 -> wrap` progression.

## 4. Desired Behavior

### 4.1 Refresh Round Model

A routine refresh round is defined as:

1. fetch current nodes;
2. run the existing node test pipeline;
3. build the list of **tested-good** nodes for this round;
4. derive a replacement queue from those tested-good nodes;
5. replace up to `floor(pool_size * 0.10)` formal public slots in sequential slot order.

If there are fewer eligible candidates than the round budget, only the available tested-good candidates are used.

### 4.2 Candidate Priority Policy

The replacement queue for one routine round must be built in this order:

1. tested-good nodes whose effective IP type is **non-hosting** (`residential` and `mobile` count as preferred);
2. tested-good nodes whose effective IP type is **hosting**;
3. within each tier, keep the existing quality ordering logic as much as practical so better nodes are tried first;
4. remove duplicates by `node_id`;
5. exclude nodes already active in READY/STARTING slots;
6. exclude nodes already warming as shadow replacements;
7. exclude nodes already consumed earlier in the same round;
8. exclude nodes still in skip or cooldown state.

The policy goal is:

- prefer non-hosting exits whenever the round supplies them;
- only consume hosting exits when preferred exits are exhausted.

### 4.3 Slot Rotation Policy

Routine slot selection must not use EMPTY-slot search or READY-slot age ranking.

Instead, maintain an in-memory routine refresh cursor over the formal slot indexes:

- first routine round after process start begins at slot `0`;
- each round selects the next consecutive `replace_count` public slots;
- after the highest slot index is reached, selection wraps to `0`;
- after service restart, the cursor resets and the next successful tested round starts again from `0`.

Example for `pool_size=200`:

- round 1 replaces slots `0-19`;
- round 2 replaces slots `20-39`;
- round 3 replaces slots `40-59`;
- ...
- after slots `180-199`, the next round resumes from `0-19`.

### 4.4 Replacement Safety Rule

The existing shadow replacement rule remains mandatory:

1. pick the target formal slot from the sequential cursor;
2. pick the next eligible candidate from the round queue;
3. start the candidate as a shadow replacement for that slot;
4. run the existing startup plus health validation;
5. only after success, cut over the formal slot to the new node;
6. if the candidate fails, keep the current formal slot unchanged and continue with the next eligible candidate.

Routine refresh must still avoid stopping a healthy READY listener first.

## 5. Architecture

### 5.1 Files

- `F:\officeProject\yigehui\aimili-vpngate\proxy_pool.py`
  - add routine refresh cursor state;
  - add preferred-candidate ordering helpers;
  - change routine rolling replacement to use sequential slot selection instead of `updated_at` ranking.
- `F:\officeProject\yigehui\aimili-vpngate\vpngate_manager.py`
  - keep current fetch/test pipeline;
  - continue calling the pool routine replacement entrypoint after tested-good nodes are produced;
  - no restart persistence for the cursor.
- `F:\officeProject\yigehui\aimili-vpngate\tests\test_proxy_pool.py`
  - add regression tests for sequential slot rotation and preferred IP-type fallback.
- `F:\officeProject\yigehui\aimili-vpngate\tests\test_vpngate_manager_fetch.py`
  - verify manager still feeds tested-good nodes into routine replacement without destructive rebuild.

### 5.2 Data Model Additions

Keep the existing slot and shadow structures.

Add minimal routine-refresh state to `PoolManager`:

- `refresh_cursor: int`
  - next formal slot index to consider for routine replacement;
  - initialized to `0` on manager startup;
  - not persisted.

No new public API fields are required for v1.

### 5.3 Replacement Count

Routine refresh replacement count is:

```text
replace_count = max(1, floor(pool_size * 0.10))
```

For `pool_size=200`, this is `20`.

The count is based on the configured formal pool size, not the current READY count.

## 6. Replacement Flow

### 6.1 Inputs

The routine replacement function receives the tested-good nodes from the current fetch/test round.

These nodes already reflect the current probe pipeline and any IP enrichment already performed by the manager.

### 6.2 Candidate Queue Build

For each routine refresh round:

1. dedupe the tested-good nodes;
2. classify candidates into preferred and fallback tiers using effective `ip_type`;
3. preferred tier contains `residential` and `mobile`;
4. fallback tier contains `hosting`;
5. keep existing quality ordering inside each tier where possible;
6. concatenate preferred tier first, fallback tier second;
7. skip nodes that are already active, already shadowing, skipped, or already consumed this round.

### 6.3 Target Slot Selection

For each attempted replacement in the round:

1. read `refresh_cursor`;
2. resolve the corresponding formal slot index;
3. if the slot is not currently eligible for routine replacement, advance the cursor and inspect the next slot;
4. once an eligible slot is found, reserve it for the routine replacement attempt;
5. after the attempt is scheduled, advance the cursor;
6. continue until the round budget is exhausted or no more candidates remain.

Eligibility for routine replacement remains conservative:

- slot must currently be a formal public slot;
- slot should normally be `READY`;
- slot must not already have `replacement_pending=True`;
- slot must not already own a shadow candidate;
- slot must have live process and listener handles.

If a sequential slot is temporarily ineligible, it is skipped for that round and the cursor still advances so later rounds continue the global progression.

### 6.4 Failure Handling Within a Round

If a candidate mapped to a target slot fails during shadow startup or health validation:

- the original slot stays on its current node;
- the failed candidate is cooled down using the existing skip mechanism;
- the round may continue with the next eligible candidate and next sequential slot.

The round does not backtrack to retry the same slot immediately with the same failed candidate.

## 7. Restart Behavior

The approved restart policy is:

- service restart triggers fresh node fetch and fresh node testing;
- after restart, `refresh_cursor` resets to `0`;
- the next successful routine tested round begins sequential replacement again from slot `0`.

No cursor persistence file or state restore is required.

## 8. Testing Requirements

Add or update regression tests for:

1. routine replacement prioritizes `residential` and `mobile` candidates before `hosting`;
2. routine replacement uses `hosting` only when preferred candidates are insufficient;
3. routine replacement replaces exactly `max(1, floor(pool_size * 0.10))` target slots when enough candidates exist;
4. sequential routine rounds advance slot targets by cursor order instead of `updated_at` order;
5. cursor wraps after the end of the slot list;
6. restart or new manager instance resets the cursor to `0`;
7. failed shadow candidate does not tear down the original slot;
8. manager-side fetch flow still calls routine rolling replacement with tested-good nodes only.

Tests should reuse the existing mocking style in `tests/test_proxy_pool.py` and `tests/test_vpngate_manager_fetch.py`.

## 9. Minimal Delivery Scope

The first shipping version should implement only:

- non-hosting-first candidate ordering;
- 10% routine replacement budget;
- sequential formal-slot cursor rotation;
- restart reset to slot `0`;
- regression coverage.

No UI control, no public API extension, and no additional pool policy knobs are required in this version.
