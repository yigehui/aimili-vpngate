# 2026-07-23 pool refill check

- 结论：`PoolManager` 当前实现会在槽位失效后，基于最新 `sync_from_nodes(available)` 候选集，排除当前已占用的 `READY/STARTING` 节点，再给失效端口补位。
- 证据1：最小复现脚本中，A 失效后，当前 available 列表为 `[B(已占用), C(空闲)]`，结果 `slot0` 从 `A` 补到 `C`。
- 证据2：新增回归测试 `test_health_refills_from_latest_available_nodes_excluding_occupied` 通过。
- 证据3：全量 `python -m unittest tests.test_proxy_pool -v` 22/22 通过。
- 因此“58 可用但仅 32 READY”更可能卡在空槽启动前置条件，而不是候选补位策略。代码里 EMPTY 常见来源：
  - `port {slot.port} occupied`
  - `start timeout after ...`
  - `start_openvpn failed`
- 直接排查入口：`/api/pool/status?detail=1` 看 `slot_detail[].last_error`。
