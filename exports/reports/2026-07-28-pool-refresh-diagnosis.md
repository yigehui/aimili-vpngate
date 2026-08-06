# 2026-07-28 代理池刷新诊断

## 结论

是，这个项目当前代码**默认就是优先保留现有 READY 节点**，不会因为 5 分钟刷新出了一批新节点，就把它们主动塞进已经健康的代理池槽位里。

换句话说：

- 新节点刷新会更新候选列表；
- 但 **已有 READY 槽位只会在 `rolling_replace_from_nodes()` 被选中时滚动替换，或者健康检查失败时被替换**；
- 如果你的运行现场看起来 IP 长时间固定，根因大概率不是“完全不会替换”，而是**替换节奏/触发条件不符合你想要的‘每 5 分钟就把新节点打进池里’**。

## 代码证据

### 1) 节点刷新时，`sync_from_nodes()` 明确写了“不 churn 现有 READY 端口”

文件：`F:\officeProject\yigehui\aimili-vpngate\proxy_pool.py`

- `sync_from_nodes()` 只更新候选列表，然后 `self._request_fill_slots()` 填充 EMPTY 槽位；
- 注释明确说明：**不要因为节点列表刷新而 churn 现有 READY 端口**。

关键位置：825-837 行。

### 2) 真正的“滚动换池”只发生在 `rolling_replace_from_nodes()`

文件：`F:\officeProject\yigehui\aimili-vpngate\vpngate_manager.py`

- 批量测试完成后，才会执行：
  - `pool_manager.sync_from_nodes(available_snapshot)`
  - `pool_manager.rolling_replace_from_nodes(available_snapshot, batch_size=POOL_REFRESH_BATCH_SIZE)`

关键位置：1769-1774 行。

### 3) `rolling_replace_from_nodes()` 不是“全量换新”，只是按批次替换一部分 READY 槽位

文件：`F:\officeProject\yigehui\aimili-vpngate\proxy_pool.py`

- 只挑 `READY` 且未 pending、没有 shadow、进程/监听都活着的槽位；
- 按 `updated_at` 从旧到新排序；
- 最多替换 `batch_size` 个；
- 默认 `POOL_REFRESH_BATCH_SIZE=5`。

关键位置：839-883 行。

这意味着：

- 池子很大时，每轮只换 5 个，本来就会让很多 IP 持续很久；
- `updated_at` 在 READY/cutover 成功后会被重置成当前时间，所以新换上的又会排到后面继续存活。

### 4) 健康检查逻辑不会因为“节点列表变了”而换掉 READY 节点

文件：`F:\officeProject\yigehui\aimili-vpngate\proxy_pool.py`

- `tick_health()` 只在以下情况触发替换：
  - 进程死了；
  - listener 死了；
  - 主动健康检查失败。

关键位置：480-535 行、1346-1379 行。

所以只要 READY 槽位一直健康，**它不会因为外部 5 分钟刷新到了新节点就被踢掉**。

### 5) 当前实现里“grace”其实没有生效成延迟淘汰

文件：`F:\officeProject\yigehui\aimili-vpngate\proxy_pool.py`

- `_should_drop_slot_immediately()` 直接 `return True`；
- 所以健康失败走 `_request_slot_replacement_locked()` 时，会立刻 drop 当前槽位，而不是靠 grace 慢慢等。

关键位置：476-477 行、439-456 行。

这点说明：

- 现在的“稳定不换”不是因为 grace 太长；
- 而是因为**健康的 READY 节点默认就被设计成保留**。

## 对你现象的直接解释

如果你现在是：

- 每 5 分钟刷新一次节点；
- 代理池大部分 IP 长时间固定；

那更接近下面这个真实行为：

1. 5 分钟刷新拿到了新 `available_snapshot`；
2. `sync_from_nodes()` 只更新候选，不动现有 READY；
3. `rolling_replace_from_nodes()` 每轮最多只滚动替换 `POOL_REFRESH_BATCH_SIZE` 个；
4. 没被选中的 READY 节点继续活着；
5. 健康检查也不会因为“有更新节点”而把它们替掉。

所以你的判断**基本是对的**：

> “只要没失效就一直不断开，导致新刷出来的节点根本没怎么进入池子。”

更精确地说，不是“完全不会进池”，而是：

> **只有 EMPTY 槽位、被批量滚动替换命中的槽位、或健康失败的槽位，才会吃到新节点。**

## 已做验证

本地跑过：

- `python -m unittest tests.test_proxy_pool tests.test_vpngate_manager_fetch`

结果：37 tests, OK。

其中现有测试已经证明：

- `sync_from_nodes()` 不会 destructive rebuild；
- `rolling_replace_from_nodes()` 只替换一个 batch；
- `vpngate_manager.test_multiple_nodes()` 会调用 rolling refresh，而不是 replace_all。

## 最小结论

所以答案是：**是，这就是你看到“池里 IP 大多固定”的主因之一，而且是代码当前的明确设计。**

但再精确一点：

- 不是“刷新节点完全没放进代理池”；
- 是“刷新节点只进入少数被轮换/空槽/故障槽位，绝大多数健康 READY 槽位不会动”。
