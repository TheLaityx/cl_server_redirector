# cl_server_redirector 私服服务端配置

基于 [Church Guard 的 cl_server_redirector](https://www.nexusmods.com/eldenringnightreign/mods/?) 模组，配置私人服务器。

**本仓库只包含服务端配置，不包含 DLL 文件。**
DLL 模组请从 NexusMods 下载原版 `cl_server_redirector.dll`。

## 文件说明

| 文件 | 说明 |
|------|------|
| `server/server.py` | Python 私服服务端（公告、匹配本地拦截、反向代理兼容） |
| `server/su-server.service` | systemd 服务配置（自动启动/守护） |
| `server/DEPLOY.md` | Python 服务端部署文档 |
| `nginx-nightreign-http.conf` | Nginx HTTP 反向代理配置 |
| `nginx-nightreign-https.conf` | Nginx HTTPS 反向代理配置（可选） |
| `setup_reverse_proxy.md` | 反向代理 + 域名部署教程 |
| `cl_server_redirector.ini` | 客户端模组配置模板（修改 `CL_SERVER_URL` 为你的地址） |
| `公告.txt` | 游戏内公告内容参考 |

## 快速开始

### 1. 部署 Python 服务端

需要一台 Linux 服务器（Ubuntu/Debian 等），Python 3 已安装。

```bash
# 1. 上传 server.py 到服务器（本机执行）
scp server/server.py root@你的服务器IP:/root/

# 2. 服务器上启动（前台测试）
python3 /root/server.py --host 0.0.0.0 --port 10901 --selfhosted

# 3. 验证启动成功（另开窗口）
curl http://你的服务器IP:10901/client/capabilities?steam_id=test
# 应返回 JSON
```

**后台常驻（systemd）：**

仓库里已提供 `server/su-server.service`，上传到服务器后启用：

```bash
# 上传
scp server/su-server.service root@你的服务器IP:/etc/systemd/system/

# 服务器上启用
systemctl daemon-reload
systemctl enable --now su-server
systemctl status su-server   # 应显示 active (running)
```

> 默认配置为 `--proxy` 学习代理模式。如需 `--selfhosted` 纯私服模式，编辑 `/etc/systemd/system/su-server.service` 修改 `ExecStart` 参数。

**防火墙/安全组：**
- 放行 TCP **10901** 端口（或你自定义的端口）
- 如果用了 Nginx 反向代理，则只放行 80/443，服务端改绑 `127.0.0.1`

### 2. 配置客户端模组

下载原版模组 `cl_server_redirector.dll`，放到游戏目录，修改同目录的 `cl_server_redirector.ini`：

```ini
[cl_server_redirector]
CL_SERVER_URL=http://你的服务器IP:10901
CL_CUSTOM_SHARD_NAME=你的Shard名
CL_USE_ALT_SAVE=true
```

所有联机玩家必须使用**相同的 `CL_CUSTOM_SHARD_NAME`**，否则互相匹配不到。

### 3. 域名 + HTTPS（可选）

参考 `setup_reverse_proxy.md` 配置 Nginx 反向代理 + SSL 证书。

## 如何修改游戏内公告

打开 `server/server.py`，搜索 `RequestGetAnnounceMessageListParams`，找到 `to_all_msg` 数组：

```python
{
    "seq_id": 0,           # 公告页序号，从 0 开始
    "page_id": 0,          # 页码
    "lang_code": "SIMPLIFIED_CHINESE",
    "abstract": "",        # 摘要（可为空）
    "message": "PHA+...",  # 公告正文，Base64 编码的 HTML
    "start_at": {"seconds": now},
},
```

**`message` 字段是 Base64 编码的 HTML。** 把自己的公告内容用以下方式编码后填入：

```bash
# Linux / macOS
echo -n '<p>你的公告内容</p>' | base64

# Python
import base64
base64.b64encode(b'<p>你的公告内容</p>').decode()
```

支持 HTML 标签：`<p>`、`<br>`、`<font>` 等。`公告.txt` 里有完整示例。

## 免责声明

仅供学习研究与自建私服联机使用，请确保你拥有正版《艾尔登法环：黑夜君临》。
