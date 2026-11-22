"""Plans, reusable suites, defects, retest, release readiness and requirement coverage."""
import tempfile
import threading
import unittest

import server
from app.storage import ExcelStorageService, XlsxCodec, WORKBOOKS
from test_api import Client, ADMIN_EMAIL, ADMIN_PASSWORD

STEP = [{"action": "Do it", "expected_result": "Done"}]


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = server.make_server("127.0.0.1", 0, self.tmp.name)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.store = self.httpd.app.store
        self.admin = Client(self.base)
        self.admin.login(ADMIN_EMAIL, "change-me")
        self.admin.call("POST", "/api/auth/change-password", {"current_password": "change-me", "new_password": ADMIN_PASSWORD})
        self.project = self.store.list("projects")["items"][0]; self.pid = self.project["id"]
        self.login = self.module("Login"); self.pay = self.module("Payments")
        self.case = {}
        for key, folder in [("L1", self.login), ("L2", self.login), ("P1", self.pay), ("P2", self.pay)]:
            created = self.admin.call("POST", "/api/test-cases", {"project_id": self.pid, "folder_id": folder["id"], "title": f"TC_{key} – Scenario {key}", "priority": "High", "steps": STEP})[1]
            self.case[key] = self.admin.call("POST", f"/api/test-cases/{created['id']}/transition", {"status": "Approved"})[1]
        user = self.admin.call("POST", "/api/users", {"name": "Arun", "email": "arun@x.local", "role": "Team Member", "password": "Temp-Pass-1"})[1]
        self.arun_id = user["id"]
        self.arun = Client(self.base); self.arun.login("arun@x.local", "Temp-Pass-1")
        self.arun.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "Team-Pass-22"})

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(5); self.tmp.cleanup()

    def module(self, name):
        folder = self.admin.call("POST", "/api/folders", {"project_id": self.pid, "parent_id": "", "name": name})[1]
        return self.admin.call("POST", f"/api/folders/{folder['id']}/transition", {"status": "Approved"})[1]

    def plan(self, name="Release 2.5", **extra):
        status, plan = self.admin.call("POST", "/api/plans", {"project_id": self.pid, "name": name, "release": "2.5", "start_date": "2026-11-01", "end_date": "2026-11-30", **extra})
        self.assertEqual(status, 201, plan); return plan

    def cycle(self, name, plan, testing_type="Regression", **selection):
        status, result = self.admin.call("POST", "/api/cycles", {"project_id": self.pid, "name": name, "testing_type": testing_type, "plan_id": plan["id"] if plan else "", **selection})
        self.assertEqual(status, 201, result); return result["cycle"]

    def runs(self, cycle):
        return {next(k for k, c in self.case.items() if c["id"] == r["test_case_id"]): r for r in self.store.list("test_runs", filters={"cycle_id": cycle["id"]})["items"]}

    def status(self, run, value, **extra):
        self.assertEqual(self.admin.call("POST", f"/api/test-runs/{run['id']}/execute", {"status": value, "actual_result": "Observed", **extra})[0], 200)

    # ------------------------------------------------------------------ plans
    def test_plan_validation_and_listing(self):
        plan = self.plan()
        self.assertEqual(plan["exit_criteria"]["min_pass_rate"], 95)
        for body, message in [({"name": " "}, "name is required"), ({"name": "release 2.5"}, "already exists"), ({"name": "X", "start_date": "2026-11-10", "end_date": "2026-11-01"}, "before the start"),
                              ({"name": "Y", "status": "Paused"}, "Plan status"), ({"name": "Z", "start_date": "11/10/2026"}, "valid date")]:
            status, body_out = self.admin.call("POST", "/api/plans", {"project_id": self.pid, **body})
            self.assertEqual(status, 400, body); self.assertIn(message, body_out["error"])
        status, updated = self.admin.call("PUT", f"/api/test_plans/{plan['id']}", {"status": "Active", "exit_criteria": {"min_pass_rate": 90, "max_open_high": 2}})
        self.assertEqual((status, updated["status"], updated["exit_criteria"]["min_pass_rate"], updated["exit_criteria"]["max_open_high"]), (200, "Active", 90, 2))
        self.assertEqual(self.admin.call("PUT", f"/api/test_plans/{plan['id']}", {"exit_criteria": {"min_pass_rate": 150}})[0], 400)
        self.cycle("Smoke", plan, "Smoke", module_ids=[self.login["id"]])
        listed = self.admin.call("GET", f"/api/plans?project_id={self.pid}")[1]["items"]
        mine = next(p for p in listed if p["id"] == plan["id"])
        self.assertEqual((mine["cycles"], mine["planned"], mine["verdict"]), (1, 2, "Not started"))
        self.assertEqual(self.admin.call("POST", "/api/test_plans", {"project_id": self.pid, "name": "bypass"})[0], 404)  # generic create no longer allowed

    def test_one_test_case_reused_in_many_cycles_keeps_independent_results(self):
        r25 = self.plan(); r26 = self.plan("Release 2.6")
        smoke = self.cycle("R2.5 Smoke", r25, "Smoke", include_case_ids=[self.case["L1"]["id"]])
        regression = self.cycle("R2.5 Regression", r25, module_ids=[self.login["id"], self.pay["id"]])
        smoke26 = self.cycle("R2.6 Smoke", r26, "Smoke", include_case_ids=[self.case["L1"]["id"]])
        self.status(self.runs(smoke)["L1"], "Passed"); self.status(self.runs(regression)["L1"], "Failed")
        self.assertEqual(self.store.list("test_cases", filters={"title": self.case["L1"]["title"]})["total"], 1)  # never copied
        self.assertEqual((self.runs(smoke)["L1"]["status"], self.runs(regression)["L1"]["status"], self.runs(smoke26)["L1"]["status"]), ("Passed", "Failed", "Not Run"))
        report = self.admin.call("GET", f"/api/plans/{r25['id']}/report")[1]
        self.assertEqual([c["name"] for c in report["cycles"]], ["R2.5 Smoke", "R2.5 Regression"])
        self.assertEqual(report["planned"], 4)
        latest = {c["case_key"]: c["status"] for c in report["cases"]}
        self.assertEqual(latest[self.case["L1"]["case_key"]], "Failed")  # latest executed result across the plan
        self.assertEqual(self.admin.call("GET", f"/api/plans/{r26['id']}/report")[1]["planned"], 1)

    def test_release_readiness_follows_exit_criteria(self):
        plan = self.plan(exit_criteria={"min_execution_pct": 100, "min_pass_rate": 75, "max_open_critical": 0, "max_open_high": 0, "max_blocked": 0})
        cycle = self.cycle("Regression", plan, module_ids=[self.login["id"], self.pay["id"]])
        runs = self.runs(cycle)
        for key in ("L1", "L2", "P1"): self.status(runs[key], "Passed")
        report = self.admin.call("GET", f"/api/plans/{plan['id']}/report")[1]
        self.assertEqual((report["readiness"]["verdict"], report["execution_pct"]), ("Not ready", 75.0))
        self.status(runs["P2"], "Failed")
        defect = self.admin.call("POST", "/api/defects", {"project_id": self.pid, "run_id": runs["P2"]["id"], "summary": "Card payment fails", "severity": "Critical"})[1]
        report = self.admin.call("GET", f"/api/plans/{plan['id']}/report")[1]
        checks = {c["key"]: c["ok"] for c in report["readiness"]["checks"]}
        self.assertEqual((checks["min_execution_pct"], checks["min_pass_rate"], checks["max_open_critical"]), (True, True, False))
        self.assertEqual((report["defects"]["open"], report["defects"]["by_severity"]["Critical"]), (1, 1))
        self.admin.call("PUT", f"/api/test_plans/{plan['id']}", {"exit_criteria": {**report["plan"]["exit_criteria"], "max_open_critical": 1}})
        self.assertEqual(self.admin.call("GET", f"/api/plans/{plan['id']}/report")[1]["readiness"]["verdict"], "Ready")
        status, blob = self.admin.call("GET", f"/api/plans/{plan['id']}/export.xlsx")
        headers, rows = XlsxCodec.read_bytes(blob)
        self.assertEqual((status, headers[3], len(rows)), (200, "Latest Status", 4))
        self.assertEqual(next(r for r in rows if r["Latest Status"] == "Failed")["Defects"], defect["key"])

    # ----------------------------------------------------------------- suites
    def test_reusable_suite_builds_cycles_across_releases(self):
        status, suite = self.admin.call("POST", "/api/suites", {"project_id": self.pid, "name": "Payment smoke", "module_ids": [self.pay["id"]], "case_ids": [self.case["L1"]["id"]]})
        self.assertEqual((status, suite["kind"]), (201, "reusable"))
        self.assertEqual(self.admin.call("POST", "/api/suites", {"project_id": self.pid, "name": "payment SMOKE", "module_ids": [self.pay["id"]]})[0], 400)
        self.assertEqual(self.admin.call("POST", "/api/suites", {"project_id": self.pid, "name": "Empty"})[0], 400)
        c25 = self.cycle("R2.5 Payment smoke", self.plan(), "Smoke", suite_ids=[suite["id"]])
        c26 = self.cycle("R2.6 Payment smoke", self.plan("Release 2.6"), "Smoke", suite_ids=[suite["id"]])
        self.assertEqual(sorted(self.runs(c25)), ["L1", "P1", "P2"]); self.assertEqual(sorted(self.runs(c26)), ["L1", "P1", "P2"])
        self.assertEqual(c25["suite_ids"], [suite["id"]])
        listed = self.admin.call("GET", f"/api/suites?project_id={self.pid}")[1]["items"]
        self.assertEqual((listed[0]["eligible"], sorted(c["name"] for c in listed[0]["cycles"])), (3, ["R2.5 Payment smoke", "R2.6 Payment smoke"]))
        self.assertEqual(self.admin.call("PUT", f"/api/test_suites/{suite['id']}", {"name": "Payment smoke v2"})[1]["name"], "Payment smoke v2")
        self.assertEqual(self.arun.call("POST", "/api/suites", {"project_id": self.pid, "name": "x", "module_ids": [self.pay["id"]]})[0], 403)

    # ---------------------------------------------------------------- defects
    def test_defects_logged_from_failures_and_existing_references(self):
        cycle = self.cycle("Regression", self.plan(), module_ids=[self.login["id"], self.pay["id"]])
        runs = self.runs(cycle)
        self.admin.call("POST", f"/api/cycles/{cycle['id']}/assign", {"run_ids": [runs["P1"]["id"]], "assignee_id": self.arun_id})
        self.status(runs["P1"], "Failed", defect_ids="JIRA-77")  # a free-text reference, as before
        self.assertEqual(self.arun.call("POST", "/api/defects", {"project_id": self.pid, "run_id": runs["L1"]["id"], "summary": "Not mine"})[0], 403)
        status, defect = self.arun.call("POST", "/api/defects", {"project_id": self.pid, "run_id": runs["P1"]["id"], "summary": "Refund total wrong", "severity": "High", "external_key": "JIRA-78"})
        self.assertEqual((status, defect["key"], defect["status"], defect["found_in_cycle_id"]), (201, "LMS-DEF-0001", "Open", cycle["id"]))
        self.assertEqual(self.store.get("test_runs", runs["P1"]["id"])["defect_ids"], ["JIRA-77", "LMS-DEF-0001"])
        listed = self.admin.call("GET", f"/api/defects?project_id={self.pid}")[1]
        self.assertEqual([u["reference"] for u in listed["unregistered"]], ["JIRA-77"])
        registered = self.admin.call("POST", "/api/defects/register", {"project_id": self.pid, "reference": "JIRA-77"})[1]
        self.assertEqual((registered["key"], registered["external_key"]), ("LMS-DEF-0002", "JIRA-77"))
        listed = self.admin.call("GET", f"/api/defects?project_id={self.pid}")[1]
        self.assertEqual((listed["unregistered"], listed["summary"]["open"]), ([], 2))
        detail = self.admin.call("GET", f"/api/defects/{registered['id']}")[1]
        self.assertEqual([e["case_key"] for e in detail["executions"]], [runs["P1"]["case_key"]])  # matched through the external key
        self.assertEqual(self.admin.call("POST", "/api/defects/register", {"project_id": self.pid, "reference": "JIRA-77"})[0], 400)
        self.assertEqual(self.admin.call("PUT", f"/api/defects/{defect['id']}", {"status": "Done"})[0], 400)
        self.assertEqual(self.admin.call("POST", "/api/defects", {"project_id": self.pid, "summary": "x", "external_key": "JIRA-78"})[0], 400)
        self.assertEqual(self.admin.call("POST", f"/api/defects/{defect['id']}/link", {"run_id": runs["P2"]["id"]})[0], 200)
        self.assertIn("LMS-DEF-0001", self.store.get("test_runs", runs["P2"]["id"])["defect_ids"])

    # ----------------------------------------------------------------- retest
    def test_retest_failed_cases_after_a_fix_and_verify(self):
        plan = self.plan()
        cycle = self.cycle("R2.5 Regression", plan, module_ids=[self.login["id"], self.pay["id"]])
        runs = self.runs(cycle)
        self.admin.call("POST", f"/api/cycles/{cycle['id']}/assign", {"run_ids": [runs["P1"]["id"]], "assignee_id": self.arun_id})
        self.status(runs["L1"], "Passed"); self.status(runs["L2"], "Blocked"); self.status(runs["P1"], "Failed"); self.status(runs["P2"], "Passed")
        defect = self.admin.call("POST", "/api/defects", {"project_id": self.pid, "run_id": runs["P1"]["id"], "summary": "Refund wrong", "severity": "High"})[1]
        status, retest = self.admin.call("POST", f"/api/cycles/{cycle['id']}/retest", {"statuses": ["Failed", "Blocked"], "build": "2.5.1"})
        self.assertEqual(status, 201, retest)
        new = retest["cycle"]
        self.assertEqual((new["testing_type"], new["source_cycle_id"], new["plan_id"], new["build"], retest["added"]), ("Retesting", cycle["id"], plan["id"], "2.5.1", 2))
        new_runs = self.runs(new)
        self.assertEqual(sorted(new_runs), ["L2", "P1"])
        self.assertEqual(new_runs["P1"]["assignee_id"], self.arun_id)  # tester kept
        self.assertEqual(self.store.get("test_runs", runs["P1"]["id"])["status"], "Failed")  # original result untouched
        self.admin.call("PUT", f"/api/defects/{defect['id']}", {"status": "Fixed", "fixed_in_build": "2.5.2"})
        self.assertEqual(self.admin.call("GET", f"/api/defects/{defect['id']}")[1]["retest"], "Awaiting retest")
        status, from_defect = self.admin.call("POST", f"/api/defects/{defect['id']}/retest", {})
        self.assertEqual((status, from_defect["cycle"]["build"], from_defect["added"]), (201, "2.5.2", 1))
        self.status(self.runs(from_defect["cycle"])["P1"], "Passed")
        self.assertEqual(self.admin.call("GET", f"/api/defects/{defect['id']}")[1]["retest"], "Retest passed")
        verified = self.admin.call("PUT", f"/api/defects/{defect['id']}", {"status": "Verified"})[1]
        self.assertTrue(verified["closed_at"])
        report = self.admin.call("GET", f"/api/plans/{plan['id']}/report")[1]
        latest = {c["case_key"]: c["status"] for c in report["cases"]}
        self.assertEqual(latest[self.case["P1"]["case_key"]], "Passed")  # the retest result is now the release's result
        nothing = self.admin.call("POST", f"/api/cycles/{from_defect['cycle']['id']}/retest", {})
        self.assertEqual(nothing[0], 400)

    # ------------------------------------------------- requirements & metadata
    def test_requirement_coverage_and_case_metadata(self):
        req = self.admin.call("POST", "/api/requirements", {"project_id": self.pid, "key": "PAY-1", "title": "Card payments", "status": "Approved"})[1]
        lonely = self.admin.call("POST", "/api/requirements", {"project_id": self.pid, "key": "PAY-2", "title": "Refunds", "status": "Draft"})[1]
        case = self.case["P1"]
        status, updated = self.admin.call("PUT", f"/api/test_cases/{case['id']}", {"requirement_ids": [req["id"]], "automation_status": "Automated", "automation_reference": "tests/pay.spec.ts"})
        self.assertEqual((status, updated["version"], updated["status"], updated["requirement_ids"]), (200, "1", "Approved", [req["id"]]))  # metadata: no new version
        self.assertEqual(self.admin.call("PUT", f"/api/test_cases/{case['id']}", {"automation_status": "Maybe"})[0], 400)
        coverage = self.admin.call("GET", f"/api/requirements/coverage?project_id={self.pid}")[1]
        rows = {r["key"]: r for r in coverage["requirements"]}
        self.assertEqual((rows["PAY-1"]["coverage"], rows["PAY-2"]["coverage"]), ("Not run", "Not covered"))
        cycle = self.cycle("Smoke", None, "Smoke", include_case_ids=[case["id"]])
        self.status(self.runs(cycle)["P1"], "Failed")
        self.assertEqual({r["key"]: r["coverage"] for r in self.admin.call("GET", f"/api/requirements/coverage?project_id={self.pid}")[1]["requirements"]}["PAY-1"], "Failing")


