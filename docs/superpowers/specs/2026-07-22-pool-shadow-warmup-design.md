# Proxy Pool Shadow Warmup Design

**Date:** 2026-07-22  
**Status:** Drafted from approved brainstorming  
**Project:** aimili-vpngate (AimiliVPN)

## 1. Goal

Change proxy-pool replacement behavior so a READY slot is **not stopped first** when it becomes unhealthy.  
Instead:

- the current READY slot keeps serving traffic for a limited grace window;
- a replacement candidate starts in the background;
- the candidate reuses the **existing pool start and health-check logic**;
- only after the candidate passes startup and current health validation does it take over the slot;
- if the candidate fails, the old slot keeps serving until the grace window expires or the old slot fully dies.

This design targets a **single high-spec VPS** running **100 formal public slots** with a priority on **fast refill** and **reduced false replacement churn**.

## 2. Non-Goals

- No node quality tiering.
- No multi-host scheduler or remote controller changes.
- No change to public API shape for `/api/pool/proxies` or `/api/pool/proxies/random`.
- No requirement to split the pool into premium/standard lanes.
- No change to the existing node-source probing pipeline in `vpngate_manager.py`.

## 3. Current Gap

Current pool behavior already pre-filters candidate nodes upstream, but formal slot replacement still behaves like:

1. active READY slot becomes unhealthy;
2. pool stops listener/process for that slot;
3. slot becomes empty;
4. fill worker starts a fresh node;
5. new slot later becomes READY and then receives health verification.

This means the slot can go dark before a replacement is known-good.  
At 100-slot steady-state, that increases visible churn and wastes usable-but-flaky nodes.

## 4. Desired Behavior

### 4.1 Formal Slot Model

Keep the existing fixed public slot identity:

- slot index remains stable;
- public port remains stable;
- client-facing credentials remain stable.

Augment each formal slot with an optional **shadow candidate** used only during replacement.

### 4.2 Replacement Rule

When a READY slot becomes replacement-worthy:

1. mark the slot as **replacement pending**;
2. keep the active listener/process alive for a grace period;
3. asynchronously start a shadow candidate on a separate tun device;
4. run the current startup + current health-check path for the shadow candidate;
5. if the shadow candidate passes, atomically swap the formal slot to the shadow resources;
6. then stop the old active resources;
7. if the shadow candidate fails, retain the old active resources and try another candidate while grace time remains.

### 4.3 Grace Window

The user-approved policy is:

- unhealthy old slot may continue serving for **2-5 minutes** while replacement is warming up;
- the old slot should still be removed immediately if its process/listener is fully dead and cannot actually serve traffic.

The first implementation should use a single env/config value with a default inside that range.

## 5. Architecture

## 5.1 Files

- `F:\officeProject\yigehui\aimili-vpngate\proxy_pool.py`
  - main behavior change;
  - owns slot state, replacement scheduling, shadow warmup lifecycle, cutover.
- `F:\officeProject\yigehui\aimili-vpngate\tests\test_proxy_pool.py`
  - new regression tests for no-stop-before-shadow-ready behavior.
- `F:\officeProject\yigehui\aimili-vpngate\vpngate_manager.py`
  - minimal config wiring and status exposure for replacement/grace state.
- `F:\officeProject\yigehui\aimili-vpngate\.env.example`
  - document new replacement grace / shadow capacity config.

## 5.2 Data Model Additions

Keep existing slot lifecycle states (`EMPTY`, `STARTING`, `READY`, `DRAINING`) for the active public slot.

Add separate replacement metadata instead of exploding the public state machine:

- `replacement_pending: bool`
- `replacement_reason: str`
- `replacement_requested_at: float`
- `replacement_deadline_at: float`
- `shadow: ShadowCandidate | None`

Introduce a lightweight `ShadowCandidate` structure containing:

- `node_id`
- `node_ip`
- `country`
- `country_name`
- `ip_type`
- `entry_ip_type`
- `latency_ms`
- `process`
- `listener`
- `config_path`
- `tun_name`
- `exit_ip`
- `health_latency_ms`
- `last_error`
- `started_at`

