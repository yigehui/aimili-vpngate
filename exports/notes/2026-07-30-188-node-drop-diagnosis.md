# 2026-07-30 188 节点数回落诊断

## 结论

`163.192.58.188` 上“只能拉到约 190 个节点”，从源码链路看，**不是 `MAX_SCAN_ROWS` 把 300+ 截断了**。

当前代码里真正控制总候选数的是两层：

1. **单源返回量**：`fetch_candidates()` 对每个源只遍历 `rows[:MAX_SCAN_ROWS]`，但现在官方/镜像单源本身通常只有 `93-98` 行，远低于 300/500。
2. **源数量**：总量主要靠 `get_candidate_api_urls()` 合并主站 + 多个 mirror；如果只成功拿到 `2` 个源，总数就会落在 `190` 左右。

## 关键证据

### 1) 抓取链路源码
- `vpngate_manager.py:161` `MAX_SCAN_ROWS = env_int("MAX_SCAN_ROWS", 300, 1)`
- `vpngate_manager.py:162` `MERGE_MIRROR_SOURCES = env_bool("MERGE_MIRROR_SOURCES", True)`
- `vpngate_manager.py:163` `MAX_MIRROR_SOURCES = env_int("MAX_MIRROR_SOURCES", 6, 0, 20)`
- `vpngate_manager.py:850-876` `get_candidate_api_urls()`：主站 + mirror 合并
- `vpngate_manager.py:975-1056` `fetch_candidates()`：遍历每个源，并在 `1000` 行附近执行 `for row in rows[:MAX_SCAN_ROWS]:`
- `vpngate_manager.py:1052` 把最终结果写成 `Fetched {len(candidates)} unique candidates from {successful_sources}/{len(source_urls)} sources.`

### 2) 本地当前运行时配置
- `.env` 只有 `MAX_SCAN_ROWS=500`
- 当前运行时代码实际值：
  - `MAX_SCAN_ROWS=500`
  - `MERGE_MIRROR_SOURCES=True`
  - `MAX_MIRROR_SOURCES=6`
  - `source_urls=7`

说明本地现在并没有被 200/190 这种硬上限卡住。

### 3) 单源真实返回量
`exports/reports/node_fetch_diagnostic.json` 显示：
- 官方 `https://www.vpngate.net/api/iphone/` 原始行数 `98`
- 去重后 `96`
- 把窗口从 `100/150/200/300/500/800/1000` 拉大，最终接受数都还是 `96`
- `extra_if_no_limit = 0`

这说明：**单源现在就这么多，`MAX_SCAN_ROWS` 再调大也不会从 96 变成 300+。**

### 4) 多源合并时为什么能到 300+
历史诊断 `exports/reports/source_quality_diagnostic.json` 显示：
- 官方源：`98`
- 其他可用镜像：`93-97`
- 合并后总数：`500`
- 重叠很低（很多镜像只和官方重叠 `4-9` 个）

所以之前“300+”靠的是：**主站 + 多个镜像源一起并集去重**，不是单个 API 自己有 300+。

## 对 188 上 190 个节点的最可能解释

按当前源码和现网返回规模，`190` 这个数非常像：

- 官方约 `96`
- 再加一个镜像约 `93-97`
- 合计约 `189-193`

也就是：**188 现在大概率只合并成功了 2 个源（主站 + 1 个镜像，或 2 个成功源）**。

优先排查这几个点：

1. `MAX_MIRROR_SOURCES` 是否在 188 被改成了 `1`
2. `MERGE_MIRROR_SOURCES` 是否被关掉后，又只靠缓存/少量 fallback 源
3. mirror 发现失败 / 大部分 mirror 超时，导致 `successful_sources` 实际只有 `2/7`
4. 188 部署的不是当前主干，还是旧版本或旧缓存

## 我这次对 188 的 live 触达结果

- `http://163.192.58.188:8787/api/pool/health` 还能返回 `401 unauthorized`，说明服务还在。
- 但 SSH `root@163.192.58.188` 免密当前失败：`Permission denied (publickey,keyboard-interactive)`。
- 所以这次没法直接读取 188 的 `/etc/default/aimilivpn`、`/opt/aimilivpn/.env`、`state.json` 和日志做最终实锤。

## 最短核查命令（去 188 上执行）

```bash
grep -E '^(MAX_SCAN_ROWS|MERGE_MIRROR_SOURCES|MAX_MIRROR_SOURCES|POOL_SIZE|SERVICE_MODE)=' /etc/default/aimilivpn /opt/aimilivpn/.env 2>/dev/null
python3 - <<'PY'
import json
p='/opt/aimilivpn/vpngate_data/state.json'
d=json.load(open(p,'r',encoding='utf-8'))
print(d.get('last_fetch_message'))
PY
journalctl -u aimilivpn -n 200 --no-pager | grep -E '源数量|Fetched|拉取成功|镜像站列表|timed out|timeout'
```

如果你把 188 上这三段输出贴我，我可以直接给你定位到底是：**只开了 1 个镜像、镜像发现失败、还是部署版本落后**。
