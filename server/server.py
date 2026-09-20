#!/usr/bin/env python3
# su_server_redirector 配套私服骨架（Python 3 标准库，无第三方依赖）
#
# 已实现：
#   GET  /client/capabilities   能力探测
#   POST /client/login          登录（passkey 校验 + shard 匹配池登记）
#   ANY  /redirect 及其他路径   全量抓包记录到 requests.log（用于逆向游戏协议）
#   GET  /status                查看当前各 shard 在线玩家（调试用）
#
# 用法：
#   python3 server.py --port 10901 --passkey 你的密码
#   python3 server.py --port 8443 --passkey xxx --cert server.crt --key server.key   # HTTPS
#
# 注意：/redirect 之后的游戏会话协议（FromSoftware 私有协议）需要逆向后
# 在 handle_game_protocol() 中实现，当前版本只做抓包记录，方便你学习协议。

import argparse
import base64
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PASSKEY = ""
PLAYERS = {}          # steam_id -> {shard, mod_version, last_seen, player_id}
PLAYERS_LOCK = threading.Lock()
PLAYER_TTL = 300      # 秒，超过视为离线
SESSIONS = {}         # session_id -> last_seen（代理模式下统计在线人数用）
SESSIONS_LOCK = threading.Lock()
LOG_FILE = "requests.log"
LOG_HANDLE = None       # 持久化日志文件句柄
LOG_LOCK = threading.Lock()
SELFHOSTED = False    # --selfhosted：/redirect 与 /api/ 本地应答
REPLICA = False       # --replica：/redirect 逐字节复刻 fs-emu 已验证应答（对照实验）
NEXT_PLAYER_ID = 1000 # 自研协议下分配的 player_id 起点

# fs-emu 2026-09-11 20:29 实测 200 应答（268 字节，游戏接受）。
# created_at/expires_at 回复时刷新为当前时间，其余原样。
REPLICA_LOGIN = {
    "type": "ResponseCreateSessionParams",
    "value": {
        "player_id": 36833,
        "user_id": "011000015f14e671",
        "client_ip": "47.80.31.123",
        "refresh_token": {
            "client_id": "641655389663097553",
            "created_at": "1789129781",
            "expires_at": "1789136981",
            "signature": "signature",
        },
        "redirect_addr": "",
    },
}

# 学习代理：非空时把未实现的请求转发到上游真实服务器并记录响应
PROXY_UPSTREAM = ""   # 例如 https://nightreign.fs-emu.net
PROXY_HOST = ""
PROXY_PORT = 443
PROXY_BASE_PATH = ""
PROXY_PASSKEY = ""    # 上游(fs-emu)的访问密码；客户端没配密码时代为注入
PROXY_TIMEOUT = 20    # 秒；游戏等不了 60s


def log_line(text):
    global LOG_HANDLE
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
    print(line, end="")
    with LOG_LOCK:
        if LOG_HANDLE is None:
            LOG_HANDLE = open(LOG_FILE, "a", encoding="utf-8")
        LOG_HANDLE.write(line)
        LOG_HANDLE.flush()


def evict_stale():
    now = time.time()
    with PLAYERS_LOCK:
        stale = [k for k, v in PLAYERS.items() if now - v["last_seen"] > PLAYER_TTL]
        for k in stale:
            log_line("player offline: %s (shard=%s)" % (k, PLAYERS[k]["shard"]))
            del PLAYERS[k]


def record_session(headers):
    """从 Cookie 中提取 session-id，记录活跃会话（代理模式下统计在线人数）。"""
    cookie = headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("session-id="):
            sid = part[len("session-id="):]
            with SESSIONS_LOCK:
                SESSIONS[sid] = time.time()
            break


def evict_sessions():
    """清理 5 分钟没活动的 session。"""
    now = time.time()
    with SESSIONS_LOCK:
        stale = [k for k, v in SESSIONS.items() if now - v > PLAYER_TTL]
        for k in stale:
            del SESSIONS[k]