The active slot remains the only source for public API responses.  
Shadow candidates never appear in `/api/pool/proxies`.

## 5.3 Tun / Port Constraints

The active slot keeps the formal public port.

The shadow candidate:

- must **not** bind the same public port while the active slot still owns it;
- must still prove it can carry proxy traffic through a real listener path;
- therefore should use a temporary shadow listener port from a dedicated ephemeral shadow port range.

This keeps replacement validation realistic without disturbing the active public listener.

## 6. Replacement Flow

### 6.1 Replacement Triggers

Existing triggers remain the source of truth:

- process/listener dead checks;
- current health-check failures from `pool_check_slot_health`;
- start timeout cleanup for actively starting slots.

But for READY slots, repeated health failure should no longer immediately call `_stop_slot(slot)` if the active slot can still serve.

### 6.2 New Flow for Unhealthy READY Slot

When a READY slot crosses replacement threshold:

1. if no replacement is pending, register replacement metadata and grace deadline;
2. queue shadow warmup work if no shadow candidate currently exists;
3. do **not** remove active listener/process yet;
4. continue regular health probes on the active slot during grace;
5. if active slot dies hard during grace, promote replacement urgency and allow immediate takeover once shadow is ready;
6. if shadow passes startup + current health validation, execute cutover;
7. if grace expires before any shadow succeeds, stop the old slot and fall back to current refill behavior.

### 6.3 Cutover Semantics

On successful shadow validation:

1. stop active public listener;
2. stop active OpenVPN process;
3. move shadow resources and metadata into the formal slot;
4. bind the formal public port with the new active listener;
5. clear replacement metadata;
6. keep slot in `READY`.

Cutover must appear atomic under the pool lock so public API never reports a half-swapped slot.

## 7. Startup / Validation Policy

The user explicitly chose to **reuse the current test logic**.

That means the shadow candidate must reuse:

- the current OpenVPN startup path;
- the current listener creation path;
- the current `health_check` callback semantics already used by READY slots.

No new custom “quality score” or external target validation is required in v1.

## 8. Capacity Controls

Because the user approved extra warmup capacity, add separate controls:

- `POOL_SIZE=100` for formal public slots;
- `POOL_MAX_STARTING` remains the active formal-slot startup ceiling;
- new `POOL_MAX_SHADOW_STARTING` controls concurrent shadow warmups;
- new `POOL_REPLACEMENT_GRACE_SECONDS` controls old-slot grace;
- new `POOL_SHADOW_PORT_BASE` and `POOL_SHADOW_PORT_COUNT` reserve temporary listener ports.

The first implementation should stay conservative:

- shadow warmups are bounded;
- shadow warmups should not starve normal empty-slot fill;
- READY count should remain the primary public metric.

## 9. Public API / UI Behavior

Public list/random APIs stay backward-compatible.

Optional additions to status/detail output:

- whether a slot is replacement-pending;
- replacement reason;
- grace deadline timestamp;
- whether a shadow candidate is warming.

These are status-only fields and should not change the shape of proxy list items used by existing clients.

## 10. Error Handling

- Shadow startup failure must not tear down the active slot.
- Shadow health-check failure must not tear down the active slot.
- If the active slot fully dies and no shadow is ready, the slot may still go empty as it does today.
- Expired shadow candidates must be cleaned up fully: listener, process, temp config, metadata.
- Shadow resources must never leak after successful cutover or failed validation.

## 11. Testing Requirements

Add regression tests covering:

1. unhealthy READY slot with live process/listener does **not** stop immediately once replacement begins;
2. shadow startup failure leaves active slot running;
3. successful shadow warmup triggers cutover and preserves slot index/public identity;
4. hard-dead active slot still gets removed if it cannot actually serve;
5. grace expiration without healthy shadow falls back to old stop-and-refill behavior;
6. shadow candidates never appear in list/random proxy APIs.

Tests should use the existing mocking style already present in `tests/test_proxy_pool.py`.

## 12. Minimal Delivery Scope

The first shipping version should change only what is required to support:

- formal slot grace period;
- bounded shadow warmup;
- validated cutover;
- regression coverage.

No broader pool redesign should be mixed into this change.