class ReleaseMigrationTests(unittest.TestCase):
    def test_existing_cycles_are_linked_to_their_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExcelStorageService(tmp)
            XlsxCodec.write(store.path_for("test_plans"), WORKBOOKS["test_plans"][1][:14], [{"id": "p1", "project_id": "x", "name": "Release 1", "status": "Draft", "archived": "False"}])
            XlsxCodec.write(store.path_for("test_suites"), WORKBOOKS["test_suites"][1][:10], [{"id": "s1", "project_id": "x", "plan_id": "p1", "name": "Cycle suite", "status": "Active", "archived": "False"}])
            XlsxCodec.write(store.path_for("test_cycles"), WORKBOOKS["test_cycles"][1][:18], [{"id": "c1", "project_id": "x", "suite_id": "s1", "name": "Old cycle", "archived": "False"},
                                                                                            {"id": "c2", "project_id": "x", "suite_id": "", "name": "No plan", "archived": "False"}])
            upgraded = ExcelStorageService(tmp)
            self.assertEqual((upgraded.get("test_cycles", "c1")["plan_id"], upgraded.get("test_cycles", "c2")["plan_id"]), ("p1", ""))
            self.assertEqual(upgraded.get("test_suites", "s1")["kind"], "cycle_link")


if __name__ == "__main__": unittest.main()
