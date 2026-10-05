"""Requirement-derived Stage 1 checks; no snapshots or credentials are logged."""
import copy
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DAY = "2030-01-07"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def fixture(zone="UTC", opens="00:00", closes="23:59", duration=60):
    return {"users": [{"id": "u1", "email": "one@example.com", "password": "password1", "display_name": "One"},
                      {"id": "u2", "email": "two@example.com", "password": "password2", "display_name": "Two"}],
            "restaurants": [{"id": "r1", "name": "First", "timezone": zone, "slot_minutes": 30,
                             "reservation_duration_minutes": duration, "cancellation_cutoff_minutes": 120,
                             "opening_hours": [{"weekday": d, "opens": opens, "closes": closes} for d in WEEKDAYS],
                             "tables": [{"id": "t1", "label": "First", "capacity": 4},
                                        {"id": "t2", "label": "Second", "capacity": 4}]}],
            "reservations": []}


def request(base, method, path, body=None, token=None, key=None, raw=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key is not None:
        headers["Idempotency-Key"] = key
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = Request(base + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=5) as response:
            content = response.read()
            return response.status, json.loads(content) if content else None
    except HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        finally:
            error.close()


def request_exact_export(base):
    with urlopen(base+"/_test/export", timeout=5) as response:
        return response.read()


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.processes, cls.bases = [], []
        for _ in range(2):
            number = port()
            env = dict(os.environ, PORT=str(number), PYTHONDONTWRITEBYTECODE="1")
            p = subprocess.Popen([sys.executable, str(Path(__file__).with_name("app.py"))], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cls.processes.append(p)
            base = f"http://127.0.0.1:{number}"
            cls.bases.append(base)
            for attempt in range(100):
                try:
                    if request(base, "GET", "/health")[0] == 200:
                        break
                except OSError:
                    time.sleep(.05)
            else:
                raise AssertionError("Service did not start")

    @classmethod
    def tearDownClass(cls):
        for p in cls.processes:
            p.terminate()
            p.wait(timeout=5)

    def setUp(self):
        self.base, self.dest = self.bases
        for base in self.bases:
            self.assertEqual(request(base, "POST", "/_test/reset", fixture())[0], 204)
        self.token = request(self.base, "POST", "/auth/login",
                             {"email": "one@example.com", "password": "password1"})[1]["token"]
        self.other = request(self.base, "POST", "/auth/login",
                             {"email": "two@example.com", "password": "password2"})[1]["token"]

    def booking(self, table="t1", start=DAY+"T12:00", **extra):
        return dict(restaurant_id="r1", table_id=table, starts_at_local=start, party_size=2, **extra)

    def create(self, key, body=None, token=None):
        return request(self.base, "POST", "/reservations", body or self.booking(),
                       token or self.token, key)

    def check_error(self, response, status, code):
        self.assertEqual(response[0], status)
        self.assertEqual(response[1]["error"]["code"], code)

    def test_50_identical_requests_linearize_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            responses = list(pool.map(lambda _: self.create("same"), range(50)))
        self.assertEqual([s for s, _ in responses].count(201), 1)
        self.assertEqual([s for s, _ in responses].count(200), 49)
        self.assertTrue(all(body == responses[0][1] for _, body in responses))
        self.assertEqual(len(request(self.base, "GET", "/reservations", token=self.token)[1]["reservations"]), 1)

    def test_50_distinct_keys_cannot_double_book(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            responses = list(pool.map(lambda i: self.create("distinct-"+str(i)), range(50)))
        self.assertEqual([s for s, _ in responses].count(201), 1)
        self.assertEqual([s for s, _ in responses].count(409), 49)
        self.assertTrue(all(s == 201 or body["error"]["code"] == "table_unavailable" for s, body in responses))

    def test_failed_keys_reusable_and_user_scoped(self):
        first = self.create("shared")[1]
        self.check_error(self.create("retry"), 409, "table_unavailable")
        self.assertEqual(self.create("retry", self.booking("t2"))[0], 201)
        self.assertEqual(request(self.base, "POST", "/reservations/"+first["reference"]+"/cancel",
                                 {}, self.token)[0], 200)
        self.assertEqual(self.create("shared", token=self.other)[0], 201)

    def test_replay_frozen_after_patch_cancel_and_json_reordering(self):
        body = self.booking(unknown={"a": [1, False, None]})
        original = self.create("replay", body)[1]
        ref = original["reference"]
        self.assertEqual(request(self.base, "PATCH", "/reservations/"+ref,
                                 {"table_id": "t2"}, self.token)[0], 200)
        self.assertEqual(request(self.base, "POST", "/reservations/"+ref+"/cancel", {}, self.token)[0], 200)
        self.assertEqual(self.create("replay", dict(reversed(list(body.items())))), (200, original))
        changed = dict(body, party_size=True)
        self.check_error(self.create("replay", changed), 409, "idempotency_key_reuse")

    def test_atomic_swap_noop_replay_and_rollback(self):
        a, b = self.create("a")[1], self.create("b", self.booking("t2"))[1]
        moves = {"moves": [{"reference": a["reference"], "table_id": "t2"},
                           {"reference": b["reference"], "table_id": "t1"}]}
        result = request(self.base, "POST", "/reservation-moves", moves, self.token, "swap")
        self.assertEqual(result[0], 201)
        self.assertEqual([r["table_id"] for r in result[1]["reservations"]], ["t2", "t1"])
        snapshot = request(self.base, "GET", "/_test/export")[1]
        bad = {"moves": [{"reference": a["reference"], "table_id": "t1"},
                         {"reference": b["reference"], "party_size": True}]}
        self.check_error(request(self.base, "POST", "/reservation-moves", bad, self.token, "rollback"),
                         422, "validation_failed")
        self.assertEqual(snapshot, request(self.base, "GET", "/_test/export")[1])
        noop = {"moves": [{"reference": a["reference"]}, {"reference": b["reference"]}]}
        self.assertEqual(request(self.base, "POST", "/reservation-moves", noop, self.token, "rollback")[0], 201)
        self.assertEqual(request(self.base, "POST", "/reservation-moves", moves, self.token, "swap"), (200, result[1]))

    def test_overlap_rollback_and_half_open_boundary(self):
        a = self.create("a")[1]
        self.assertEqual(self.create("b", self.booking(start=DAY+"T13:00"))[0], 201)
        snapshot = request(self.base, "GET", "/_test/export")[1]
        self.check_error(request(self.base, "PATCH", "/reservations/"+a["reference"],
                                {"starts_at_local": DAY+"T12:30"}, self.token), 409, "table_unavailable")
        self.assertEqual(snapshot, request(self.base, "GET", "/_test/export")[1])

    def test_login_email_format_precedes_credentials_and_preserves_state(self):
        before = request(self.base, "GET", "/_test/export")[1]
        for email in ("missing-at", "@domain", "local@", "a@@b", "a b@c", "", "local@do main"):
            self.check_error(request(self.base, "POST", "/auth/login",
                                    {"email": email, "password": "password"}), 422, "validation_failed")
        self.check_error(request(self.base, "POST", "/auth/login",
                                {"email": "unknown@example.com", "password": "password"}), 401, "unauthenticated")
        self.check_error(request(self.base, "POST", "/auth/login",
                                {"email": "one@example.com", "password": "incorrect"}), 401, "unauthenticated")
        self.check_error(request(self.base, "POST", "/auth/login",
                                {"email": False, "password": "password"}), 400, "malformed_request")
        self.assertEqual(request(self.base, "GET", "/_test/export")[1], before)
        self.assertEqual(request(self.base, "POST", "/auth/login",
                                {"email": "one@example.com", "password": "password1"})[0], 200)

    def test_owner_privacy_and_auth_sessions(self):
        a = self.create("a")[1]
        path = "/reservations/"+a["reference"]
        for method, suffix, body in (("GET", "", None), ("PATCH", "", {}), ("POST", "/cancel", {})):
            self.check_error(request(self.base, method, path+suffix, body, self.other), 404, "not_found")
        self.check_error(request(self.base, "POST", "/reservation-moves",
                                {"moves": [{"reference": a["reference"]}]}, self.other, "foreign"),
                         404, "not_found")
        token2 = request(self.base, "POST", "/auth/login",
                         {"email": "one@example.com", "password": "password1"})[1]["token"]
        self.assertEqual(request(self.base, "GET", path, token=token2)[0], 200)
        self.assertEqual(request(self.base, "GET", path, token=self.token)[0], 200)
        self.check_error(request(self.base, "GET", "/reservations"), 401, "unauthenticated")

    def test_portable_replacement_preserves_receipts_sessions_hashes(self):
        a = self.create("original")[1]
        batch = {"moves": [{"reference": a["reference"], "table_id": "t2"}]}
        moved = request(self.base, "POST", "/reservation-moves", batch, self.token, "batch")[1]
        snapshot = request(self.base, "GET", "/_test/export")[1]
        self.assertNotIn("password1", json.dumps(snapshot))
        self.assertEqual(request(self.base, "POST", "/reservations/"+a["reference"]+"/cancel", {}, self.token)[0], 200)
        request(self.dest, "POST", "/auth/signup", {"email": "extra@example.com", "password": "password3", "display_name": "Extra"})
        for _ in range(2):
            self.assertEqual(request(self.dest, "POST", "/_test/import", snapshot)[0], 204)
            self.assertEqual(request(self.dest, "POST", "/reservations", self.booking(), self.token, "original"), (200, a))
            self.assertEqual(request(self.dest, "POST", "/reservation-moves", batch, self.token, "batch"), (200, moved))
            self.assertEqual(request(self.dest, "GET", "/_test/export")[1], snapshot)
        self.check_error(request(self.dest, "POST", "/auth/login",
                                {"email": "extra@example.com", "password": "password3"}), 401, "unauthenticated")
        self.assertEqual(request(self.dest, "POST", "/auth/login",
                                 {"email": "one@example.com", "password": "password1"})[0], 200)
        request(self.dest, "POST", "/_test/reset", fixture())
        self.check_error(request(self.dest, "GET", "/reservations", token=self.token), 401, "unauthenticated")

    def test_invalid_import_does_not_change_destination(self):
        self.create("receipt")
        snapshot = request(self.base, "GET", "/_test/export")[1]
        cases = [{}, dict(snapshot, track="wrong"), dict(snapshot, format_version=True),
                 dict(snapshot, state={}), dict(snapshot, state={"users": []})]
        for path in ("password_hash", "receipt"):
            bad = copy.deepcopy(snapshot)
            if path == "password_hash":
                bad["state"]["users"]["u1"]["password_hash"]["salt"] = "bad"
            else:
                bad["state"]["receipts"][0]["response"]["reference"] = "MISSING"
            cases.append(bad)
        for bad in cases:
            self.check_error(request(self.base, "POST", "/_test/import", bad), 422, "validation_failed")
            self.assertEqual(request(self.base, "GET", "/_test/export")[1], snapshot)

    def test_dst_gap_fold_absolute_duration_both_zones(self):
        for zone, gap, fold, local, offset, end in [
            ("Europe/Berlin", "2026-03-29", "2026-10-25", "02:30", "+02:00", "03:00:00+01:00"),
            ("America/New_York", "2026-03-08", "2026-11-01", "01:30", "-04:00", "02:00:00-05:00")]:
            request(self.base, "POST", "/_test/reset", fixture(zone, "00:00", "06:00", 90))
            token = request(self.base, "POST", "/auth/login",
                            {"email": "one@example.com", "password": "password1"})[1]["token"]
            slots = request(self.base, "GET", "/availability?restaurant_id=r1&date="+gap+"&party_size=2")[1]["slots"]
            self.assertFalse(any(s["starts_at_local"][11:13] == "02" for s in slots))
            self.check_error(request(self.base, "POST", "/reservations",
                                    self.booking(start=gap+"T02:30"), token, "gap"), 422, "invalid_local_time")
            booked = request(self.base, "POST", "/reservations", self.booking(start=fold+"T"+local), token, "fold")
            self.assertEqual(booked[0], 201)
            self.assertTrue(booked[1]["starts_at"].endswith(offset))
            self.assertTrue(booked[1]["ends_at"].endswith(end))
            slots = request(self.base, "GET", "/availability?restaurant_id=r1&date="+fold+"&party_size=2")[1]["slots"]
            self.assertEqual(len([s for s in slots if s["starts_at_local"] == fold+"T"+local]), 1)

    def test_past_create_cutoff_and_cancelled_idempotence(self):
        past = self.create("past", self.booking(start="2020-01-06T12:00"))
        self.assertEqual(past[0], 201)
        ref = past[1]["reference"]
        self.check_error(request(self.base, "POST", "/reservations/"+ref+"/cancel", {}, self.token),
                         409, "cutoff_passed")
        self.check_error(request(self.base, "PATCH", "/reservations/"+ref, {"party_size": True}, self.token),
                         409, "cutoff_passed")
        self.check_error(request(self.base, "POST", "/reservation-moves",
                                {"moves": [{"reference": ref, "party_size": True}]}, self.token, "pastbatch"),
                         409, "cutoff_passed")
        future = self.create("future", self.booking("t2"))[1]["reference"]
        cancelled = request(self.base, "POST", "/reservations/"+future+"/cancel", {}, self.token)
        self.assertEqual(request(self.base, "POST", "/reservations/"+future+"/cancel", {}, self.token), cancelled)
        self.check_error(request(self.base, "PATCH", "/reservations/"+future, {}, self.token),
                         409, "reservation_cancelled")

    def test_types_formats_missing_fields_and_unknowns(self):
        self.check_error(request(self.base, "POST", "/reservations", token=self.token, key="json", raw=b"{"),
                         400, "malformed_request")
        for raw in (b"[]", b"null", b"true"):
            self.check_error(request(self.base, "POST", "/reservations", token=self.token, key="shape", raw=raw),
                             400, "malformed_request")
        for size in ("2", False, 0, -1, 2.5):
            self.check_error(self.create("invalid-size", dict(self.booking(), party_size=size)), 422, "validation_failed")
        self.check_error(self.create("wrong", dict(self.booking(), table_id=[])), 400, "malformed_request")
        self.check_error(self.create("wrong", dict(self.booking(), table_id="x"*65)), 422, "validation_failed")
        for start in ("2030-01-07T12:00Z", "2030-01-07T12:00:00", "2030-02-30T12:00"):
            self.check_error(self.create("format", dict(self.booking(), starts_at_local=start)), 422, "validation_failed")
        self.check_error(self.create("x"*256), 422, "validation_failed")
        self.check_error(request(self.base, "POST", "/reservations", self.booking(), self.token),
                         400, "missing_idempotency_key")
        for size in ("1e9", "4.0", "%2B4", "-1"):
            self.check_error(request(self.base, "GET", "/availability?restaurant_id=r1&date="+DAY+"&party_size="+size),
                             422, "validation_failed")
        self.assertEqual(self.create("unknown", self.booking(extra={"arbitrary": [False, None]}))[0], 201)
        self.assertEqual(request(self.base, "GET", "/availability?restaurant_id=r1&date="+DAY+"&party_size=2&unknown=true")[0], 200)

    def test_batch_shape_cross_path_and_error_order(self):
        a = self.create("same-key")[1]
        batch = {"moves": [{"reference": a["reference"]}]}
        self.assertEqual(request(self.base, "POST", "/reservation-moves", batch, self.token, "same-key")[0], 201)
        for moves in (None, [], [{}], [False], [{"reference": a["reference"]}]*2,
                      [{"reference": "UNKNOWN"}]*9):
            self.check_error(request(self.base, "POST", "/reservation-moves", {"moves": moves}, self.token, "shape"),
                             422, "validation_failed")
        b = self.create("b", self.booking("t2"))[1]
        bad = {"moves": [{"reference": a["reference"], "table_id": "t2"},
                         {"reference": b["reference"], "party_size": 0}]}
        self.check_error(request(self.base, "POST", "/reservation-moves", bad, self.token, "precedence"),
                         422, "validation_failed")

    def test_bodyless_cancel_and_exact_json_numbers(self):
        ref = self.create("empty-cancel")[1]["reference"]
        self.assertEqual(request(self.base, "POST", "/reservations/"+ref+"/cancel", token=self.token)[0], 200)
        raw = b'{"restaurant_id":"r1","table_id":"t1","starts_at_local":"2030-01-07T14:00","party_size":2,"unknown":1e999}'
        original = request(self.base, "POST", "/reservations", token=self.token, key="large-unknown", raw=raw)
        self.assertEqual(original[0], 201)
        self.assertEqual(request(self.base, "POST", "/reservations", token=self.token, key="large-unknown", raw=raw), (200, original[1]))
        self.check_error(request(self.base, "POST", "/reservations", token=self.token, key="different", raw=raw.replace(b'"party_size":2', b'"party_size":2.0000000000000000000001')), 422, "validation_failed")
        self.assertEqual(request(self.dest, "POST", "/_test/import", token=None, raw=request_exact_export(self.base))[0], 204)

    def test_calendar_years_and_historical_offset_rfc3339(self):
        for zone, date in [("UTC", "0001-01-01"), ("UTC", "9999-12-31"), ("Europe/Berlin", "1880-01-01")]:
            request(self.base, "POST", "/_test/reset", fixture(zone))
            token = request(self.base, "POST", "/auth/login", {"email": "one@example.com", "password": "password1"})[1]["token"]
            booking = request(self.base, "POST", "/reservations", self.booking(start=date+"T12:00"), token, "calendar")
            self.assertEqual(booking[0], 201)
            self.assertRegex(booking[1]["starts_at"], r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2}$")
            self.assertEqual(request(self.base, "GET", "/availability?restaurant_id=r1&date="+date+"&party_size=2")[0], 200)

    def test_seed_identity_fixture_order_and_reset_atomicity(self):
        seeded = fixture()
        seeded["restaurants"][0]["tables"].reverse()
        seeded["reservations"] = [dict(self.booking(), id="seed-id", reference="SEED01", user_id="u1")]
        self.assertEqual(request(self.base, "POST", "/_test/reset", seeded)[0], 204)
        token = request(self.base, "POST", "/auth/login",
                        {"email": "one@example.com", "password": "password1"})[1]["token"]
        self.assertEqual(request(self.base, "GET", "/reservations/SEED01", token=token)[1]["reservation_id"], "seed-id")
        slots = request(self.base, "GET", "/availability?restaurant_id=r1&date="+DAY+"&party_size=2")[1]["slots"]
        self.assertEqual(slots[0]["available_table_ids"], ["t2", "t1"])
        before = request(self.base, "GET", "/_test/export")[1]
        bad = copy.deepcopy(seeded)
        bad["restaurants"][0]["id"] = "x"*65
        self.check_error(request(self.base, "POST", "/_test/reset", bad), 422, "validation_failed")
        self.assertEqual(request(self.base, "GET", "/_test/export")[1], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
