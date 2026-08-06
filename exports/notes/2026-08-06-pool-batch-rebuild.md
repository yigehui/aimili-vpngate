# 2026-08-06 pool batch rebuild change

## Result

- Pulled latest `origin/main`: already up to date.
- Pool refresh now rebuilds from the latest fully tested available node snapshot.
- Rebuild batch size is fixed at 30 via `proxy_pool.DEFAULT_REFRESH_BATCH_SIZE`.
- Health checks now drop unhealthy READY slots and do not start automatic refill/replacement from cached candidates.

## Evidence

- `vpngate_manager.test_multiple_nodes()` calls `replace_all_slots_from_target_nodes(available_snapshot, batch_size=30)` only after the node test batch has completed and `nodes.json` has been updated.
- `PoolManager.tick_health()` no longer calls `_request_fill_slots()` after health checks.
- `_probe_ready_slot()` stops failed READY slots directly and leaves them empty instead of requesting a shadow replacement.

## Verification

- `python -m unittest tests.test_proxy_pool -v`
- `python -m unittest tests.test_vpngate_manager_fetch -v`
- `python -m unittest tests.test_pool_api_auth -v`
- `python -m py_compile proxy_pool.py vpngate_manager.py`

## Notes

- No `cases/<slug>/state.json` was present in this repository, so no case state was updated.
