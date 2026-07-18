# Proxy Pool Mode Design

**Date:** 2026-07-19  
**Status:** Approved for planning  
**Project:** aimili-vpngate (AimiliVPN)

## 1. Goal

Transform the project so it can run as a **deployable proxy pool** on a VPS:

- Only **available** (health-checked) nodes are exposed.
- Each pool slot is a dual-protocol **HTTP + SOCKS5** endpoint with a **global username/password**.
- Ports start at **52000** (fixed slot mapping).
- **Token-authenticated HTTP API** lists proxies with filters (country prefix, limit, etc.) and supports **random selection**.
- Modes are mutually exclusive: existing single-tunnel **gateway** vs new **pool**.

## 2. Feasibility

| Layer | Technology | Notes |
| --- | --- | --- |
| Node source / probe | Existing Python (VPNGate API, OpenVPN test) | Reuse |
| Egress tunnel per node | **OpenVPN** + dedicated `tun{N}` | VPNGate configs are OpenVPN; mihomo cannot act as OpenVPN client |
| Multi-port proxy front | Extended `proxy_server` (HTTP/SOCKS adaptive) | No mihomo in v1 |
| Query API | Token-protected routes on existing web port | Read-only |

**mihomo:** Not required for v1. Optional later as “export one YAML” only. OpenVPN has no native multi-port HTTP/SOCKS pool.

**Target scale:** `POOL_SIZE=50` on ~4C/24G VPS, with concurrent start limits and health replacement.

## 3. Decisions (locked)

| Topic | Decision |
| --- | --- |
| Client usage | Multi-port direct proxies (model A) |
| Auth (proxy) | One global `user` / `pass` for all pool ports |
| Auth (API) | Separate `POOL_API_TOKEN` (Bearer / `X-API-Token`) |
| Slot model | Fixed slots; ports `POOL_PORT_BASE + index` (default 52000–52049) |
| Mode coexistence | **Switch only**: `gateway` \| `pool` (not simultaneous) |
| Code structure | New `proxy_pool.py` owns pool logic; `proxy_server.py` thin reusable listener; `vpngate_manager.py` thin orchestration |
| mihomo | Out of scope for v1 |
| Per-port credentials | Out of scope for v1 |

## 4. Architecture

```
vpngate_manager.py          # mode switch, node probe feed, HTTP route glue
        |
        +-- service_mode=gateway --> existing single OpenVPN + :7928
        |
        +-- service_mode=pool ----> proxy_pool.PoolManager
                                         |
                                         +-- OpenVPN x N (tun0..tunN-1)
                                         +-- proxy_server.create_proxy_listener per slot
                                         +-- list / random / status query helpers
```

### 4.1 Mode switch

- Config / env: `SERVICE_MODE=gateway|pool` (default `gateway`).
- Entering **pool**: stop active gateway OpenVPN, clear gateway policy routing, stop `:7928` single listener, then `PoolManager.start()`.
- Entering **gateway**: `PoolManager.shutdown()` (all slots), then existing gateway path.
- Switching requires orderly teardown; do not run both resource stacks.

### 4.2 File boundaries

| File | Role |
| --- | --- |
| `proxy_pool.py` (**new**) | `PoolSlot`, `PoolManager`, fill/replace/health, list/random filters |
| `proxy_server.py` (**thin change**) | Reusable single-port listener start/stop with auth + optional `bind_device`; no slot table |
| `vpngate_manager.py` (**thin glue**) | Read mode, feed available nodes, register `/api/pool/*`, lifecycle |
| `vpn_utils.py` | Prefer unchanged |
| `install.sh` / docs | Pool env vars, firewall note for 52000–52049 |

Optional: small `pool_api.py` only if route handlers clutter manager; otherwise keep parse/auth helpers next to `PoolManager`.

## 5. Slot model and lifecycle

### 5.1 Fixed mapping

| Field | Rule |
| --- | --- |
| Slot count | `POOL_SIZE` (default 50, soft cap suggested ≤64) |
| Port | `POOL_PORT_BASE + index` (default base 52000) |
| TUN | `tun{index}` |
| Proxy auth | Global `POOL_PROXY_USER` / `POOL_PROXY_PASS` (required in pool mode; no anonymous proxy) |

### 5.2 State machine

```
EMPTY → STARTING → READY
          │          │
          │ fail     │ health fail / node dead
          ▼          ▼
       DRAINING → EMPTY → (re-allocate)
```

