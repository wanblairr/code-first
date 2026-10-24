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


def client_is_loopback(address):
    """Use the accepted socket peer, never a browser-supplied forwarding header."""
    try:
        parsed = ipaddress.ip_address(address)
        return bool((getattr(parsed, "ipv4_mapped", None) or parsed).is_loopback)
    except ValueError:
        return False


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
        server_version = "CafeDemo/3.7"

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

        def local_configuration_ok(self):
            if client_is_loopback(self.client_address[0]):
                return True
            self.reply(403, {"error": "接口设置与连接检查请在演示电脑上通过 127.0.0.1 打开工作台操作。"})
            return False

        def integration_state(self):
            result = dict(service.integrations_public())
            result["local_admin"] = client_is_loopback(self.client_address[0])
            return result

        def gmail_redirect_uri(self):
            return f"http://127.0.0.1:{self.server.server_port}/api/integrations/gmail/oauth/callback"

        def do_GET(self):
            if not self.host_ok():
                return self.reply(403, {"error": "无效主机名。"})
            path = urlsplit(self.path).path
            public = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/whatsapp_sandbox.js": ("whatsapp_sandbox.js", "text/javascript; charset=utf-8"),
                      "/gmail_callback.js": ("gmail_callback.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8"),
                      "/bom-local.js": ("bom-local.js", "text/javascript; charset=utf-8"),
                      "/bom-i18n.mjs": ("bom-i18n.mjs", "text/javascript; charset=utf-8"),
                      "/bom-local.css": ("bom-local.css", "text/css; charset=utf-8")}
            if path == "/api/integrations/gmail/oauth/callback":
                if not self.local_configuration_ok():
                    return
                # Cross-site top-level redirects do not carry a Strict cookie.
                # This shell exchanges nothing; same-origin JS clears the URL,
                # then completes with the owner session and CSRF protection.
                return self.reply(200, (WEB / "gmail_callback.html").read_bytes(), "text/html; charset=utf-8")
            if path in public:
                filename, mime = public[path]
                return self.reply(200, (WEB / filename).read_bytes(), mime)
            session = self.session()
            if not session:
                return self.reply(401, {"error": "请先以老板身份登录。"})
            try:
                if path == "/api/bom/state":
                    result = service.bom_state()
                    result["csrf"] = session["csrf"]
                    return self.reply(200, result)
                if path.startswith("/api/bom/drafts/"):
                    draft_id = path.removeprefix("/api/bom/drafts/")
                    if not draft_id or "/" in draft_id or not draft_id.startswith("BOM-DRAFT-"):
                        raise CafeError("BOM草稿编号无效。")
                    draft = service.bom().draft(draft_id)
                    fmt = parse_qs(urlsplit(self.path).query).get("format", ["txt"])[0]
                    if fmt not in ("txt", "eml"):
                        raise CafeError("草稿格式无效。")
                    return self.reply(200, draft["email_eml"] if fmt == "eml" else draft["body"],
                        "message/rfc822" if fmt == "eml" else "text/plain; charset=utf-8",
                        {"Content-Disposition": f'attachment; filename="purchase-{draft_id}.{fmt}"'})
                if path == "/api/state":
                    state = service.store.snapshot()
                    state.update(csrf=session["csrf"], timezone="Asia/Kuala_Lumpur", trigger_time="22:00",
                                 local_date=service.clock().astimezone(SHANGHAI).date().isoformat(), last_schedule_failure=service.last_schedule_failure)
                    inputs = service.inputs()
                    from cafe_catalog import CATEGORIES, TEMPLATES
                    state["catalog"] = dict(inputs["catalog"], categories=CATEGORIES, templates=TEMPLATES,
                                            suppliers=[{"supplier_id": s["supplier_id"], "name": s["name"]} for s in inputs["suppliers"]["suppliers"]])
                    state["readiness"] = service.readiness(inputs)
                    state["context"] = service.context_state(inputs)
                    state["integrations"] = self.integration_state()
                    state["ingredients"] = [{"ingredient_id": i["ingredient_id"], "name": i["name"],
                                              "unit": i["unit"], "pack_size": i["pack_size"],
                                              "purchase_unit": i.get("purchase_unit", "包"),
                                              "on_hand": sum(batch["quantity"] for batch in i["batches"])}
                                            for i in inputs["inventory"]["items"]]
                    for order in state["orders"]:
                        order["drafts"] = [{k: v for k, v in d.items() if k != "email_eml"} for d in service.store.drafts_for(order["id"])]
                    if getattr(service, "bom_enabled", False) is True:
                        bom = service.bom_state()
                        state["bom_enabled"] = True
                        state["bom_meta"] = {"revision": bom["revision"], "active_order_id": bom["active_order_id"], "context_signature": bom["context_signature"]}
                        state["context"] = service.bom().local_context_panel(bom)
                    return self.reply(200, state)
                if path == "/api/integrations":
                    return self.reply(200, self.integration_state())
                if path.startswith("/api/runpod/jobs/"):
                    job_id = path.removeprefix("/api/runpod/jobs/")
                    if not job_id or "/" in job_id:
                        raise CafeError("Runpod 任务编号无效。")
                    return self.reply(200, service.runpod_job(job_id))
                if path.startswith("/api/drafts/") and path.endswith("/whatsapp"):
                    draft_id = path.removeprefix("/api/drafts/").removesuffix("/whatsapp")
                    if not draft_id or "/" in draft_id:
                        raise CafeError("采购草稿编号无效。")
                    return self.reply(200, service.whatsapp_draft(draft_id))
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

        def reject_post(self, code, message):
            # Windows may reset a closing socket with an unread POST body.
            # Drain only a bounded, unambiguous body; never parse it or dispatch
            # business work. An incomplete rejected request gets only 0.5 s.
            lengths = self.headers.get_all("Content-Length", [])
            if (not self.headers.get_all("Transfer-Encoding", []) and len(lengths) == 1
                    and lengths[0].isascii() and lengths[0].isdigit()
                    and len(lengths[0]) <= 5 and int(lengths[0]) <= 65536):
                deadline = time.monotonic() + 0.5
                remaining = int(lengths[0])
                try:
                    while remaining:
                        budget = deadline - time.monotonic()
                        if budget <= 0:
                            break
                        self.connection.settimeout(budget)
                        chunk = self.rfile.read1(min(remaining, 8192))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except OSError:
                    pass
                finally:
                    self.connection.settimeout(15)
            self.close_connection = True
            return self.reply(code, {"error": message})

        def do_POST(self):
            if not self.host_ok():
                return self.reject_post(403, "无效主机名。")
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host"):
                return self.reject_post(403, "请求来源无效。")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.reject_post(415, "需要 JSON 请求。")
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
                        sessions[key] = {"csrf": secrets.token_urlsafe(32), "oauth_binding": secrets.token_urlsafe(32), "expires": tick + 8 * 3600}
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
                if path.startswith("/api/bom/"):
                    action = path.removeprefix("/api/bom/")
                    if action == "sale":
                        result = service.bom().sale(data)
                    elif action == "undo":
                        result = service.bom().undo(data)
                    elif action == "prices":
                        result = service.bom().set_prices(data)
                    elif action == "history":
                        result = service.bom().set_history(data)
                    elif action == "extras":
                        result = service.bom().set_extras(data)
                    elif action == "suggest":
                        result = service.bom().suggest()
                    elif action == "approve":
                        result = service.bom().approve(data)
                    elif action == "preview":
                        result = service.bom().preview(data)
                    elif action == "context":
                        result = service.bom().set_context({key: value for key, value in data.items() if key != "request_id"})
                    elif action.startswith("drafts/") and action.endswith("/gmail"):
                        draft_id = action.removeprefix("drafts/").removesuffix("/gmail")
                        service.bom_approved_draft(draft_id)
                        result = service.create_gmail_draft(draft_id)
                    else:
                        return self.reply(404, {"error": "BOM接口不存在。"})
                    return self.reply(200, result)
                if path.startswith("/api/integrations/gmail/oauth/"):
                    if not self.local_configuration_ok():
                        return
                    action = path.removeprefix("/api/integrations/gmail/oauth/")
                    if action in ("start", "complete") and urlsplit("http://" + self.headers.get("Host", "")).hostname != "127.0.0.1":
                        raise CafeError("请在同一浏览器使用 127.0.0.1 打开工作台并登录，再连接 Gmail。")
                    if action == "client":
                        result = service.import_gmail_oauth_client(data["client_json"])
                    elif action == "start":
                        result = service.start_gmail_oauth(session["oauth_binding"], self.gmail_redirect_uri())
                    elif action == "complete":
                        result = service.complete_gmail_oauth(data["code"], data["state"], session["oauth_binding"], self.gmail_redirect_uri())
                    elif action == "disconnect":
                        result = service.disconnect_gmail_oauth()
                    else:
                        return self.reply(404, {"error": "OAuth 接口不存在。"})
                elif path == "/api/integrations/config":
                    if not self.local_configuration_ok():
                        return
                    result = service.configure_integrations(data)
                elif path in ("/api/integrations/runpod/check", "/api/integrations/gmail/check", "/api/integrations/whatsapp/check"):
                    if not self.local_configuration_ok():
                        return
                    result = service.check_integration(path.split("/")[3])
                elif path == "/api/integrations/whatsapp/test":
                    if not self.local_configuration_ok():
                        return
                    result = service.test_whatsapp_message(data.get("recipient"), data.get("opt_in"), data.get("confirmed"))
                elif path.startswith("/api/drafts/") and path.endswith(("/whatsapp/preview", "/whatsapp/send")):
                    action = path.rsplit("/", 1)[1]
                    draft_id = path.removeprefix("/api/drafts/").removesuffix("/whatsapp/" + action)
                    if not draft_id or "/" in draft_id:
                        raise CafeError("采购草稿编号无效。")
                    if action == "preview":
                        result = service.preview_whatsapp_message(draft_id, data.get("recipient"), data.get("opt_in"))
                    else:
                        result = service.send_whatsapp_message(draft_id, data.get("recipient"), data.get("preview_id"), data.get("opt_in"), data.get("confirmed"))
                elif path.startswith("/api/drafts/") and path.endswith(("/whatsapp-sandbox/preview", "/whatsapp-sandbox/send")):
                    action = path.rsplit("/", 1)[1]
                    draft_id = path.removeprefix("/api/drafts/").removesuffix("/whatsapp-sandbox/" + action)
                    if not draft_id or "/" in draft_id:
                        raise CafeError("采购草稿编号无效。")
                    allowed = {"opt_in", "window_confirmed", "contact_at"} | ({"preview_id", "confirmed"} if action == "send" else set())
                    if set(data) - allowed:
                        raise CafeError("本人沙盒只允许使用本机配置的固定收件标识。")
                    if action == "preview":
                        result = service.preview_whatsapp_sandbox(draft_id, data.get("opt_in"), data.get("window_confirmed"), data.get("contact_at"))
                    else:
                        result = service.send_whatsapp_sandbox(draft_id, data.get("preview_id"), data.get("opt_in"), data.get("window_confirmed"), data.get("confirmed"), data.get("contact_at"))
                elif path == "/api/runpod/review":
                    result = service.start_runpod_review(data["id"], data["revision"], data["input_hash"])
                elif path.startswith("/api/drafts/") and path.endswith("/gmail"):
                    draft_id = path.removeprefix("/api/drafts/").removesuffix("/gmail")
                    if not draft_id or "/" in draft_id:
                        raise CafeError("采购草稿编号无效。")
                    result = service.create_gmail_draft(draft_id)
                elif path == "/api/catalog":
                    result = service.save_catalog(data)
                elif path == "/api/trigger":
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
