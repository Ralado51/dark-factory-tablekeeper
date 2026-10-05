"""Cumulative API checks derived from the Stage 2 contract."""
import copy
import concurrent.futures
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

import verify_contract as contract
from verify_contract import DAY, fixture, port, request


def paired_fixture():
    seeded = fixture()
    seeded["restaurants"][0]["tables"] = [
        {"id": "t1", "label": "Window", "capacity": 2},
        {"id": "t2", "label": "Garden", "capacity": 4},
        {"id": "t3", "label": "Library", "capacity": 3},
        {"id": "t4", "label": "Terrace", "capacity": 4}]
    seeded["restaurants"][0]["combinable"] = [["t2", "t1"], ["t2", "t3"], ["t3", "t4"]]
    return seeded


class Stage2(contract.Contract):
    def reset_pair(self):
        self.assertEqual(request(self.base, "POST", "/_test/reset", paired_fixture())[0], 204)
        self.token = request(self.base, "POST", "/auth/login",
                             {"email": "one@example.com", "password": "password1"})[1]["token"]

    def pair_body(self, ids=None, size=5):
        body = self.booking()
        del body["table_id"]
        body.update(table_ids=ids or ["t2", "t1"], party_size=size)
        return body

    def test_pair_options_order_occupancy_and_cancellation(self):
        self.reset_pair()
        options = request(self.base, "GET", "/availability?restaurant_id=r1&date="+DAY+"&party_size=5")[1]["slots"][0]
        self.assertEqual(options["available_table_ids"], [])
        self.assertEqual(options["available_options"], [
            {"table_ids": ["t2", "t1"], "capacity": 6},
            {"table_ids": ["t2", "t3"], "capacity": 7},
            {"table_ids": ["t3", "t4"], "capacity": 7}])
        created = self.create("pair", self.pair_body(["t1", "t2"]))[1]
        self.assertEqual(created["table_ids"], ["t2", "t1"])
        self.assertNotIn("table_id", created)
        slots = request(self.base, "GET", "/availability?restaurant_id=r1&date="+DAY+"&party_size=1")[1]["slots"]
        current = next(s for s in slots if s["starts_at_local"] == DAY+"T12:00")
        self.assertEqual(current["available_table_ids"], ["t3", "t4"])
        self.assertEqual(current["available_options"], [
            {"table_ids": ["t3"], "capacity": 3}, {"table_ids": ["t4"], "capacity": 4},
            {"table_ids": ["t3", "t4"], "capacity": 7}])
        request(self.base, "POST", "/reservations/"+created["reference"]+"/cancel", {}, self.token)
        self.assertEqual(self.create("single", self.booking("t1"))[0], 201)

    def test_pair_input_errors_and_nontransitivity(self):
        self.reset_pair()
        for body, code, status in [
            (dict(self.pair_body(), table_id="t1"), "validation_failed", 422),
            (self.pair_body(["t1", "t1"]), "validation_failed", 422),
            (self.pair_body(["t1", "t2", "t3"]), "combination_not_allowed", 422),
            (self.pair_body(["t1", "t3"]), "combination_not_allowed", 422),
            (self.pair_body(["missing", "t2"]), "not_found", 404),
            (dict(self.pair_body(), table_ids=[]), "validation_failed", 422),
            (dict(self.pair_body(), table_ids="t1+t2"), "malformed_request", 400),
            (dict(self.pair_body(), table_ids=[False]), "malformed_request", 400),
            (self.pair_body(size=7), "party_exceeds_capacity", 422)]:
            self.check_error(self.create("failed-shape", body), status, code)
        # All failed uses leave the same key reusable.
        self.assertEqual(self.create("failed-shape", self.pair_body())[0], 201)

    def test_50_pair_contenders_share_member_linearizably(self):
        self.reset_pair()
        def contender(i):
            return self.create("pair-"+str(i), self.pair_body(["t2", "t1"] if i%2 else ["t2", "t3"]))
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            replies = list(pool.map(contender, range(50)))
        self.assertEqual(sum(s == 201 for s, _ in replies), 1)
        self.assertEqual(sum(s == 409 and body["error"]["code"] == "table_unavailable" for s, body in replies), 49)
        self.assertEqual(len(request(self.base, "GET", "/reservations", token=self.token)[1]["reservations"]), 1)

    def test_pair_amendment_atomicity_and_import_original_receipt(self):
        self.reset_pair()
        blocker = self.create("blocker", self.booking("t1"))[1]
        original = self.create("pair", self.pair_body(["t2", "t3"]))[1]
        before = request(self.base, "GET", "/_test/export")[1]
        path = "/reservations/"+original["reference"]
        self.check_error(request(self.base, "PATCH", path, {"table_ids":["t1","t2"]}, self.token),
                         409, "table_unavailable")
        self.assertEqual(request(self.base, "GET", "/_test/export")[1], before)
        request(self.base, "POST", "/reservations/"+blocker["reference"]+"/cancel", {}, self.token)
        changed = request(self.base, "PATCH", path, {"table_ids":["t1","t2"]}, self.token)
        self.assertEqual(changed[0], 200)
        self.assertEqual(changed[1]["reservation_id"], original["reservation_id"])
        self.assertEqual(changed[1]["created_at"], original["created_at"])
        exported = request(self.base, "GET", "/_test/export")[1]
        self.assertEqual(request(self.dest, "POST", "/_test/import", exported)[0], 204)
        self.assertEqual(request(self.dest, "POST", "/reservations", self.pair_body(["t2","t3"]),
                                 self.token, "pair"), (200, original))
        single = request(self.dest, "PATCH", path, {"table_id":"t4","party_size":2}, self.token)
        self.assertEqual(single[0], 200)
        self.assertEqual(single[1]["table_ids"], ["t4"])
        self.assertEqual(single[1]["table_id"], "t4")

    def test_pair_batch_swap_overlap_rollback_and_retry(self):
        self.reset_pair()
        a = self.create("a", self.pair_body(["t1","t2"]))[1]
        b = self.create("b", self.pair_body(["t3","t4"]))[1]
        body = {"moves":[{"reference":a["reference"],"table_ids":["t3","t4"]},
                         {"reference":b["reference"],"table_ids":["t1","t2"]}]}
        response = request(self.base,"POST","/reservation-moves",body,self.token,"swap")
        self.assertEqual(response[0],201)
        self.assertEqual([r["table_ids"] for r in response[1]["reservations"]], [["t3","t4"],["t2","t1"]])
        before = request(self.base,"GET","/_test/export")[1]
        conflict = {"moves":[{"reference":a["reference"],"table_ids":["t2","t3"]},
                             {"reference":b["reference"],"table_ids":["t1","t2"]}]}
        self.check_error(request(self.base,"POST","/reservation-moves",conflict,self.token,"retry"),
                         409,"table_unavailable")
        self.assertEqual(request(self.base,"GET","/_test/export")[1],before)
        self.assertEqual(request(self.base,"POST","/reservation-moves",body,self.token,"retry")[0],201)
        self.assertEqual(request(self.base,"POST","/reservation-moves",body,self.token,"swap"),(200,response[1]))

    def test_cancelled_seeded_pair_does_not_occupy(self):
        seeded = paired_fixture()
        seeded["reservations"] = [dict(self.pair_body(), id="cancelled-seed", reference="SEED02", user_id="u1",status="cancelled")]
        self.assertEqual(request(self.base,"POST","/_test/reset",seeded)[0],204)
        self.token=request(self.base,"POST","/auth/login",{"email":"one@example.com","password":"password1"})[1]["token"]
        detail=request(self.base,"GET","/reservations/SEED02",token=self.token)[1]
        self.assertEqual(detail["status"],"cancelled")
        self.assertEqual(self.create("reuse",self.pair_body())[0],201)

    def test_actual_accepted_stage1_export_upgrade_preserves_originals(self):
        app = Path(os.environ.get("STAGE1_APP", str(Path(__file__).parent.parent/"stage-1"/"app.py")))
        number = port()
        process = subprocess.Popen([sys.executable, str(app)], env=dict(os.environ,PORT=str(number),PYTHONDONTWRITEBYTECODE="1"),
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        source = f"http://127.0.0.1:{number}"
        try:
            for _ in range(100):
                try:
                    if request(source,"GET","/health")[0] == 200:
                        break
                except OSError:
                    time.sleep(.05)
            request(source,"POST","/_test/reset",fixture())
            token=request(source,"POST","/auth/login",{"email":"one@example.com","password":"password1"})[1]["token"]
            body=self.booking()
            original=request(source,"POST","/reservations",body,token,"lost-original")[1]
            batch={"moves":[{"reference":original["reference"],"table_id":"t2"}]}
            receipt=request(source,"POST","/reservation-moves",batch,token,"batch")[1]
            exported=request(source,"GET","/_test/export")[1]
            self.assertEqual(request(self.dest,"POST","/_test/import",exported)[0],204)
            self.assertEqual(request(self.dest,"POST","/reservations",body,token,"lost-original"),(200,original))
            self.assertEqual(request(self.dest,"POST","/reservation-moves",batch,token,"batch"),(200,receipt))
            detail=request(self.dest,"GET","/reservations/"+original["reference"],token=token)[1]
            self.assertEqual(detail["table_ids"],["t2"])
            self.assertEqual(detail["reservation_id"],original["reservation_id"])
            self.assertEqual(request(self.dest,"POST","/auth/login",{"email":"one@example.com","password":"password1"})[0],200)
            # A Stage 2 re-export remains repeatably importable, including legacy receipts.
            upgraded=request(self.dest,"GET","/_test/export")[1]
            self.assertEqual(request(self.dest,"POST","/_test/import",upgraded)[0],204)
            self.assertEqual(request(self.dest,"POST","/reservations",body,token,"lost-original"),(200,original))
        finally:
            process.terminate();process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
