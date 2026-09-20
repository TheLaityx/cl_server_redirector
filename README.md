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
| `su_server_redirector.ini` | 客户端模组配置模板 |
| `公告.txt` | 游戏内公告内容参考 |

## 快速开始

1. 下载原版模组 `cl_server_redirector.dll`
2. 修改 `su_server_redirector.ini` 中的 `SU_SERVER_URL` 为你的服务器地址
3. 参考 `server/DEPLOY.md` 部署 Python 服务端
4. 如需域名 + HTTPS，参考 `setup_reverse_proxy.md`

## 免责声明

仅供学习研究与自建私服联机使用，请确保你拥有正版《艾尔登法环：黑夜君临》。
