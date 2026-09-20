# Nginx 反向代理 + 域名部署指南

## 目标

- 把 `cl_server_redirector.ini` 里的裸公网 IP 换成域名
- 通过 Nginx 反向代理隐藏真实服务器 IP
- 可选：启用 HTTPS（SSL 证书）

---

## 前置条件

- 域名：`your-domain.com`
- 服务器公网 IP：`你的服务器IP`
- 服务端已能正常运行（`python3 server.py --port 10901`）

---

## 第一步：DNS 解析（域名管理后台）

在你的域名提供商后台添加解析记录：

| 主机记录 | 记录类型 | 记录值 | TTL |
|---------|---------|--------|-----|
| `@` | A | `你的服务器IP` | 默认 |
| `www` | CNAME | `your-domain.com` | 默认 |

> `@` 表示根域名

验证生效（等 5~10 分钟）：

```bash
nslookup your-domain.com
# 应返回你的服务器IP
```

---

## 第二步：安装 Nginx

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install nginx -y

# 验证
sudo nginx -t
sudo systemctl enable nginx --now
```

---

## 第三步：配置反向代理

### HTTP 模式（无 SSL）

复制 `nginx-nightreign-http.conf` 到服务器：

```bash
sudo cp nginx-nightreign-http.conf /etc/nginx/sites-available/nightreign
sudo ln -s /etc/nginx/sites-available/nightreign /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default  # 移除默认站点，避免冲突
sudo nginx -t
sudo systemctl reload nginx
```

### HTTPS 模式（有 SSL 证书）

如果你有证书文件（`fullchain.pem` + `privkey.pem`）：

```bash
sudo cp nginx-nightreign-https.conf /etc/nginx/sites-available/nightreign
# 编辑配置，把 your-domain.com 和证书路径改成你的
sudo ln -s /etc/nginx/sites-available/nightreign /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

---

## 第四步：修改服务端绑定地址

之前服务端绑定 `0.0.0.0:10901`，现在改成只监听本地（由 Nginx 反向代理进来）：

```bash
# 编辑 systemd 服务
sudo systemctl edit --full su-server
# 把 ExecStart 改成：
# ExecStart=/usr/bin/python3 /root/server.py --host 127.0.0.1 --port 10901 --selfhosted

sudo systemctl daemon-reload
sudo systemctl restart su-server
```

---

## 第五步：修改客户端 INI

```ini
; cl_server_redirector.ini
CL_SERVER_URL = http://your-domain.com
```

或 HTTPS：
```ini
CL_SERVER_URL = https://your-domain.com
```

---

## 验证

```bash
# 1. Nginx 是否正常运行
curl -I http://your-domain.com/client/capabilities
# 应返回 HTTP 200

# 2. 反向代理是否生效（服务端日志应显示请求来自 127.0.0.1）
sudo journalctl -u su-server -f

# 3. 域名解析是否正确
nslookup your-domain.com
```

---

## 安全加固（可选）

### 1. 防火墙：只留 80/443，封掉 10901 外网

```bash
# UFW
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw deny 10901/tcp
sudo ufw reload

# 或 iptables
sudo iptables -A INPUT -p tcp --dport 10901 -j DROP
sudo iptables-save
```

### 2. 用 fail2ban 防暴力扫描

```bash
sudo apt install fail2ban -y
sudo systemctl enable fail2ban --now
```

---

## 常见问题

**Q: 游戏提示"无法连接到服务器"**
- 检查 Nginx 是否运行：`sudo systemctl status nginx`
- 检查域名解析：`nslookup your-domain.com`
- 检查服务端是否监听 127.0.0.1：`ss -tlnp | grep 10901`

**Q: 加了 `--host 127.0.0.1` 后本地 curl 测试不通**
- 这是正常的，因为服务端不再监听外网
- 测试改用：`curl http://127.0.0.1:10901/status`（在服务器本地执行）
- 外网测试改用域名：`curl http://your-domain.com/status`
