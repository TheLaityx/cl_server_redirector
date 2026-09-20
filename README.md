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

1. 下载原版模组 `cl_server_redirector.dll`
2. 修改 `cl_server_redirector.ini` 中的 `CL_SERVER_URL` 为你的服务器地址
3. 参考 `server/DEPLOY.md` 部署 Python 服务端
4. 如需域名 + HTTPS，参考 `setup_reverse_proxy.md`

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
