"""Phase 8: dashboard metrics, filters, trends and Excel reports."""
import tempfile
import threading
import unittest
from datetime import date

import server
from app.storage import XlsxCodec
from test_api import Client, ADMIN_EMAIL, ADMIN_PASSWORD

STEP = [{"action": "Do it", "expected_result": "Done"}]


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd = server.make_server("127.0.0.1", 0, cls.tmp.name)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True); cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.store = cls.httpd.app.store
        cls.admin = Client(cls.base)
        cls.admin.login(ADMIN_EMAIL, "change-me")
        cls.admin.call("POST", "/api/auth/change-password", {"current_password": "change-me", "new_password": ADMIN_PASSWORD})
        cls.project = cls.store.list("projects")["items"][0]
        enroll = cls.module("Enroll"); waitlist = cls.module("Waitlist", enroll); pay = cls.module("Pay")
        cls.case = {key: cls.new_case(title, folder) for key, title, folder in [
            ("e1", "ENR-1 – Enroll", enroll), ("e2", "ENR-2 – Drop", enroll), ("w1", "ENR-3 – Waitlist", waitlist),
            ("p1", "PAY-1 – Card", pay), ("p2", "PAY-2 – Refund", pay)]}
        cls.arun = cls.member("Arun"); cls.kumar = cls.member("Kumar")
        plan = {"C1": ("2026-10-01", "e1:Passed e2:Failed w1:Passed p1:Passed p2:Failed"),
                "C2": ("2026-10-15", "e1:Passed e2:Passed w1:Failed p1:Passed p2:Failed"),
                "C3": ("2026-11-01", "e1:Failed e2:Failed p1:Passed p2:Blocked")}
        cls.cycles = {}
        for name, (start, spec) in plan.items():
            cycle = cls.admin.call("POST", "/api/cycles", {"project_id": cls.project["id"], "name": name, "testing_type": "Regression", "start_date": start,
                                                          "include_case_ids": [c["id"] for c in cls.case.values()]})[1]["cycle"]
            cls.cycles[name] = cycle["id"]
            runs = {r["test_case_id"]: r["id"] for r in cls.store.list("test_runs", filters={"cycle_id": cycle["id"]})["items"]}
            if name == "C3":
                cls.runs3 = {k: runs[c["id"]] for k, c in cls.case.items()}
                cls.admin.call("POST", f"/api/cycles/{cycle['id']}/module-assignment", {"module_path": "/Enroll", "assignee_id": cls.arun[0]["id"]})
                cls.admin.call("POST", f"/api/cycles/{cycle['id']}/module-assignment", {"module_path": "/Pay", "assignee_id": cls.kumar[0]["id"]})
                cls.admin.call("POST", f"/api/cycles/{cycle['id']}/assign", {"run_ids": [cls.runs3["p2"]], "assignee_id": ""})
            for item in spec.split():
                key, value = item.split(":")
                if key == "e1" and name == "C3":
                    cls.admin.call("POST", f"/api/test-runs/{runs[cls.case[key]['id']]}/execute", {"status": "Failed", "actual_result": "Error", "defect_ids": "ENR-9, ENR-10"})
                else:
                    cls.admin.call("PUT", f"/api/test-runs/{runs[cls.case[key]['id']]}/status", {"status": value})
            if name != "C3":
                cls.admin.call("PUT", f"/api/test_cycles/{cycle['id']}", {"status": "Completed"})

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.thread.join(5); cls.tmp.cleanup()

    @classmethod
    def module(cls, name, parent=None):
        folder = cls.admin.call("POST", "/api/folders", {"project_id": cls.project["id"], "parent_id": parent["id"] if parent else "", "name": name})[1]
        return cls.admin.call("POST", f"/api/folders/{folder['id']}/transition", {"status": "Approved"})[1]

    @classmethod
    def new_case(cls, title, folder):
        case = cls.admin.call("POST", "/api/test-cases", {"project_id": cls.project["id"], "folder_id": folder["id"], "title": title, "priority": "High", "steps": STEP})[1]
        return cls.admin.call("POST", f"/api/test-cases/{case['id']}/transition", {"status": "Approved"})[1]

    @classmethod
    def member(cls, name):
        email = f"{name.lower()}@x.local"
        user = cls.admin.call("POST", "/api/users", {"name": name, "email": email, "role": "Team Member", "password": "Temp-Pass-1"})[1]
        client = Client(cls.base); client.login(email, "Temp-Pass-1")
        client.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "Team-Pass-22"})
        return user, client

    def dashboard(self, client=None, **filters):
        query = "&".join(f"{k}={v}" for k, v in filters.items())
        return (client or self.admin).call("GET", f"/api/reports/dashboard?project_id={self.project['id']}&{query}")

    # ----------------------------------------------------------------- metrics
    def test_default_scope_is_active_cycles_with_exact_numbers(self):
        status, data = self.dashboard()
        self.assertEqual(status, 200)
        self.assertEqual((data["scope"]["basis"], [c["name"] for c in data["scope"]["cycles"]]), ("active", ["C3"]))
        ex = data["execution"]
        self.assertEqual((ex["total"], ex["executed"], ex["open"], ex["completion"], ex["pass_rate"]), (5, 4, 1, 80.0, 25.0))
        self.assertEqual((ex["counts"]["Passed"], ex["counts"]["Failed"], ex["counts"]["Blocked"], ex["counts"]["Not Run"]), (1, 2, 1, 1))
        self.assertEqual((data["cycles"]["active"], data["cycles"]["completed"]), (1, 2))
        self.assertEqual((data["repository"]["test_cases"], data["repository"]["approval"]["Approved"]), (6, 6))  # +1 seeded case

    def test_module_and_member_breakdowns(self):
        data = self.dashboard()[1]
        modules = {m["path"]: m for m in data["modules"]}
        self.assertEqual((modules["/Enroll"]["total"], modules["/Enroll"]["executed"], modules["/Enroll/Waitlist"]["total"], modules["/Pay"]["executed"]), (3, 2, 1, 2))
        members = {m["name"]: m for m in data["members"]}
        self.assertEqual((members["Arun"]["total"], members["Arun"]["executed"], members["Arun"]["open"], members["Arun"]["workload"]), (3, 2, 1, 1))
        self.assertEqual((members["Kumar"]["total"], members["Unassigned"]["total"], members["Unassigned"]["workload"]), (1, 1, None))
        self.assertEqual(data["members"][-1]["name"], "Unassigned")

    def test_regression_trend_compares_each_cycle_with_the_one_before(self):
        trend = {t["name"]: t for t in self.dashboard()[1]["trend"]}
        self.assertEqual(list(trend), ["C1", "C2", "C3"])
        self.assertEqual((trend["C2"]["regressions"], trend["C2"]["fixed"], trend["C2"]["new_failures"]), (1, 1, 0))
        self.assertEqual((trend["C3"]["regressions"], trend["C3"]["reopened"], trend["C3"]["fixed"]), (2, 1, 0))
        self.assertEqual(trend["C1"]["pass_rate"], 60.0)
        latest = self.dashboard()[1]["latest"]
        self.assertEqual((latest["cycle"], latest["regressions"], latest["reopened"]), ("C3", 2, 1))

    def test_filters_change_the_scope(self):
        self.assertEqual(self.dashboard(module="/Enroll/Waitlist")[1]["execution"]["total"], 1)
        self.assertEqual(self.dashboard(member=self.kumar[0]["id"])[1]["execution"]["total"], 1)
        self.assertEqual(self.dashboard(member="__none")[1]["execution"]["total"], 1)
        selected = self.dashboard(cycle_ids=self.cycles["C1"])[1]
        self.assertEqual((selected["scope"]["basis"], selected["execution"]["counts"]["Failed"]), ("selected", 2))
        self.assertEqual(self.dashboard(cycle_scope="all")[1]["execution"]["total"], 15)
        dated = self.dashboard(date_from="2026-10-10", date_to="2026-10-31")[1]
        self.assertEqual([c["name"] for c in dated["scope"]["cycles"]], ["C2"])
        self.assertEqual(self.dashboard(date_from="2026-10-31", date_to="2026-10-01")[0], 400)
        self.assertEqual(self.dashboard(date_from="31/10/2026")[0], 400)
        self.assertEqual(self.dashboard(cycle_ids="missing")[0], 400)

    def test_activity_counts_executions_per_day_in_scope(self):
        today = date.today().isoformat()
        activity = {a["date"]: a["count"] for a in self.dashboard()[1]["activity"]}
        self.assertEqual(activity[today], 4)  # the four C3 results; C1/C2 are outside the default scope
        self.assertEqual(len(activity), 14)

    def test_recent_failures_list_links_defects(self):
        failures = self.dashboard()[1]["recent_failures"]
        e1 = next(f for f in failures if f["case_key"] == "ENR-1")
        self.assertEqual((e1["defect_ids"], e1["assignee"], e1["cycle"]), (["ENR-9", "ENR-10"], "Arun", "C3"))

    # ----------------------------------------------------------------- reports
    def test_excel_reports(self):
        for kind, expected_rows, first_header in [("execution", 5, "Cycle"), ("modules", 3, "Module"), ("members", 3, "Member"), ("defects", 2, "Defect")]:
            status, blob = self.admin.call("GET", f"/api/reports/{kind}.xlsx?project_id={self.project['id']}")
            self.assertEqual(status, 200, kind)
            headers, rows = XlsxCodec.read_bytes(blob)
            self.assertEqual((headers[0], len(rows)), (first_header, expected_rows), kind)
        headers, rows = XlsxCodec.read_bytes(self.admin.call("GET", f"/api/reports/defects.xlsx?project_id={self.project['id']}")[1])
        self.assertEqual(sorted(r["Defect"] for r in rows), ["ENR-10", "ENR-9"])
        self.assertEqual(self.admin.call("GET", f"/api/reports/unknown.xlsx?project_id={self.project['id']}")[0], 400)

    def test_team_members_see_only_their_own_numbers_and_cannot_export(self):
        kumar = self.kumar[1]
        data = self.dashboard(kumar, member=self.arun[0]["id"])[1]
        self.assertEqual((data["execution"]["total"], [m["name"] for m in data["members"]]), (1, ["Kumar"]))
        self.assertEqual(kumar.call("GET", f"/api/reports/execution.xlsx?project_id={self.project['id']}")[0], 403)


if __name__ == "__main__": unittest.main()
