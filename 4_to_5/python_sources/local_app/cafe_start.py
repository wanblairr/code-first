"""Start the local v3.7 workbench without external provider calls.

Source may live on a share; CAFE_DEMO_BASE selects a SQLite-capable local folder.
Existing snapshots, stock, approvals and API configuration are never reset.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys
import threading
import webbrowser

APP = Path(__file__).resolve().parent
ROOT = APP.parent
sys.path.insert(0, str(APP))


import getpass
import json
from cafe_auth import make_auth_policy


def configure(folder=APP, read_password=None, interactive=None):
    config=Path(folder)/'auth_config.json'
    if config.exists():
        print('Existing local password configuration retained.')
        return 0
    if interactive is None: interactive=sys.stdin.isatty()
    if not interactive:
        print('Open this script in a local interactive terminal to choose a password.')
        return 2
    read_password=read_password or getpass.getpass
    password=read_password('Choose local workbench password (8-128 chars): ')
    repeated=read_password('Confirm password: ')
    if password!=repeated or not 8<=len(password)<=128:
        print('Passwords must match and contain 8-128 characters. Nothing was saved.')
        return 2
    policy=make_auth_policy(password)
    temporary=config.with_name('auth_config.setup.tmp')
    temporary.write_text(json.dumps(policy,indent=2)+'\n',encoding='utf-8')
    os.replace(temporary,config)
    print('Local password saved as a salted verifier. No password is displayed.')
    return 0


def bom_scheduler_loop(service):
    """Wait before local nightly checks; do not resume legacy cloud requests."""
    while not service.stop.wait(30):
        try:
            service.scheduler_tick()
            service.last_schedule_failure = None
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if detail != service.last_schedule_failure:
                service.store.log("scheduled_trigger_failed", {"error": detail})
                service.last_schedule_failure = detail


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cafe Replenishment v3.7 local workbench")
    parser.add_argument("--base", default=os.environ.get("CAFE_DEMO_BASE") or str(APP))
    parser.add_argument("--port", type=int, default=os.environ.get("CAFE_PORT") or "8790")
    parser.add_argument("--lan", action="store_true", help="Same-Wi-Fi demo; local-only by default")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--setup-only", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        print("Python 3.11 or later is required.", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("Port must be between 1 and 65535.", file=sys.stderr)
        return 2
    if configure(APP):
        return 2
    if args.setup_only:
        return 0
    from cafe_app import prepare_context_demo
    from cafe_service import CafeService
    from cafe_web import make_server
    from cafe_core import CafeError
    import sqlite3
    base = Path(args.base).expanduser().resolve()
    service = None
    server = None
    try:
        prepare_context_demo(base)
        service = CafeService(base)
        service.bom_enabled = True
        app = service.bom()
        # The first local demo is explicitly simulated. Keep every saved choice.
        if not app.settings_path.exists():
            app.set_context({"weather_mode": "rain", "holiday_mode": "auto"})
        if not app.state().get("proposal"):
            try:
                app.suggest()
            except CafeError as exc:
                print("Initial suggestion needs attention in the workbench: " + str(exc))
        server = make_server(service, "0.0.0.0" if args.lan else "127.0.0.1", args.port)
        url = f"http://127.0.0.1:{server.server_port}/"
        print("Cafe Replenishment v3.7: " + url, flush=True)
        print("Records: " + str(service.runtime), flush=True)
        print("No Runpod/Gmail/WhatsApp calls on startup. Keep this window open; Ctrl+C stops it.", flush=True)
        print("Nightly 22:00 checks remain enabled; live-weather mode may refresh weather on schedule.", flush=True)
        if args.lan:
            print("Same-Wi-Fi phone: use this PC's LAN IPv4 and the above port. API settings require 127.0.0.1.", flush=True)
        worker = threading.Thread(target=bom_scheduler_loop, args=(service,), daemon=True)
        worker.start()
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            service.stop.set()
            server.server_close()
            worker.join(timeout=2)
        return 0
    except (CafeError, OSError, ValueError, sqlite3.Error) as exc:
        if service:
            service.stop.set()
        if server:
            server.server_close()
        print("Could not start: " + str(exc), file=sys.stderr)
        print("Check the port, Python version, password configuration and local database folder. Existing data was preserved.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
