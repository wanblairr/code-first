"""Local/LAN synthetic demo UI; use a production HTTPS server for real deployment."""
from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import socket
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cafe_core import CafeError
from cafe_service import SHANGHAI
from cafe_auth import load_auth_policy, verify_owner_password

WEB = Path(__file__).parent / "web_v3"


def host_is_allowed(authority, local_ip, port, trusted_names, lan=False):
    """Validate an HTTP authority without resolving an untrusted hostname.

    For a wildcard LAN listener, the accepted socket knows the actual local NIC
    address even when hostname lookup omits that NIC. Only that numeric address
    is added per connection; this never trusts an arbitrary private IP or domain.
    """
    if not isinstance(authority, str) or not authority or len(authority) > 512:
        return False
    if any(c.isspace() or ord(c) < 32 or c in "/\\?#@" for c in authority):
        return False
    try:
        parsed = urlsplit("http://" + authority)
        name = parsed.hostname
        if not name or (parsed.port if parsed.port is not None else 80) != port:
            return False
        if name in trusted_names:
            return True
        address = ipaddress.ip_address(name)
        if address in (ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")):
            return True
        return lan and not address.is_unspecified and address == ipaddress.ip_address(local_ip)
    except ValueError:
        return False


def make_server(service, host="127.0.0.1", port=8765, password_config=None):
    auth_policy = load_auth_policy(password_config)
    sessions, attempts = {}, {}
    auth_lock = threading.RLock()
    trusted_names = {"localhost", socket.gethostname().lower()}
    lan = host not in ("127.0.0.1", "localhost", "::1")

    class Handler(BaseHTTPRequestHandler):
        server_version = "CafeDemo/3.2"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            # Do not log password bodies, cookies or query strings.
            pass

        def reply(self, code, body, mime="application/json; charset=utf-8", headers=None):
            if not isinstance(body, (str, bytes)):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False)
            payload = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def host_ok(self):
            hosts = self.headers.get_all("Host", [])
            return len(hosts) == 1 and host_is_allowed(
                hosts[0], self.connection.getsockname()[0], self.server.server_port, trusted_names, lan)

        def session(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                key = cookie["cafe_session"].value
            except (KeyError, ValueError, CookieError):
                return None
            with auth_lock:
                result = sessions.get(key)
                if result and result["expires"] > time.monotonic():
                    return result
                sessions.pop(key, None)
            return None

        def do_GET(self):
            if not self.host_ok():
                return self.reply(403, {"error": "无效主机名。"})
            path = urlsplit(self.path).path
            public = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in public:
                filename, mime = public[path]
                return self.reply(200, (WEB / filename).read_bytes(), mime)
            session = self.session()
            if not session:
                return self.reply(401, {"error": "请先以老板身份登录。"})
            try:
                if path == "/api/state":
                    state = service.store.snapshot()
                    state.update(csrf=session["csrf"], timezone="Asia/Kuala_Lumpur", trigger_time="22:00",
                                 local_date=service.clock().astimezone(SHANGHAI).date().isoformat(), last_schedule_failure=service.last_schedule_failure)
                    inputs = service.inputs()
                    state["readiness"] = service.readiness(inputs)
                    state["context"] = service.context_state(inputs)
                    state["ingredients"] = [{"ingredient_id": i["ingredient_id"], "name": i["name"],
                                              "unit": i["unit"], "pack_size": i["pack_size"],
                                              "purchase_unit": i.get("purchase_unit", "包"),
                                              "on_hand": sum(batch["quantity"] for batch in i["batches"])}
                                            for i in inputs["inventory"]["items"]]
                    for order in state["orders"]:
                        order["drafts"] = [{k: v for k, v in d.items() if k != "email_eml"} for d in service.store.drafts_for(order["id"])]
                    return self.reply(200, state)
                if path.startswith("/draft/"):
                    draft_id = path.removeprefix("/draft/")
                    draft = service.store.draft(draft_id)
                    fmt = parse_qs(urlsplit(self.path).query).get("format", ["txt"])[0]
                    if fmt not in ("txt", "eml"):
                        raise CafeError("草稿格式无效。")
                    content = draft["email_eml"] if fmt == "eml" else draft["body"]
                    return self.reply(200, content, "message/rfc822" if fmt == "eml" else "text/plain; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="purchase-{draft_id}.{fmt}"'})
                return self.reply(404, {"error": "页面不存在。"})
            except (CafeError, KeyError, TypeError, ValueError, OSError) as error:
                return self.reply(400, {"error": str(error)})

        def do_POST(self):
            if not self.host_ok():
                return self.reply(403, {"error": "无效主机名。"})
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host"):
                return self.reply(403, {"error": "请求来源无效。"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.reply(415, {"error": "需要 JSON 请求。"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    raise CafeError("请求大小无效。")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise CafeError("需要 JSON 对象。")
                path = urlsplit(self.path).path
                if path == "/api/login":
                    address = self.client_address[0]
                    tick = time.monotonic()
                    with auth_lock:
                        prior = [t for t in attempts.get(address, []) if tick - t < 300]
                        if len(prior) >= 10:
                            return self.reply(429, {"error": "尝试过多，请 5 分钟后重试。"})
                        supplied = data.get("password", "")
                        if not verify_owner_password(supplied, auth_policy):
                            attempts[address] = prior + [tick]
                            return self.reply(401, {"error": "访问密码错误，请重新输入。"})
                        attempts.pop(address, None)
                        # Expired entries are pruned and active sessions bounded.
                        for key in list(sessions):
                            if sessions[key]["expires"] <= tick:
                                sessions.pop(key)
                        if len(sessions) >= 100:
                            sessions.pop(next(iter(sessions)))
                        key = secrets.token_urlsafe(32)
                        sessions[key] = {"csrf": secrets.token_urlsafe(32), "expires": tick + 8 * 3600}
                    service.store.log("owner_login", {}, actor="owner")
                    return self.reply(200, {"ok": True}, headers={"Set-Cookie": f"cafe_session={key}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800"})
                session = self.session()
                if not session:
                    return self.reply(401, {"error": "请先登录。"})
                if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                    return self.reply(403, {"error": "审批令牌无效，请刷新页面。"})
                if path == "/api/logout":
                    cookie = SimpleCookie(self.headers.get("Cookie", ""))
                    with auth_lock:
                        sessions.pop(cookie["cafe_session"].value, None)
                    return self.reply(200, {"ok": True}, headers={"Set-Cookie": "cafe_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
                if path == "/api/trigger":
                    result = service.trigger("manual")
                elif path == "/api/stockout":
                    result = service.stockout(data["ingredient_id"])
                elif path == "/api/context/weather":
                    result = service.refresh_weather()
                elif path == "/api/context/scenario":
                    result = service.scenario(data)
                elif path == "/api/approve":
                    if type(data.get("acknowledged")) is not bool:
                        raise CafeError("核对确认字段无效。")
                    result = service.approve(data["id"], data["revision"], data["input_hash"], data.get("quantities", {}), data["acknowledged"])
                elif path == "/api/preview":
                    result = service.preview(data["id"], data["revision"], data["input_hash"], data.get("quantities", {}))
                elif path == "/api/reject":
                    service.store.reject(data["id"], data["revision"], data.get("reason", ""))
                    result = {"ok": True}
                else:
                    return self.reply(404, {"error": "接口不存在。"})
                return self.reply(200, result)
            except (CafeError, KeyError, TypeError, ValueError, OSError) as error:
                return self.reply(400, {"error": str(error)})

    return ThreadingHTTPServer((host, port), Handler)
