"""First-start persistence and no-network launcher acceptance."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cafe_start
from cafe_service import CafeService, SHANGHAI


class StartupV37Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="cafe-v37-start-")
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)

    def test_first_start_password_is_salted_and_never_printed(self):
        output=io.StringIO()
        with redirect_stdout(output):
            result=cafe_start.configure(self.base,read_password=Mock(side_effect=["fixture-password-only"]*2),interactive=True)
        self.assertEqual(result,0)
        self.assertNotIn("fixture-password-only",output.getvalue())
        raw=(self.base/"auth_config.json").read_text(encoding="utf-8")
        self.assertIn("pbkdf2_sha256",raw)
        self.assertNotIn("fixture-password-only",raw)

    def test_existing_password_is_preserved_without_prompt(self):
        target=self.base/"auth_config.json"
        target.write_text("existing verifier placeholder",encoding="utf-8")
        prompt=Mock(side_effect=AssertionError("Do not replace existing password"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cafe_start.configure(self.base,read_password=prompt,interactive=True),0)
        self.assertEqual(target.read_text(encoding="utf-8"),"existing verifier placeholder")
        prompt.assert_not_called()

    def test_mismatch_or_noninteractive_start_does_not_save_auth(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cafe_start.configure(self.base,interactive=False),2)
            self.assertEqual(cafe_start.configure(self.base,read_password=Mock(side_effect=["fixture-password-a","fixture-password-b"]),interactive=True),2)
        self.assertFalse((self.base/"auth_config.json").exists())

    def test_bom_scheduler_never_resumes_legacy_paid_jobs(self):
        service=Mock()
        service.stop.wait.return_value=True
        cafe_start.bom_scheduler_loop(service)
        service._cloud.assert_not_called()
        service.scheduler_tick.assert_not_called()

    def test_startup_preserves_stock_history_and_context_without_any_provider_request(self):
        instant=datetime(2026,10,24,12,tzinfo=SHANGHAI)
        service=CafeService(self.base,clock=lambda:instant)
        app=service.bom()
        app.set_context({"weather_mode":"sunny","holiday_mode":"ordinary"})
        app.set_history({"revision":0,"date":"2026-10-23","rows":[{"productId":"BV-013","size":"中杯","quantity":70}]})
        app.sale({"productId":"BV-013","size":"中杯","quantity":1,"revision":0,"request_id":"startup-sale"})
        old=app.state()
        server=Mock(server_port=8790)
        with patch.object(cafe_start,"configure",return_value=0), patch("cafe_app.prepare_context_demo") as prepare, \
             patch("cafe_service.CafeService",return_value=service), patch("cafe_web.make_server",return_value=server), \
             patch.object(service,"_cloud",side_effect=AssertionError("Do not resume legacy cloud jobs")), \
             patch("urllib.request.OpenerDirector.open",side_effect=AssertionError("No startup network calls")), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(cafe_start.main(["--base",str(self.base),"--no-browser"]),0)
        prepare.assert_called_once_with(self.base)
        after=app.state()
        self.assertEqual(after["stock"],old["stock"])
        self.assertEqual(after["sales_history"],old["sales_history"])
        self.assertEqual(after["bom_context_settings"],old["bom_context_settings"])
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()


if __name__=="__main__": unittest.main()
