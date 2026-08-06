# 2026-07-30 188 mirror source refresh

- Server: 163.192.58.188
- SSH user: ubuntu via D:\mykey\188.ppk
- Goal: find extra live mirrors and remove dead sources

## Findings
- Runtime before change: `MAX_MIRROR_SOURCES=3`
- Current official + discovered mirrors total: 7 sources
- Probe results: official + 6 mirrors all alive
- Current cache file already contained the 6 live mirrors only; no dead cached mirror needed removal
- Root cause of low candidate count: config limited mirrors to 3, so only 4 total sources were used

## Live probe rows
- official: 98
- 150.40.105.13:35090: 95
- 150.40.105.23:64629: 94
- 150.40.105.11:4917: 95
- 150.40.105.8:61446: 94
- 150.40.105.15:38301: 94
- 150.40.105.17:50406: 95

## Changes applied
- `/etc/default/aimilivpn`: `MAX_MIRROR_SOURCES=6`
- `/opt/aimilivpn/.env`: `MAX_MIRROR_SOURCES=6`
- `systemctl restart aimilivpn`

## Verification
- Runtime `source_urls`: 7 total
- `state.json`: `Fetched 191 unique candidates from 7/7 sources.`
- Pool refill in progress after restart