- API “available” means **READY only**.
- Same `node_id` / remote endpoint occupies at most one slot.
- Prefer **stable slot assignment** while a node stays available (port stability for clients).
- Concurrent `STARTING` capped by `POOL_MAX_STARTING` (default 5).
- On start failure: slot → EMPTY, skip node briefly, try another candidate.
- Drain order: stop accepting new proxy work → stop listener → kill OpenVPN → clear per-slot routes → EMPTY.

### 5.3 Routing constraint

Pool mode must **not** steal the host default route the way gateway policy routing does for a single tunnel. Each proxy connection must egress via its slot’s `tun{N}` (reuse/extend bind-to-device / per-interface path already used by local proxy).

### 5.4 Health

| Check | Typical interval | Action |
| --- | --- | --- |
| OpenVPN process alive | 10–30s | fail → DRAINING/replace after threshold |
| Egress probe via slot | 60–120s | N consecutive fails → replace |
| Listener alive | with process tick | restart listener or replace slot |

### 5.5 Resource guards (4C/24G)

- Do not force 50 slots if fewer nodes are available.
- Global and/or per-slot max proxy connections (configurable).
- Ordered shutdown on signal to avoid orphaned `openvpn` processes.

## 6. Proxy front

- Dual protocol on one port (existing adaptive HTTP + SOCKS5 behavior).
- Listen host: `POOL_LISTEN_HOST` (default public bind; operators must firewall + strong password).
- Public host advertised in API: `POOL_PUBLIC_HOST` (explicit preferred; auto-detect fallback).
- Auth failures: HTTP 407 / SOCKS5 auth reject.

### 6.1 `proxy_server` conceptual API

```text
create_proxy_listener(
    host, port,
    *,
    username, password,
    bind_device: str | None,
    max_connections: int | None,
) -> ProxyListener   # start / stop / is_alive
```

Gateway keeps one listener; pool creates one per READY path (started after tunnel ready).

## 7. Token API

Enabled only when `service_mode=pool`. Served on existing UI HTTP port (default 8787).

### 7.1 Authentication

- Header: `Authorization: Bearer <POOL_API_TOKEN>` or `X-API-Token: <POOL_API_TOKEN>`.
- Missing/wrong → **401**.
- Not in pool mode → **403**.
- Invalid query → **400**.
- Pool not ready (optional) → **503**.

Web UI session does **not** substitute for API token on these routes.

### 7.2 Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/pool/proxies` | Filtered list of READY proxies |
| `GET` | `/api/pool/proxies/random` | One random READY proxy (same filters) |
| `GET` | `/api/pool/status` | Pool counters / mode / public host |
| `GET` | `/api/pool/health` | Liveness (token required in v1) |

No write APIs in v1 (no lease, no password rotate via API).

### 7.3 Query parameters (list + random)

| Param | Type | Default | Meaning |
| --- | --- | --- | --- |
| `country` | string | empty | Case-insensitive prefix on `country_short`; comma multi: `JP,KR` |
| `limit` | int | `0` = unlimited (list only) | Max rows for **list only**; **ignored** on random (random always returns at most one proxy) |
| `offset` | int | `0` | List pagination only |
| `sort` | string | `latency` | `latency` \| `country` \| `port` (list; random ignores sort) |
| `protocol` | string | `all` | Affects advertised URL preference only if needed |

### 7.4 Random endpoint behavior

`GET /api/pool/proxies/random`

1. Build candidate set = READY slots matching `country` (and any other shared filters).
2. If empty → **404** with `{ "ok": false, "error": "no_proxy_available" }`.
3. Else pick **uniform random** one candidate.
4. Response shape: same object as one element of `proxies[]` (or wrapped):

```json
{
  "ok": true,
  "proxy": { /* same fields as list item */ }
}
```

Optional query `exclude=port1,port2` or `exclude=id1,id2` may be added if cheap; not required for v1 unless implemented in the same pass.

### 7.5 List response shape

```json
{
  "ok": true,
  "total": 12,
  "count": 12,
  "proxies": [
    {
      "id": "JP_1.2.3.4_1195_udp",
      "slot": 3,
      "port": 52003,
      "host": "203.0.113.10",
      "country": "JP",
      "country_name": "日本",
      "latency_ms": 86,
      "protocol": "http,socks5",
      "username": "pooluser",
      "password": "poolpass",
      "http": "http://pooluser:poolpass@203.0.113.10:52003",
      "socks5": "socks5://pooluser:poolpass@203.0.113.10:52003",
      "node_ip": "1.2.3.4",
      "updated_at": 1720000000
    }
  ]
}
```

- `POOL_API_RETURN_CREDENTIALS` (default `true`): when `false`, omit username/password and credential-embedded URLs; still return host/port.
- Random uses the same credential policy.

