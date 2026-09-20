# 服务器部署

## 一次性部署（SSH 上服务器执行）

```bash
# 1. 上传文件（本机执行）
scp server/server.py server/su-server.service root@你的服务器IP:/root/

# 2. 服务器上执行
systemctl daemon-reload
systemctl enable --now su-server
systemctl status su-server        # 应显示 active (running)

tail -f /root/requests.log        # 抓包记录
curl http://127.0.0.1:10901/status  # 在线玩家池
```

之后进程死了 systemd 3 秒自动拉起，重启服务器也会自启。

## 验证外网可达（本机执行）

```powershell
Test-NetConnection 你的服务器IP -Port 10901   # 必须 True
curl http://你的服务器IP:10901/status          # 应返回 JSON
```

## 两种运行模式

| 模式 | 命令 | 效果 |
|---|---|---|
| 学习代理（当前推荐） | `--proxy https://nightreign.fs-emu.net` | 游戏经我们服务器透明登录 fs-emu，能正常玩；requests.log 采集完整协议 |
| 纯抓包 | 不带 `--proxy` | `/redirect` 只回 `{"status":"ok"}`，游戏连不上（仅调试用） |

## 注意

- 安全组入方向必须放行 TCP 10901（若换端口需重配）。
