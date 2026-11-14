"""Phase 10: audit integrity and viewer, history parts, security headers and input safety."""
import csv
import io
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import server
from app.storage import ExcelStorageService, XlsxCodec, WORKBOOKS
from app.service import TMExpertService
from test_api import Client, ADMIN_EMAIL, ADMIN_PASSWORD

STEP = [{"action": "Do it", "expected_result": "Done"}]


class StorageHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ExcelStorageService(self.tmp.name)
        TMExpertService(self.store).bootstrap()

    def tearDown(self): self.tmp.cleanup()

    def test_audit_entries_form_a_verifiable_chain_and_tampering_is_detected(self):
        result = self.store.verify_audit()
        self.assertTrue(result["ok"]); self.assertGreater(result["checked"], 5); self.assertEqual(result["legacy"], 0)
        rows = self.store._read_all("audit")
        rows[3]["action"] = "Approve"  # edit an entry outside TMExpert
        path = self.store.path_for("audit")
        XlsxCodec.write(path, WORKBOOKS["audit"][1], rows)
        reopened = ExcelStorageService(self.tmp.name)
        broken = reopened.verify_audit()
        self.assertFalse(broken["ok"]); self.assertEqual(broken["broken_at"]["position"], 4)

    def test_entries_written_before_tracking_are_reported_as_legacy(self):
        rows = self.store._read_all("audit")
        for row in rows[:3]: row["hash"] = ""
        legacy = [{k: v for k, v in r.items()} for r in rows[:3]]
        path = self.store.path_for("audit")
        XlsxCodec.write(path, WORKBOOKS["audit"][1], legacy)
        reopened = ExcelStorageService(self.tmp.name)
        reopened.record_event("Sign-in", "users", "u", {}, "system")
        result = reopened.verify_audit()
        self.assertEqual((result["ok"], result["legacy"], result["checked"]), (True, 3, 1))

    def test_growing_history_is_split_into_parts_and_read_back_as_one(self):
        self.store.segment_rows = 5
        project = self.store.list("projects")["items"][0]
        for i in range(12): self.store.record_event("Ping", "projects", project["id"], {"n": i}, "system")
        folder = Path(self.tmp.name) / "history" / "Audit_History_archive"
        parts = sorted(folder.glob("Audit_History_*.xlsx"))
        self.assertGreaterEqual(len(parts), 2)
        total = len(self.store._load("audit")[2])
        reopened = ExcelStorageService(self.tmp.name)
        reopened.segment_rows = 5
        self.assertEqual(len(reopened._load("audit")[2]), total)
        self.assertTrue(reopened.verify_audit()["ok"])
        pings = [r["new_value"]["n"] for r in reopened.list("audit", filters={"action": "Ping"}, page_size=100)["items"]]
        self.assertEqual(pings, list(range(12)))  # order kept across parts
        with self.assertRaises(ValueError): reopened.update("audit", pings and reopened.list("audit")["items"][0]["id"], {"action": "x"})

    def test_an_interrupted_move_heals_itself(self):
        self.store.segment_rows = 4
        for i in range(5): self.store.record_event("Ping", "projects", "p", {"n": i}, "system")
        folder = Path(self.tmp.name) / "history" / "Audit_History_archive"
        part = sorted(folder.glob("*.xlsx"))[-1]
        # Simulate a crash after the part was written but before the current workbook was emptied.
        headers, rows = XlsxCodec.read(part)
        XlsxCodec.write(self.store.path_for("audit"), headers, rows)
        reopened = ExcelStorageService(self.tmp.name)
        ids = [r["id"] for r in reopened._load("audit")[2]]
        self.assertEqual(len(ids), len(set(ids)))
        reopened.record_event("Ping", "projects", "p", {"n": 99}, "system")
        self.assertTrue(reopened.verify_audit()["ok"])

    def test_writer_round_trips_text_exactly(self):
        path = Path(self.tmp.name) / "rt.xlsx"
        values = {"a": "  leading and trailing  ", "b": "", "c": "line1\nline2", "d": "x\x01y", "e": "<b>&amp;</b>", "f": "=SUM(A1)"}
        XlsxCodec.write(path, tuple(values), [values])
        headers, rows = XlsxCodec.read(path)
        self.assertEqual(rows[0], {"a": "  leading and trailing  ", "b": "", "c": "line1\nline2", "d": "xy", "e": "<b>&amp;</b>", "f": "=SUM(A1)"})
        self.assertNotIn(b'r="B2"', __import__("zipfile").ZipFile(path).read("xl/worksheets/sheet1.xml"))  # empty cells are not written

    def test_unsafe_record_ids_are_refused(self):
        project = self.store.list("projects")["items"][0]
        with self.assertRaisesRegex(ValueError, "Record IDs"):
            self.store.create("requirements", {"id": "x');alert(1)//", "project_id": project["id"], "key": "R-1", "title": "t", "status": "Draft"})


class ServerHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = server.make_server("127.0.0.1", 0, self.tmp.name)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.store = self.httpd.app.store
        self.admin = Client(self.base)
        self.admin.login(ADMIN_EMAIL, "change-me")
        self.admin.call("POST", "/api/auth/change-password", {"current_password": "change-me", "new_password": ADMIN_PASSWORD})
        self.project = self.store.list("projects")["items"][0]
        self.folder = self.store.list("folders", filters={"project_id": self.project["id"]})["items"][0]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(5); self.tmp.cleanup()

    def test_one_audit_write_per_request_and_it_happens_before_the_reply(self):
        calls = []
        original = self.store._write_audit
        self.store._write_audit = lambda entries: (calls.append(len(entries)), original(entries))[1]
        status, case = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": self.folder["id"], "title": "Batched", "priority": "Low", "steps": STEP})
        self.assertEqual(status, 201)
        self.assertEqual(len(calls), 1); self.assertGreaterEqual(calls[0], 3)  # case + steps + version in one write
        entities = {r["entity"] for r in self.store.list("audit", page_size=10000)["items"] if r["record_id"] == case["id"] or (r.get("new_value") or {}).get("test_case_id") == case["id"]}
        self.assertTrue({"test_cases", "test_steps", "test_case_versions"} <= entities)

    def test_archiving_is_logged_as_archive_and_the_audit_log_is_readable(self):
        case = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": self.folder["id"], "title": "To archive", "priority": "Low", "steps": STEP})[1]
        self.admin.call("PUT", f"/api/test_cases/{case['id']}", {"title": "Renamed before archive"})
        self.admin.call("DELETE", f"/api/test_cases/{case['id']}")
        status, log = self.admin.call("GET", f"/api/audit-log?project_id={self.project['id']}&entity=test_cases&q={case['id']}")
        self.assertEqual(status, 200)
        actions = [item["action"] for item in log["items"]]
        self.assertEqual(actions[:3], ["Archive", "Update", "Create"])  # newest first
        update = log["items"][1]
        self.assertIn({"field": "title", "before": "To archive", "after": "Renamed before archive"}, update["changes"])
        self.assertEqual((update["actor_name"], update["entity_label"], update["tracked"]), ("Avery Chen", "Test case", True))
        self.assertIn("Archive", log["facets"]["actions"])
        self.assertTrue(self.admin.call("GET", "/api/audit-log/verify")[1]["ok"])
        status, blob = self.admin.call("GET", f"/api/audit-log/export.xlsx?project_id={self.project['id']}&entity=test_cases")
        headers, rows = XlsxCodec.read_bytes(blob)
        self.assertEqual((status, headers[0], headers[2]), (200, "When", "Action"))
        self.assertTrue(any("title: To archive → Renamed before archive" in r["Changes"] for r in rows))

    def test_team_members_cannot_read_the_audit_log(self):
        self.admin.call("POST", "/api/users", {"name": "Tara", "email": "tara@x.local", "role": "Team Member", "password": "Temp-Pass-1"})
        team = Client(self.base); team.login("tara@x.local", "Temp-Pass-1")
        team.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "Team-Pass-22"})
        for path in ("/api/audit-log", "/api/audit-log/verify", "/api/audit-log/export.xlsx", "/api/audit"):
            self.assertEqual(team.call("GET", f"{path}?project_id={self.project['id']}")[0], 403, path)

    def test_security_headers_are_sent(self):
        for path in ("/", "/api/health"):
            with urllib.request.urlopen(self.base + path) as response:
                headers = response.headers
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
            self.assertEqual((headers["X-Frame-Options"], headers["X-Content-Type-Options"]), ("DENY", "nosniff"))

    def test_injection_style_ids_and_csv_formulas_are_neutralised(self):
        status, body = self.admin.call("POST", "/api/test-cases", {"id": "x');alert(1)//", "project_id": self.project["id"], "folder_id": self.folder["id"], "title": "Evil id", "priority": "Low", "steps": STEP})
        self.assertEqual(status, 400); self.assertIn("Record IDs", body["error"])
        self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": self.folder["id"], "title": "=HYPERLINK(\"http://evil\",\"x\")", "priority": "Low", "steps": STEP})
        status, blob = self.admin.call("GET", f"/api/export/test-cases?project_id={self.project['id']}")
        titles = [row["title"] for row in csv.DictReader(io.StringIO(blob.decode()))]
        self.assertIn("'=HYPERLINK(\"http://evil\",\"x\")", titles)

    def test_sessions_expire_after_seven_days_even_when_active(self):
        auth = self.httpd.app.auth
        for session in auth.sessions.values(): session["started"] = time.time() - 8 * 24 * 3600
        self.assertEqual(self.admin.call("GET", "/api/dashboard")[0], 401)


if __name__ == "__main__": unittest.main()


class NetworkSharingTests(unittest.TestCase):
    def test_default_passwords_block_network_sharing(self):
        import os, subprocess, sys
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "TMEXPERT_HOST": "0.0.0.0", "TMEXPERT_PORT": "0", "TMEXPERT_DATA_DIR": tmp, "PYTHONIOENCODING": "utf-8"}
            root = Path(__file__).resolve().parent.parent
            result = subprocess.run([sys.executable, "server.py"], cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("NOT shared on the network", result.stdout)
            self.assertIn("avery@tmexpert.local", result.stdout)

    def test_default_password_detection_and_secure_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd = server.make_server("127.0.0.1", 0, tmp)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
            try:
                self.assertEqual(server.accounts_with_default_password(httpd.app), ["avery@tmexpert.local"])
                admin = Client(f"http://127.0.0.1:{httpd.server_address[1]}")
                admin.login(ADMIN_EMAIL, "change-me")
                admin.call("POST", "/api/auth/change-password", {"current_password": "change-me", "new_password": ADMIN_PASSWORD})
                self.assertEqual(server.accounts_with_default_password(httpd.app), [])
                httpd.tls = True  # as when TMEXPERT_CERT/TMEXPERT_KEY are set
                request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/auth/login", data=b'{"email":"avery@tmexpert.local","password":"' + ADMIN_PASSWORD.encode() + b'"}', method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request) as response:
                    self.assertIn("Secure", response.headers["Set-Cookie"])
            finally:
                httpd.shutdown(); httpd.server_close(); thread.join(5)

    def test_team_links_are_found(self):
        addresses = server.network_addresses()
        self.assertTrue(all(not a.startswith("127.") for a in addresses))
        self.assertFalse(server.is_local_only("0.0.0.0")); self.assertTrue(server.is_local_only("127.0.0.1"))
