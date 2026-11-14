"""Phase 7: historical comparison across test cycles."""
import tempfile
import threading
import unittest

import server
from app.storage import XlsxCodec
from test_api import Client, ADMIN_EMAIL, ADMIN_PASSWORD

STEP = [{"action": "Do it", "expected_result": "Done"}]


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd = server.make_server("127.0.0.1", 0, cls.tmp.name)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True); cls.thread.start()
        cls.store = cls.httpd.app.store
        cls.admin = Client(f"http://127.0.0.1:{cls.httpd.server_address[1]}")
        cls.admin.login(ADMIN_EMAIL, "change-me")
        cls.admin.call("POST", "/api/auth/change-password", {"current_password": "change-me", "new_password": ADMIN_PASSWORD})
        cls.project = cls.store.list("projects")["items"][0]
        folder = cls.module("Orders"); other = cls.module("Returns")
        cls.case = {}
        for key in "ABCDEFGHIJKLM":
            cls.case[key] = cls.new_case(f"ORD-{key} – Scenario {key}", folder)
        # Same title in two modules must stay two separate rows.
        cls.case["N1"] = cls.new_case("Shared title", folder)
        cls.case["N2"] = cls.new_case("Shared title", other, allow_duplicate=True)
        everyone = "ABCDEFGIJKLMN"
        # Created out of order on purpose: comparison must sort by date.
        cls.c3 = None
        cls.c1 = cls.cycle("Cycle 01", "2026-10-01", [k for k in "ABCDEGIJM"] + ["N1", "N2"])
        cls.c2 = cls.cycle("Cycle 02", "2026-10-15", [k for k in "ABCDEFGJKL"] + ["N1", "N2"])
        # K's definition changes between Cycle 02 and Cycle 03.
        cls.admin.call("PUT", f"/api/test_cases/{cls.case['K']['id']}", {"steps": STEP + [{"action": "Verify", "expected_result": "Verified"}]})
        cls.admin.call("POST", f"/api/test-cases/{cls.case['K']['id']}/transition", {"status": "Approved"})
        cls.c3 = cls.cycle("Cycle 03", "2026-11-01", [k for k in "ABCDEFHIJKL"] + ["N1", "N2"])
        results = {cls.c1: "A:Passed B:Passed C:Failed D:Passed E:Failed G:Passed I:Passed J:Passed M:Passed N1:Passed N2:Failed",
                   cls.c2: "A:Passed B:Passed C:Failed D:Failed E:Passed F:Not_Run G:Passed J:Passed K:Passed L:Passed N1:Passed N2:Failed",
                   cls.c3: "A:Passed B:Failed C:Failed D:Passed E:Failed F:Failed H:Passed I:Passed J:Not_Run K:Passed L:Blocked N1:Passed N2:Passed"}
        for cycle_id, spec in results.items():
            runs = {r["test_case_id"]: r for r in cls.store.list("test_runs", filters={"cycle_id": cycle_id}, page_size=100)["items"]}
            for item in spec.split():
                key, value = item.split(":"); value = value.replace("_", " ")
                if value != "Not Run":
                    cls.admin.call("PUT", f"/api/test-runs/{runs[cls.case[key]['id']]['id']}/status", {"status": value})

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.thread.join(5); cls.tmp.cleanup()

    @classmethod
    def module(cls, name):
        folder = cls.admin.call("POST", "/api/folders", {"project_id": cls.project["id"], "parent_id": "", "name": name})[1]
        return cls.admin.call("POST", f"/api/folders/{folder['id']}/transition", {"status": "Approved"})[1]

    @classmethod
    def new_case(cls, title, folder, **extra):
        case = cls.admin.call("POST", "/api/test-cases", {"project_id": cls.project["id"], "folder_id": folder["id"], "title": title, "priority": "High", "steps": STEP, **extra})[1]
        return cls.admin.call("POST", f"/api/test-cases/{case['id']}/transition", {"status": "Approved"})[1]

    @classmethod
    def cycle(cls, name, start, keys):
        result = cls.admin.call("POST", "/api/cycles", {"project_id": cls.project["id"], "name": name, "testing_type": "Regression", "start_date": start,
                                                       "include_case_ids": [cls.case[k]["id"] for k in keys]})
        assert result[0] == 201, result
        return result[1]["cycle"]["id"]

    def compare(self, ids=None, **extra):
        ids = ids or [self.c3, self.c1, self.c2]
        query = "&".join(f"{k}={v}" for k, v in extra.items())
        return self.admin.call("GET", f"/api/comparison?project_id={self.project['id']}&cycle_ids={','.join(ids)}&{query}")

    def rows(self, data):
        by_id = {c["id"]: k for k, c in self.case.items()}
        return {by_id[r["test_case_id"]]: r for r in data["rows"]}

    def test_cycles_are_ordered_by_date_and_the_last_two_are_compared(self):
        status, data = self.compare()
        self.assertEqual(status, 200)
        self.assertEqual([c["name"] for c in data["cycles"]], ["Cycle 01", "Cycle 02", "Cycle 03"])
        self.assertEqual((data["previous_id"], data["current_id"]), (self.c2, self.c3))

    def test_every_category_is_identified(self):
        rows = self.rows(self.compare()[1])
        expected = {"A": "consistently_passing", "B": "regression", "C": "repeated_failure", "D": "fixed", "E": "regression",
                    "F": "new_failure", "G": "removed", "H": "new_case", "I": "returned", "J": "not_executed", "K": "consistently_passing",
                    "L": "blocked", "M": "elsewhere"}
        self.assertEqual({k: rows[k]["category"] for k in expected}, expected)
        self.assertIn("reopened", rows["E"]["flags"])
        self.assertNotIn("reopened", rows["D"]["flags"])
        self.assertEqual((rows["E"]["flips"], rows["A"]["flips"]), (2, 0))
        self.assertIn("modified", rows["K"]["flags"])
        self.assertEqual((rows["K"]["cells"][self.c2]["version"], rows["K"]["cells"][self.c3]["version"]), ("1", "2"))
        self.assertNotIn("modified", rows["A"]["flags"])

    def test_matching_uses_record_ids_not_titles(self):
        rows = self.rows(self.compare()[1])
        self.assertEqual((rows["N1"]["category"], rows["N2"]["category"]), ("consistently_passing", "fixed"))
        self.assertNotEqual(rows["N1"]["test_case_id"], rows["N2"]["test_case_id"])

    def test_any_two_selected_cycles_can_be_compared(self):
        data = self.compare(previous_id=self.c1, current_id=self.c3)[1]
        rows = self.rows(data)
        # D failed in the cycle in between, so it is passing but not consistently; I passed every time it ran.
        self.assertEqual((rows["D"]["category"], rows["I"]["category"], rows["G"]["category"]), ("passing", "consistently_passing", "removed"))
        swapped = self.compare(previous_id=self.c3, current_id=self.c1)[1]
        self.assertEqual((swapped["previous_id"], swapped["current_id"]), (self.c1, self.c3))  # always earlier → later

    def test_summary_and_trend_use_real_counts(self):
        data = self.compare()[1]
        self.assertEqual(data["summary"]["regression"], 2)
        self.assertEqual((data["modified"], data["reopened"]), (1, 1))
        trend = {t["name"]: t for t in data["trend"]}
        c3 = trend["Cycle 03"]
        self.assertEqual((c3["total"], c3["counts"]["Passed"], c3["counts"]["Failed"], c3["counts"]["Blocked"], c3["counts"]["Not Run"]), (13, 7, 4, 1, 1))
        self.assertEqual((c3["executed"], c3["completion"], c3["pass_rate"]), (12, 92.3, 58.3))

    def test_default_selection_uses_cycles_with_results(self):
        empty = self.admin.call("POST", "/api/cycles", {"project_id": self.project["id"], "name": "Cycle 04 (not started)", "testing_type": "Smoke", "start_date": "2026-12-01", "include_case_ids": [self.case["A"]["id"]]})[1]["cycle"]["id"]
        try:
            data = self.admin.call("GET", f"/api/comparison?project_id={self.project['id']}")[1]
            self.assertEqual([c["name"] for c in data["cycles"]], ["Cycle 01", "Cycle 02", "Cycle 03"])
        finally:
            self.admin.call("DELETE", f"/api/test_cycles/{empty}")

    def test_invalid_requests_are_refused(self):
        self.assertEqual(self.compare([self.c1])[0], 400)
        self.assertEqual(self.compare([self.c1, "missing"])[0], 400)
        self.assertEqual(self.compare(previous_id=self.c1, current_id=self.c1)[0], 400)

    def test_comparison_exports_to_excel(self):
        status, blob = self.admin.call("GET", f"/api/comparison/export.xlsx?project_id={self.project['id']}&cycle_ids={self.c1},{self.c2},{self.c3}")
        self.assertEqual(status, 200)
        headers, rows = XlsxCodec.read_bytes(blob)
        self.assertEqual(headers[:6], ["Test Case ID", "Title", "Module", "Category", "Definition changed", "Reopened"])
        self.assertEqual(headers[6:], ["Cycle 01", "Cycle 02", "Cycle 03"])
        b = next(r for r in rows if r["Title"] == "ORD-B – Scenario B")
        self.assertEqual(b["Test Case ID"], self.case["B"]["case_key"])
        self.assertEqual((b["Category"], b["Cycle 01"], b["Cycle 03"]), ("Regression (passed → failed)", "Passed (v1)", "Failed (v1)"))
        h = next(r for r in rows if r["Title"] == "ORD-H – Scenario H")
        self.assertEqual(h["Cycle 01"], "—")


if __name__ == "__main__": unittest.main()