def online_in_shard(pool):
    with PLAYERS_LOCK:
        return sum(1 for v in PLAYERS.values() if v.get("pool") == pool)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SU_PrivateServer/0.1"

    def send_response(self, code, message=None):
        # 只写状态行，不带 Server/Date 头。原版 redirector 会从响应里过滤
        # date/server/set-cookie/content-type —— 游戏对这些头敏感，
        # Python 默认的 "Server: xxx Python/3.x" 会让游戏拒收登录应答。
        self.log_request(code)
        self.send_response_only(code, message)

    # ---------- 工具 ----------
    def _send_json(self, obj, status=200):
        # 紧凑 JSON（separators 无空格）——fs-emu 的应答就是这个格式，
        # 游戏引擎的自研解析器对冒号后的空格可能不兼容。
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive")  # 与 fs-emu/Cloudflare 一致
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _check_passkey(self):
        if not PASSKEY:
            return True
        return self.headers.get("passkey", "") == PASSKEY

    def _reject(self):
        self._send_json({"error": "UNAUTHORIZED"}, status=401)

    def _capture(self, method):
        """把请求完整记录下来——这是逆向游戏协议的关键数据来源。"""
        body = self._read_body()
        # 代理模式下 PLAYERS 字典可能为空，用 session-id Cookie 统计在线人数
        record_session(self.headers)
        # 完全跳过高频通知轮询，不写任何日志（每秒多次/每玩家，日志膨胀元凶）
        if body:
            try:
                if "RequestGetNotificationMessageParams" in body.decode("utf-8", errors="replace"):
                    return body
            except Exception:
                pass
        log_line("=== %s %s" % (method, self.path))
        for k, v in self.headers.items():
            log_line("    %s: %s" % (k, v))
        if body:
            try:
                log_line("    body: " + body.decode("utf-8", errors="replace"))
            except Exception:
                log_line("    body(%d bytes binary)" % len(body))
        return body

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/client/capabilities":
            self.handle_capabilities(parsed)
        elif parsed.path == "/status":
            self.handle_status()
        else:
            self.handle_game_protocol("GET")

    def _safe(self, fn):
        """处理器异常兜底：记日志 + 尽力回个响应，别静默掐断连接。"""
        try:
            fn()
        except Exception:
            import traceback
            log_line("handler CRASHED:\n" + traceback.format_exc())
            try:
                self._send_json({"status": "error"})
            except Exception:
                pass

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/client/login":
            self.handle_login()
        elif SELFHOSTED and parsed.path == "/redirect":
            self._safe(self.handle_redirect_selfhosted)
        elif parsed.path.rstrip("/") == "/api":
            # Always handle /api/ locally for interception (announcements, etc.)
            # even in proxy mode; unknown types fall through to proxy inside handle_api.
            self._safe(self.handle_api)
        else:
            self.handle_game_protocol("POST")

    # 其他方法（PUT/DELETE/...）一律抓包
    def __getattr__(self, name):
        if name.startswith("do_"):
            return lambda: self.handle_game_protocol(name[3:])
        raise AttributeError(name)

    # ---------- 端点 ----------
    def handle_capabilities(self, parsed):
        if not self._check_passkey():
            return self._reject()
        qs = parse_qs(parsed.query)
        steam_id = qs.get("steam_id", ["?"])[0]
        log_line("capabilities probe from steam_id=%s" % steam_id)
        self._send_json({
            "capabilities": ["matchmaking", "passive_multiplayer"],
            "server": "SU_PrivateServer/0.1",
            "serverMessage": {
                "title": "Ultra 野排服",
                "body": "私人服务器\n如有问题请在社区反馈",
                "type": "info",
            },
        })

    def handle_login(self):
        if not self._check_passkey():
            log_line("login REJECTED (bad passkey) from %s" % self.client_address[0])
            return self._reject()
        try:
            data = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            data = {}
        steam_id = str(data.get("steam_id", "?"))
        shard = data.get("shard", "") or "(default)"
        mod_version = data.get("mod_version", "?")
        regbin_hash = data.get("regbin_hash", "") or "(unknown)"
        # 匹配池分区键 = shard + regulation.bin 哈希：
        # shard 名相同但装了不同数据模组的人不会进入同一池。
        pool = "%s@%s" % (shard, regbin_hash)

        # TODO: 如需验证玩家身份，用 data["ticket"] 调 Steam Web API:
        #   GET https://api.steampowered.com/ISteamUserAuth/AuthenticateUserTicket/v1/
        #       ?key=<你的Steam WebAPI Key>&appid=<AppID>&ticket=<ticket>

        evict_stale()
        with PLAYERS_LOCK:
            PLAYERS[steam_id] = {
                "shard": shard, "pool": pool, "mod_version": mod_version,
                "last_seen": time.time(),
            }
        n = online_in_shard(pool)
        log_line("login OK: steam_id=%s pool=%s mod=%s (pool online=%d)"
                 % (steam_id, pool, mod_version, n))
        self._send_json({"status": "ok", "shard": shard, "pool": pool, "online_in_pool": n})

    def handle_status(self):
        evict_stale()
        with PLAYERS_LOCK:
            pools = {}
            for sid, v in PLAYERS.items():
                pools.setdefault(v["pool"], []).append(
                    {"steam_id": sid, "shard": v["shard"], "mod_version": v["mod_version"],
                     "idle_sec": int(time.time() - v["last_seen"])})
        self._send_json({"players": pools, "total": sum(len(x) for x in pools.values())})

    # ---------- 自研协议（--selfhosted） ----------
    # 协议格式从 fs-emu 抓包学得：
    #   POST /redirect -> {"type":"ResponseCreateSessionParams","value":{
    #       player_id, user_id, client_ip,
    #       refresh_token:{client_id,created_at,expires_at,signature},
    #       redirect_addr}}
    #   POST /api/     请求  {"type":"RequestXxxParams","value":{...}}
    #                应答  {"type":"ResponseXxxParams","value":{...}}
    def handle_redirect_selfhosted(self):
        """游戏登录：本地直接签发会话，不再依赖 fs-emu。"""
        global NEXT_PLAYER_ID
        body = self._capture("POST")
        steam_id = "0"
        shard_tag = ""
        try:
            data = json.loads(body or b"{}")
            value = (data.get("emu_auth_ticket") or {}).get("value") or {}
            steam_id = str(value.get("steam_id") or "0")
            shard_tag = value.get("shard", "")
        except json.JSONDecodeError:
            pass

        evict_stale()
        with PLAYERS_LOCK:
            entry = PLAYERS.get(steam_id)
            # 记录可能来自 /client/login 握手（没有 player_id）——缺就补发
            if entry and "player_id" in entry:
                player_id = entry["player_id"]
            else:
                player_id = NEXT_PLAYER_ID
                NEXT_PLAYER_ID += 1
            PLAYERS[steam_id] = {
                "shard": shard_tag or "(unknown)", "pool": shard_tag or "(unknown)",
                "mod_version": "game", "last_seen": time.time(),
                "player_id": player_id,
            }
        now = int(time.time())
        if REPLICA:
            # 对照实验：逐字节复刻 fs-emu 成功应答，只刷新时间戳
            resp = json.loads(json.dumps(REPLICA_LOGIN))
            resp["value"]["refresh_token"]["created_at"] = str(now)
            resp["value"]["refresh_token"]["expires_at"] = str(now + 7200)
            resp["value"]["client_ip"] = self._client_ip()
            resp["value"]["user_id"] = steam_id
        else:
            resp = {
                "type": "ResponseCreateSessionParams",
                "value": {
                    "player_id": player_id,
                    "user_id": steam_id,
                    "client_ip": self._client_ip(),
                    "refresh_token": {
                        "client_id": str(6 * 10**17 + player_id * 7919 + now % 100000),
                        "created_at": str(now),
                        "expires_at": str(now + 7200),
                        "signature": "signature",
                    },
                    "redirect_addr": "",
                },
            }
        log_line("selfhosted login: steam_id=%s player_id=%d pool=%s%s"
                 % (steam_id, player_id, shard_tag, " [REPLICA]" if REPLICA else ""))
        log_line("    reply: " + json.dumps(resp, separators=(",", ":")))
        self._send_json(resp)

    # 匹配相关请求本地处理（不转发 fs-emu，fs-emu 不认识私服玩家 session）
    API_MATCH_STUBS = {
        "RequestCreateMatchParams": {"match_id": ""},
        "RequestJoinMatchParams": {"match_id": ""},
        "RequestCancelMatchParams": {},
        "RequestSearchMatchParams": {"matches": []},
        "RequestKeepMultiplayParams": {},
    }

    # /api/ 本地应答表：日志/通知类接口回空 value 即可满足游戏。
    # 未知类型：有 --proxy 就转发 fs-emu 学习（抓响应），否则回通用空信封。
    API_LOCAL_SIMPLE = {
        "RequestNotifyFamilySharingInfoLogParams": {},
        "RequestBasicKpiLogParams": {},
    }

    def handle_api(self):
        body = self._capture("POST")
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            data = {}
        req_type = data.get("type", "")
        resp_type = "Response" + req_type[len("Request"):] if req_type.startswith("Request") else ""

        if req_type == "RequestGetNotificationMessageParams":
            now = int(time.time())
            return self._send_json({
                "type": "ResponseGetNotificationMessageParams",
                "value": {
                    "messages": [],
                    "in_campaign": True,
                    "campaign_info": {
                        "campaign_id": -1,
                        "start_at": {"seconds": str(now)},
                        "end_at": {"seconds": str(now + 30 * 86400)},
                        "target_boss": ["BOSS_00", "BOSS_01", "BOSS_02", "BOSS_03",
                                        "BOSS_04", "BOSS_05", "BOSS_06", "BOSS_08"],
                        "reward": 67,
                    },
                    "enable_chaos": True,
                },
            })
        if req_type == "RequestGetAnnounceMessageListParams":
            now = str(int(time.time()))
            return self._send_json({
                "type": "ResponseGetAnnounceMessageListParams",
                "value": {
                    "to_all_msg": [
                        {
                            "seq_id": 0,
                            "page_id": 0,
                            "lang_code": "SIMPLIFIED_CHINESE",
                            "abstract": "PHAgYWxpZ249ImNlbnRlciI+PGltZyBzcmM9J2ltZzovL0Zha2VMb2FkaW5nSWNvbi5wbmcnIGhlaWdodD0iNDgiIHdpZHRoPSI0OCI+PGZvbnQgZmFjZT0iTWVudUZvbnRfMDEiIHNpemU9IjQ4IiBjb2xvcj0iIzI4OGM5OSI+VWx0cmEg6YeO5o6S5pyNPC9mb250PjxpbWcgc3JjPSdpbWc6Ly9GYWtlTG9hZGluZ0ljb24ucG5nJyBoZWlnaHQ9IjQ4IiB3aWR0aD0iNDgiPjwvcD4=",
                            "message": "PHA+5pyN5Yqh5Zmo55SxIELnq5nkv5fluo8g5o+Q5L6bPGJyPuiBlOezu+aWueW8jzogY2VsZXN0ZXJ5ZGVAMTYzLmNvbTwvcD4KPHA+5L2/55So6K+l5qih57uE5piv5ZCm5Lya6KKr5bCB5Y+377yfPGJyPuS4jeS8muOAgmNsX3NlcnZlcl9yZWRpcmVjdG9yIOS7heWBmue9kee7nOivt+axgumHjeWumuWQke+8jOS4jea2ieWPiuWGheWtmOS/ruaUueaIluS9nOW8iuihjOS4uu+8jOato+W4uOS9v+eUqOS4jeS8muinpuWPkeWPjeS9nOW8iuajgOa1i+OAguS9huivt+azqOaEj++8jOS7u+S9leesrOS4ieaWueW3peWFt+mDveWtmOWcqOS4gOWumumjjumZqe+8jOivt+iHquihjOivhOS8sOOAgjwvcD4KPHA+6L+e5o6l6YeN5a6a5ZCR55SxIGNsX3NlcnZlcl9yZWRpcmVjdG9yIOaPkOS+mzxicj7ljp/kvZzogIU6IENodXJjaCBHdWFyZDxicj7mlK/mjIHljp/kvZzogIU6IGtvLWZpLmNvbS9jaHVyY2hndWFyZDwvcD4=",
                            "start_at": {"seconds": now},
                        },
                        {
                            "seq_id": 1,
                            "page_id": 1,
                            "lang_code": "SIMPLIFIED_CHINESE",
                            "abstract": "",
                            "message": "PHA+PGZvbnQgZmFjZT0iTWVudUZvbnRfMDEiIHNpemU9IjE4Ij7mnKzmnI3nm67liY3lpITkuo7liJ3niYjmtYvor5XpmLbmrrXvvIzljLnphY3nm7jlhbPlip/og73lsJrmnKrlrozlhajphY3nva7lrozlloTvvIzlj6/og73lh7rnjrDljLnphY3nrYnlvoXkuYXjgIHml6Dlk43lupTnrYnpl67popjvvIzov5jor7flpJrlpJrljIXmtrXvvIzlkI7nu63kvJrmoLnmja7ov5DooYzmg4XlhrXmjIHnu63kvJjljJbjgII8L2ZvbnQ+PC9wPg==",
                            "start_at": {"seconds": now},
                        },
                    ],
                    "to_player_msg": [],
                },
            })
        if req_type in self.API_LOCAL_SIMPLE:
            return self._send_json({"type": resp_type, "value": dict(self.API_LOCAL_SIMPLE[req_type])})

        # 匹配相关请求本地处理——fs-emu 不认识私服玩家 session，转发过去只会返回空数据
        # 导致客户端匹配 UI 卡住（取消按钮虚化）。本地返回空 value 让游戏自己处理。
        if req_type in self.API_MATCH_STUBS:
            log_line("api stub (match): %s -> %s (local, not proxying)" % (req_type, resp_type))
            return self._send_json({"type": resp_type, "value": dict(self.API_MATCH_STUBS[req_type])})

        # 未知类型：优先转发学习，拿不到再回通用空信封
        if PROXY_UPSTREAM:
            return self._proxy("POST", body)
        if resp_type:
            log_line("api stub (generic): %s -> %s {}" % (req_type, resp_type))
            return self._send_json({"type": resp_type, "value": {}})
        self._send_json({"status": "ok"})

    def handle_game_protocol(self, method):
        """/redirect 及一切未实现路径：抓包 + 代理转发到 fs-emu 学习协议。"""
        body = self._capture(method)
        self._register_from_game_login(body)
        if PROXY_UPSTREAM:
            self._proxy(method, body)
        else:
            self._send_json({"status": "ok"})

    def _register_from_game_login(self, body):
        """从游戏登录请求(POST /redirect)的 emu_auth_ticket 里提取
        steam_id / shard，把玩家登记进匹配池。

        emu_auth_ticket 由客户端模组(SendRequest hook)注入：
          {"type":"EmulatedAuthTicket","value":{"shard":"C_XXX;<hash>;",...}}
        """
        if not body:
            return
        try:
            data = json.loads(body)
            ticket = data.get("emu_auth_ticket") or {}
            value = ticket.get("value") or {}
            steam_id = str(value.get("steam_id", ""))
            shard_tag = value.get("shard", "")
            if not steam_id:
                return
            # shard_tag 形如 "C_Ultra;aff3fbaf248bd24e;"，直接整串当池键，
            # 天然包含 shard 名 + regbin 哈希双重隔离。
            pool = shard_tag or "(unknown)"
            evict_stale()
            with PLAYERS_LOCK:
                PLAYERS[steam_id] = {
                    "shard": pool, "pool": pool,
                    "mod_version": data.get("login_version", "?"),
                    "last_seen": time.time(),
                }
            log_line("game login: steam_id=%s pool=%s (pool online=%d)"
                     % (steam_id, pool, online_in_shard(pool)))
        except (json.JSONDecodeError, AttributeError):
            pass

    def _client_ip(self):
        """获取玩家真实 IP（支持 Nginx 反向代理的 X-Real-IP / X-Forwarded-For）。"""
        xri = self.headers.get("X-Real-IP", "")
        if xri:
            return xri
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def _proxy(self, method, body):
        """把请求原样转发给上游（fs-emu），记录响应并回传。

        这让游戏通过我们完成对真实服务器的登录（链路验证），同时
        requests.log 里会留下完整的协议双向记录（实现自己服务端的教材）。
        """
        import http.client
        upstream_url = PROXY_UPSTREAM + self.path
        log_line("--- proxying %s %s -> %s" % (method, self.path, upstream_url))
        try:
            if PROXY_UPSTREAM.startswith("https"):
                ctx = ssl._create_unverified_context()
                conn = http.client.HTTPSConnection(PROXY_HOST, PROXY_PORT, timeout=PROXY_TIMEOUT, context=ctx)
            else:
                conn = http.client.HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=PROXY_TIMEOUT)

            # 原样转发请求头（换掉 Host，hop-by-hop 头去掉）
            headers = {}
            for k, v in self.headers.items():
                lk = k.lower()
                if lk in ("host", "content-length", "connection", "keep-alive",
                          "proxy-authenticate", "proxy-authorization", "te",
                          "trailers", "transfer-encoding", "upgrade"):
                    continue
                headers[k] = v
            # 客户端没配上游密码时，由我们代为注入（fs-emu 校验 passkey 头）
            if PROXY_PASSKEY and not headers.get("passkey"):
                headers["passkey"] = PROXY_PASSKEY
            # fs-emu 按 User-Agent 识别 redirector 客户端：非原版 UA 一律
            # 400 {"code":"UNSUPPORTED_REDIRECTOR"}。转发时伪装成原版 UA，
            # 本地 SU 品牌标识不受影响。
            headers["User-Agent"] = "CL_Server_Redirector/0.0.4"
            conn.request(method, PROXY_BASE_PATH + self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            log_line("--- upstream response: HTTP %d (%d bytes)" % (resp.status, len(resp_body)))
            for k, v in resp.getheaders():
                log_line("    %s: %s" % (k, v))
            try:
                log_line("    body: " + resp_body.decode("utf-8", errors="replace"))
            except Exception:
                log_line("    body(%d bytes binary)" % len(resp_body))

            # Patch: replace fs-emu's client_ip (proxy server IP) with the
            # player's real IP so the game doesn't reject the session.
            try:
                data = json.loads(resp_body)
                if isinstance(data, dict) and "value" in data and "client_ip" in data["value"]:
                    real_ip = self._client_ip()
                    data["value"]["client_ip"] = real_ip
                    resp_body = json.dumps(data, separators=(",", ":")).encode("utf-8")
                    log_line("--- patched client_ip -> %s" % real_ip)
            except Exception:
                pass

            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("connection", "keep-alive", "transfer-encoding",
                                 "content-length", "content-encoding"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            conn.close()
        except Exception as e:
            log_line("--- proxy FAILED: %r" % (e,))
            # 客户端可能已断开，不再尝试写响应

    def log_message(self, fmt, *args):  # 关闭默认的 stderr 访问日志（自己记）
        pass


def main():
    global PASSKEY, PROXY_UPSTREAM, PROXY_HOST, PROXY_PORT, PROXY_BASE_PATH, PROXY_PASSKEY, SELFHOSTED, REPLICA
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0",
                    help="绑定地址，默认 0.0.0.0；配合 Nginx 反向代理时设为 127.0.0.1")
    ap.add_argument("--port", type=int, default=10901)
    ap.add_argument("--passkey", default="")
    ap.add_argument("--cert", default="", help="TLS 证书（可选，启用 HTTPS）")
    ap.add_argument("--key", default="", help="TLS 私钥（可选）")
    ap.add_argument("--proxy", default="",
                    help="学习代理上游，如 https://nightreign.fs-emu.net；未实现的请求转发给它并记录响应")
    ap.add_argument("--proxy-passkey", default="",
                    help="上游服务器的访问密码；客户端请求没带 passkey 时注入（fs-emu 必需）")
    ap.add_argument("--selfhosted", action="store_true",
                    help="自研协议模式：/redirect 本地签发会话，/api/ 本地应答（未知类型仍走 --proxy 学习）")
    ap.add_argument("--replica", action="store_true",
                    help="对照实验：/redirect 逐字节复刻 fs-emu 已验证的成功应答（需配合 --selfhosted）")
    args = ap.parse_args()
    PASSKEY = args.passkey
    SELFHOSTED = args.selfhosted
    REPLICA = args.replica

    if args.proxy:
        p = urlparse(args.proxy)
        PROXY_UPSTREAM = args.proxy.rstrip("/")
        PROXY_HOST = p.hostname
        PROXY_PORT = p.port or (443 if p.scheme == "https" else 80)
        PROXY_BASE_PATH = p.path.rstrip("/")
        PROXY_PASSKEY = args.proxy_passkey
        log_line("learning proxy enabled: upstream=%s (passkey %s)"
                 % (PROXY_UPSTREAM, "SET" if PROXY_PASSKEY else "EMPTY"))

    ThreadingHTTPServer.allow_reuse_address = True  # 类级别设置，避免 TIME_WAIT 等待
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True  # 残留请求线程不阻塞退出/重启
    scheme = "http"
    if args.cert and args.key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    log_line("listening on %s://%s:%d (passkey %s, selfhosted=%s)"
             % (scheme, args.host, args.port, "SET" if PASSKEY else "EMPTY", SELFHOSTED))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        global LOG_HANDLE
        if LOG_HANDLE:
            LOG_HANDLE.close()
            LOG_HANDLE = None


if __name__ == "__main__":
    main()
