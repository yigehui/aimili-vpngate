# AimiliVPN 服务器部署说明

## 一键部署

在 Linux VPS 上以 root 执行：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/yigehui/aimili-vpngate/main/scripts/deploy_server.sh)
```

如果脚本已经在当前项目目录内，也可以直接执行：

```bash
chmod +x scripts/deploy_server.sh
sudo AIMILI_MODE=gateway ./scripts/deploy_server.sh
```

## 代理池模式部署

代理池模式必须提供公网可访问的 host：

远程一键部署：

```bash
AIMILI_MODE=pool \
  POOL_PUBLIC_HOST=你的服务器公网IP或域名 \
  POOL_SIZE=50 \
  POOL_PORT_BASE=52000 \
  POOL_PROXY_USER=你的代理用户名 \
  POOL_PROXY_PASS=你的代理密码 \
  bash <(curl -Ls https://raw.githubusercontent.com/yigehui/aimili-vpngate/main/scripts/deploy_server.sh)
```

本地脚本部署：

```bash
sudo AIMILI_MODE=pool \
  POOL_PUBLIC_HOST=你的服务器公网IP或域名 \
  POOL_SIZE=50 \
  POOL_PORT_BASE=52000 \
  POOL_PROXY_USER=你的代理用户名 \
  POOL_PROXY_PASS=你的代理密码 \
  POOL_LISTEN_HOST=0.0.0.0 \
  ./scripts/deploy_server.sh
```

部署完成后：

- Web 管理页面：`http://服务器IP:8787/安全后缀/`
- 池接口：`http://服务器IP:8787/api/pool/*`
- 代理端口：默认 `52000-52049/tcp`
- Token/代理账号：`/opt/aimilivpn/vpngate_data/pool_secrets.json`

### 代理池代理用户名密码

代理池对外提供的 HTTP/SOCKS5 代理用户名密码有两种配置方式：

1. 环境变量优先：

```bash
POOL_PROXY_USER=你的代理用户名
POOL_PROXY_PASS=你的代理密码
POOL_API_TOKEN=你的接口Token
```

systemd 部署后通常写在：

```bash
/etc/default/aimilivpn
```

修改后重启：

```bash
systemctl restart aimilivpn
```

2. 如果没有配置环境变量，首次池模式启动会自动生成并保存到：

```bash
/opt/aimilivpn/vpngate_data/pool_secrets.json
```

读取示例：

```bash
python3 -c "import json;d=json.load(open('/opt/aimilivpn/vpngate_data/pool_secrets.json'));print(d['proxy_user'], d['proxy_pass'])"
```

## 服务器要求

### 网关模式

- Linux VPS
- root 权限
- Python 3
- OpenVPN
- `iproute2` / `iptables`
- 支持 TUN/TAP
- 放行 Web 管理端口，默认 `8787/tcp`

### 代理池模式

- root 权限和多 TUN 设备能力
- 多 OpenVPN 进程稳定运行
- 建议 4C/24G 以上跑 50 槽位
- 放行：
  - Web 管理端口：`8787/tcp`
  - 池代理端口范围：`POOL_PORT_BASE` 到 `POOL_PORT_BASE + POOL_SIZE - 1`

## 配置文件

项目现在支持直接读取安装目录下的 `.env`：

```bash
cd /opt/aimilivpn
cp .env.example .env
nano .env
systemctl restart aimilivpn
```

systemd 部署后可编辑：

```bash
nano /etc/default/aimilivpn
systemctl restart aimilivpn
```

优先级：

1. systemd/命令行环境变量
2. `/opt/aimilivpn/.env`
3. 程序默认值或 `vpngate_data/pool_secrets.json`

示例见：`.env.example` 和 `scripts/aimilivpn.env.example`

## 常用命令

```bash
systemctl status aimilivpn
journalctl -u aimilivpn -f
systemctl restart aimilivpn
```

代理池 API 示例：

```bash
TOKEN=$(python3 -c "import json;print(json.load(open('/opt/aimilivpn/vpngate_data/pool_secrets.json'))['api_token'])")
curl -H "Authorization: Bearer $TOKEN" "http://服务器IP:8787/api/pool/status?detail=1"
curl -H "Authorization: Bearer $TOKEN" "http://服务器IP:8787/api/pool/proxies?country=JP&ip_type=residential&limit=10"
curl -H "Authorization: Bearer $TOKEN" "http://服务器IP:8787/api/pool/proxies/random?ip_type=hosting"
```