### 7.6 Status shape

```json
{
  "ok": true,
  "mode": "pool",
  "pool_size": 50,
  "port_base": 52000,
  "slots": { "ready": 41, "starting": 2, "empty": 6, "draining": 1 },
  "proxy_auth": true,
  "public_host": "203.0.113.10"
}
```

Optional `?detail=1` for per-slot summary (no OpenVPN config plaintext).

### 7.7 Examples

```bash
curl -s -H "Authorization: Bearer $POOL_API_TOKEN" \
  "http://$HOST:8787/api/pool/proxies?country=JP&limit=10"

curl -s -H "Authorization: Bearer $POOL_API_TOKEN" \
  "http://$HOST:8787/api/pool/proxies/random?country=US"

curl -x "http://pooluser:poolpass@203.0.113.10:52003" https://ifconfig.me
```

## 8. Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SERVICE_MODE` | `gateway` | `gateway` \| `pool` |
| `POOL_SIZE` | `50` | Slot count |
| `POOL_PORT_BASE` | `52000` | First proxy port |
| `POOL_API_TOKEN` | generate if empty | API token |
| `POOL_PROXY_USER` | generate if empty | Global proxy user |
| `POOL_PROXY_PASS` | generate if empty | Global proxy password |
| `POOL_PUBLIC_HOST` | auto-detect | Host embedded in API URLs |
| `POOL_LISTEN_HOST` | `0.0.0.0` | Proxy bind address |
| `POOL_API_RETURN_CREDENTIALS` | `true` | Include creds in API JSON |
| `POOL_MAX_STARTING` | `5` | Max concurrent tunnel starts |

On first pool boot, persist generated secrets under data dir (mode 0600) and print once to console (install/docs).

## 9. Manager integration

- Probe/fetch loops keep maintaining `nodes.json` / availability.
- In pool mode, **do not** run single-node auto-connect / gateway routing selection.
- After probe batch: `PoolManager.sync_from_nodes(available_nodes)`.
- Keep READY slots whose nodes remain available; replace dead; fill EMPTY under start limit.
- Logging module tags: `Pool`, `PoolSlot` via existing `log_to_json`.
- Web UI v1 minimum: show mode + ready/starting/empty counts; full pool UI is later.

## 10. Error handling

| Scenario | Behavior |
| --- | --- |
| OpenVPN start fail/timeout | Slot EMPTY; brief skip node; try next |
| TUN/permission failure | Log critical; status unhealthy; API empty/503 as appropriate |
| Port bind failure | Fail that slot only |
| Health flap | Require N consecutive failures before replace |
| No candidates for random | 404 `no_proxy_available` |
| Process exit signal | Shutdown all slots, kill children, clear routes |

## 11. Testing strategy

**Unit (no live VPNGate required)**

- Country/limit/offset/sort filters
- Random: only READY, respects country, 404 when empty, uniform over filtered set
- Token missing/wrong/ok
- State transitions and dedupe
- Port formula stability

**Integration (Linux + openvpn)**

- `POOL_SIZE=3` full path to READY
- Traffic via HTTP and SOCKS5 with global auth
- Kill one openvpn → replace
- Mode switch leaves no orphan processes/ports

**Smoke on 4C/24G**

- 50 slots, `POOL_MAX_STARTING=5`, memory/CPU/fd observation
- List + random under load

## 12. Implementation phases

| Phase | Deliverable |
| --- | --- |
| P0 | Extract `ProxyListener`; `PoolManager` skeleton; mode switch; single slot E2E |
| P1 | Multi-slot fill, start limit, health replace, size 50 |
| P2 | Token API: list, **random**, status, filters, credentials flag |
| P3 | install/docs, teardown hardening, resource smoke |

## 13. Out of scope (v1)

- mihomo runtime integration
- Per-port or per-user proxy credentials
- Simultaneous gateway + pool
- Write/lease APIs
- Unlimited dynamic ports beyond `POOL_SIZE`
- Full pool administration UI

## 14. Success criteria

1. With `SERVICE_MODE=pool`, up to 50 READY nodes expose `52000+` dual-protocol proxies with one global account.
2. `GET /api/pool/proxies` returns only usable proxies and honors country/limit filters.
3. `GET /api/pool/proxies/random` returns one usable proxy or clear 404.
4. Unusable nodes are not listed and are replaced without stranding ports.
5. Switching back to gateway cleans pool resources.
6. New pool logic lives primarily in `proxy_pool.py`; existing large files only glue and thin listener reuse.
