"""Cumulative Stage 3 HTTP service. State mutations and snapshots are linearized by LOCK."""
import copy
from decimal import Decimal
import sys
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.set_int_max_str_digits(0)
UTC = timezone.utc
LOCK = threading.RLock()
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def fail(code="validation_failed", status=422):
    raise APIError(status, code)


def empty_state():
    return {"users": {}, "restaurants": {}, "reservations": {}, "tokens": {}, "receipts": []}


STATE = empty_state()


def obj(value):
    if not isinstance(value, dict):
        fail("malformed_request", 400)
    return value


def field(body, name, kind=str):
    if name not in body:
        fail()
    value = body[name]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        fail("malformed_request", 400)
    return value


def identifier(value):
    if not isinstance(value, str):
        fail("malformed_request", 400)
    if len(value) > 64:
        fail()
    return value


def party(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        fail()
    if isinstance(value, (float, Decimal)) and (not Decimal(value).is_finite() or value != int(value)):
        fail()
    if value < 1:
        fail()
    return int(value)


def integer(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        fail("malformed_request", 400)
    if isinstance(value, (float, Decimal)) and (not Decimal(value).is_finite() or value != int(value)):
        fail()
    if value < minimum:
        fail()
    return int(value)


def now():
    return datetime.now(UTC)


def stamp(value):
    # Historical IANA offsets can contain seconds; RFC 3339 cannot.
    # UTC preserves the exact instant while providing a legal explicit offset.
    if value.utcoffset().total_seconds() % 60:
        value = value.astimezone(UTC)
    return value.isoformat(timespec="seconds")


def parse_date(text):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        fail()
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        fail()


def local_datetime(text, zone):
    if not isinstance(text, str):
        fail("malformed_request", 400)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", text):
        fail()
    try:
        wall = datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError:
        fail()
    # fold=0 chooses the earlier occurrence; round-trip detects skipped times.
    result = wall.replace(tzinfo=zone, fold=0)
    if result.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != wall:
        fail("invalid_local_time")
    return result


def clock(text):
    if not isinstance(text, str):
        fail("malformed_request", 400)
    if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", text):
        fail()
    hour, minute = map(int, text.split(":"))
    if hour > 23 or minute > 59:
        fail()
    return hour * 60 + minute


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8", errors="surrogatepass"), salt=bytes.fromhex(salt), n=8192, r=8, p=1).hex()
    return {"algorithm": "scrypt-8192-8-1", "salt": salt, "digest": digest}


def password_matches(password, stored):
    digest = hashlib.scrypt(password.encode("utf-8", errors="surrogatepass"), salt=bytes.fromhex(stored["salt"]),
                            n=8192, r=8, p=1).hex()
    return hmac.compare_digest(digest, stored["digest"])


def email_valid(email):
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
        fail()


def json_equal(a, b):
    # JSON booleans are not numbers; numeric representations compare by value.
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return a == b


def restaurant(state, rid):
    identifier(rid)
    if rid not in state["restaurants"]:
        fail("not_found", 404)
    return state["restaurants"][rid]


def members(record):
    return record.get("table_ids", [record.get("table_id")])


def seating(r, body):
    if "table_id" in body and "table_ids" in body:
        fail()
    if "table_ids" in body:
        ids = field(body, "table_ids", list)
    else:
        ids = [identifier(field(body, "table_id"))]
    if not ids:
        fail()
    if len(ids) > 2:
        fail("combination_not_allowed")
    ids = [identifier(tid) for tid in ids]
    if len(set(ids)) != len(ids):
        fail()
    by_id = {t["id"]: t for t in r["tables"]}
    if any(tid not in by_id for tid in ids):
        fail("not_found", 404)
    if len(ids) == 2:
        pair = next((p for p in r.get("combinable", []) if set(p) == set(ids)), None)
        if pair is None:
            fail("combination_not_allowed")
        ids = list(pair)
    return ids, sum(by_id[tid]["capacity"] for tid in ids)


def record_proposed(state, record):
    body = {k: record[k] for k in ("starts_at_local", "party_size")}
    body["table_ids"] = members(record)
    return proposed(state, record["restaurant_id"], body)


def valid_record_shape(record, private=False):
    base = {"reservation_id", "reference", "restaurant_id", "party_size", "status",
            "starts_at_local", "starts_at", "ends_at", "created_at"}
    if private:
        base.add("user_id")
    if not isinstance(record, dict):
        fail()
    if set(record) - {"table_id", "table_ids"} != base:
        fail()
    ids = members(record)
    if not isinstance(ids, list) or not 1 <= len(ids) <= 2:
        fail()
    if "table_ids" in record and "table_id" in record and (len(ids) != 1 or record["table_id"] != ids[0]):
        fail()
    if "table_ids" not in record and "table_id" not in record:
        fail()


def fields_match(expected, record):
    # Legacy Stage 1 records/receipts omit table_ids; receipts remain verbatim.
    for key, value in expected.items():
        if key == "table_ids" and key not in record:
            if value != [record.get("table_id")]:
                return False
        elif not json_equal(value, record.get(key)):
            return False
    return True

def proposed(state, rid, body):
    r = restaurant(state, rid)
    ids, capacity = seating(r, body)
    size = party(body.get("party_size")) if "party_size" in body else fail()
    start = local_datetime(field(body, "starts_at_local"), ZoneInfo(r["timezone"]))
    hours = next((h for h in r["opening_hours"] if h["weekday"] == WEEKDAYS[start.weekday()]), None)
    if hours is None:
        fail("outside_opening_hours")
    wall_minute = start.hour * 60 + start.minute
    opens, closes = clock(hours["opens"]), clock(hours["closes"])
    if wall_minute < opens or wall_minute >= closes:
        fail("outside_opening_hours")
    if (wall_minute - opens) % r["slot_minutes"]:
        fail("not_on_slot_grid")
    try:
        closing = local_datetime(start.date().isoformat() + "T" + hours["closes"], start.tzinfo)
        if r["reservation_duration_minutes"] * 60 > (closing.astimezone(UTC) - start.astimezone(UTC)).total_seconds():
            fail("outside_opening_hours")
        end = (start.astimezone(UTC) + timedelta(minutes=r["reservation_duration_minutes"])).astimezone(start.tzinfo)
    except (OverflowError, ValueError):
        fail()
    if end.astimezone(UTC) > closing.astimezone(UTC):
        fail("outside_opening_hours")
    if size > capacity:
        fail("party_exceeds_capacity")
    result = {"restaurant_id": rid, "table_ids": ids, "party_size": size,
              "starts_at_local": body["starts_at_local"], "starts_at": stamp(start), "ends_at": stamp(end)}
    if len(ids) == 1:
        result["table_id"] = ids[0]
    return result


def overlap(a, b):
    return (a["status"] == b["status"] == "confirmed"
            and a["restaurant_id"] == b["restaurant_id"] and bool(set(members(a)) & set(members(b)))
            and datetime.fromisoformat(a["starts_at"]) < datetime.fromisoformat(b["ends_at"])
            and datetime.fromisoformat(b["starts_at"]) < datetime.fromisoformat(a["ends_at"]))


def occupancy(state, candidates, excluded=()):
    existing = [r for ref, r in state["reservations"].items() if ref not in excluded]
    for i, candidate in enumerate(candidates):
        if any(overlap(candidate, other) for other in existing + candidates[:i]):
            fail("table_unavailable", 409)


def public_reservation(r):
    return {k: v for k, v in r.items() if k != "user_id"}


def owned(state, reference, uid):
    r = state["reservations"].get(reference)
    if r is None or r["user_id"] != uid:
        fail("not_found", 404)
    return r


def cutoff(state, r):
    if r["status"] == "cancelled":
        fail("reservation_cancelled", 409)
    minutes = restaurant(state, r["restaurant_id"])["cancellation_cutoff_minutes"]
    # Use subtraction instead of adding arbitrarily large durations.
    if (datetime.fromisoformat(r["starts_at"]).astimezone(UTC) - now()).total_seconds() <= minutes * 60:
        fail("cutoff_passed", 409)


def amend(state, r, changes):
    cutoff(state, r)
    merged = {k: changes.get(k, r[k]) for k in ("starts_at_local", "party_size")}
    if "table_id" in changes or "table_ids" in changes:
        merged.update({k: changes[k] for k in ("table_id", "table_ids") if k in changes})
    else:
        merged["table_ids"] = members(r)
    updated = proposed(state, r["restaurant_id"], merged)
    if fields_match(updated, r):
        return dict(r)
    result = {k: v for k, v in r.items() if k not in ("table_id", "table_ids")}
    result.update(updated)
    return result


def build_fixture(body):
    obj(body)
    result = empty_state()
    for name in ("users", "restaurants", "reservations"):
        if not isinstance(body.get(name, []), list):
            fail("malformed_request", 400)
    emails = set()
    for user in body.get("users", []):
        obj(user)
        uid = identifier(field(user, "id"))
        email, password = field(user, "email"), field(user, "password")
        display = field(user, "display_name")
        email_valid(email)
        if uid in result["users"] or email in emails:
            fail()
        # Fixtures supply credentials, not a signup request.
        result["users"][uid] = {"id": uid, "email": email, "display_name": display,
                                "password_hash": password_hash(password)}
        emails.add(email)
    for raw in body.get("restaurants", []):
        obj(raw)
        rid = identifier(field(raw, "id"))
        zone = field(raw, "timezone")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            fail()
        r = {"id": rid, "name": field(raw, "name"), "timezone": zone,
             "slot_minutes": integer(field(raw, "slot_minutes", (int, float, Decimal)), 1),
             "reservation_duration_minutes": integer(field(raw, "reservation_duration_minutes", (int, float, Decimal)), 1),
             "cancellation_cutoff_minutes": integer(field(raw, "cancellation_cutoff_minutes", (int, float, Decimal)), 0),
             "opening_hours": [], "tables": [], "combinable": []}
        if rid in result["restaurants"]:
            fail()
        weekdays, tids = set(), set()
        for h in field(raw, "opening_hours", list):
            obj(h)
            weekday = field(h, "weekday")
            opens, closes = field(h, "opens"), field(h, "closes")
            if weekday not in WEEKDAYS or weekday in weekdays or clock(closes) <= clock(opens):
                fail()
            weekdays.add(weekday)
            r["opening_hours"].append({"weekday": weekday, "opens": opens, "closes": closes})
        for table in field(raw, "tables", list):
            obj(table)
            tid = identifier(field(table, "id"))
            if tid in tids:
                fail()
            tids.add(tid)
            r["tables"].append({"id": tid, "label": field(table, "label"),
                                "capacity": integer(field(table, "capacity", (int, float, Decimal)), 1)})
        pairs = raw.get("combinable", [])
        if not isinstance(pairs, list):
            fail("malformed_request", 400)
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2:
                fail()
            pair = [identifier(tid) for tid in pair]
            if pair[0] == pair[1] or any(tid not in tids for tid in pair):
                fail()
            r["combinable"].append(pair)
        result["restaurants"][rid] = r
    reservation_ids = set()
    for raw in body.get("reservations", []):
        obj(raw)
        rid = identifier(field(raw, "restaurant_id"))
        uid = identifier(field(raw, "user_id"))
        ref = field(raw, "reference")
        res_id = identifier(field(raw, "id"))
        if uid not in result["users"] or ref in result["reservations"] or res_id in reservation_ids:
            fail()
        if not re.fullmatch(r"[A-Z0-9]{6,12}", ref):
            fail()
        r = proposed(result, rid, raw)
        status = raw.get("status", "confirmed")
        if not isinstance(status, str):
            fail("malformed_request", 400)
        if status not in ("confirmed", "cancelled"):
            fail()
        r.update({"reservation_id": res_id, "reference": ref, "user_id": uid,
                  "status": status, "created_at": stamp(now())})
        occupancy(result, [r])
        result["reservations"][ref] = r
        reservation_ids.add(res_id)
    return result


def valid_snapshot(raw):
    """Validate the complete private format before replacing any live state."""
    try:
        if not isinstance(raw, dict) or set(raw) != set(empty_state()):
            fail()
        if not all(isinstance(raw[k], dict) for k in ("users", "restaurants", "reservations", "tokens")):
            fail()
        if not isinstance(raw["receipts"], list):
            fail()
        emails = set()
        for uid, u in raw["users"].items():
            if set(u) != {"id", "email", "display_name", "password_hash"} or u["id"] != uid:
                fail()
            identifier(uid)
            email_valid(field(u, "email"))
            field(u, "display_name")
            if u["email"] in emails:
                fail()
            emails.add(u["email"])
            p = u["password_hash"]
            if set(p) != {"algorithm", "salt", "digest"} or p["algorithm"] != "scrypt-8192-8-1":
                fail()
            if not re.fullmatch(r"[0-9a-f]{32}", p["salt"]) or not re.fullmatch(r"[0-9a-f]{128}", p["digest"]):
                fail()
        # Reuse fixture config validation without regenerating any account or booking.
        fixture = {"restaurants": list(raw["restaurants"].values())}
        normalized = build_fixture(fixture)["restaurants"]
        for rid, restaurant_record in raw["restaurants"].items():
            legacy = dict(restaurant_record)
            legacy.setdefault("combinable", [])
            if not json_equal(normalized.get(rid), legacy):
                fail()
        ids = set()
        for ref, r in raw["reservations"].items():
            valid_record_shape(r, private=True)
            if r["reference"] != ref or not re.fullmatch(r"[A-Z0-9]{6,12}", ref):
                fail()
            identifier(r["reservation_id"])
            if r["reservation_id"] in ids or r["user_id"] not in raw["users"]:
                fail()
            ids.add(r["reservation_id"])
            if r["status"] not in ("confirmed", "cancelled"):
                fail()
            expected = record_proposed(raw, r)
            if not fields_match(expected, r):
                fail()
            created = datetime.fromisoformat(r["created_at"])
            if created.tzinfo is None:
                fail()
        occupancy(empty_state(), list(raw["reservations"].values()))
        for token, uid in raw["tokens"].items():
            if not token or not isinstance(uid, str) or uid not in raw["users"]:
                fail()
        keys = set()
        for receipt in raw["receipts"]:
            if set(receipt) != {"user_id", "method", "path", "key", "body", "response"}:
                fail()
            if receipt["user_id"] not in raw["users"] or receipt["method"] != "POST":
                fail()
            if receipt["path"] not in ("/reservations", "/reservation-moves"):
                fail()
            if not isinstance(receipt["key"], str) or not 1 <= len(receipt["key"]) <= 255:
                fail()
            if not isinstance(receipt["body"], dict) or not isinstance(receipt["response"], dict):
                fail()
            key = tuple(receipt[k] for k in ("user_id", "method", "path", "key"))
            if key in keys:
                fail()
            keys.add(key)
            response_items = ([receipt["response"]] if receipt["path"] == "/reservations"
                              else receipt["response"].get("reservations"))
            if not isinstance(response_items, list) or not 1 <= len(response_items) <= 8:
                fail()
            for item in response_items:
                valid_record_shape(item)
                if item.get("reference") not in raw["reservations"]:
                    fail()
                current = raw["reservations"][item["reference"]]
                for k in ("reservation_id", "reference", "restaurant_id", "created_at"):
                    if item[k] != current[k]:
                        fail()
                if current["user_id"] != receipt["user_id"] or item["status"] != "confirmed":
                    fail()
                expected = record_proposed(raw, item)
                if not fields_match(expected, item):
                    fail()
        migrated = copy.deepcopy(raw)
        for restaurant_record in migrated["restaurants"].values():
            restaurant_record.setdefault("combinable", [])
        for record in migrated["reservations"].values():
            record.setdefault("table_ids", [record.get("table_id")])
        return migrated
    except (APIError, KeyError, TypeError, ValueError, OverflowError, AttributeError):
        fail()


def authenticate(state, headers):
    value = headers.get("Authorization", "")
    match = re.fullmatch(r"Bearer ([^\s]+)", value, re.IGNORECASE)
    if match is None or match[1] not in state["tokens"]:
        fail("unauthenticated", 401)
    return state["tokens"][match[1]]


def availability(state, query):
    for name in ("restaurant_id", "date", "party_size"):
        if name not in query:
            fail()
    date, size = query["date"][0], query["party_size"][0]
    parse_date(date)
    if not re.fullmatch(r"[0-9]+", size):
        fail()
    size = int(size)
    if size < 1:
        fail()
    r = restaurant(state, query["restaurant_id"][0])
    zone = ZoneInfo(r["timezone"])
    day = parse_date(date)
    hours = next((h for h in r["opening_hours"] if h["weekday"] == WEEKDAYS[day.weekday()]), None)
    response = {"restaurant_id": r["id"], "date": date, "timezone": r["timezone"], "slots": []}
    if hours is None:
        return response
    for minute in range(clock(hours["opens"]), clock(hours["closes"]), r["slot_minutes"]):
        local = date + "T" + f"{minute // 60:02}:{minute % 60:02}"
        try:
            start = local_datetime(local, zone)
            close = local_datetime(date + "T" + hours["closes"], zone)
            if r["reservation_duration_minutes"] * 60 > (close.astimezone(UTC) - start.astimezone(UTC)).total_seconds():
                continue
            end = (start.astimezone(UTC) + timedelta(minutes=r["reservation_duration_minutes"])).astimezone(zone)
        except APIError as exc:
            if exc.code == "invalid_local_time":
                continue
            raise
        if end.astimezone(UTC) > close.astimezone(UTC):
            continue
        available, options = [], []
        for t in r["tables"]:
            candidate = {"status": "confirmed", "restaurant_id": r["id"], "table_id": t["id"],
                         "starts_at": stamp(start), "ends_at": stamp(end)}
            if t["capacity"] >= size and not any(overlap(candidate, x) for x in state["reservations"].values()):
                available.append(t["id"])
                options.append({"table_ids": [t["id"]], "capacity": t["capacity"]})
        by_id = {t["id"]: t for t in r["tables"]}
        for pair in r.get("combinable", []):
            capacity = sum(by_id[tid]["capacity"] for tid in pair)
            candidate = {"status": "confirmed", "restaurant_id": r["id"], "table_ids": pair,
                         "starts_at": stamp(start), "ends_at": stamp(end)}
            if capacity >= size and not any(overlap(candidate, x) for x in state["reservations"].values()):
                options.append({"table_ids": list(pair), "capacity": capacity})
        response["slots"].append({"starts_at_local": local, "starts_at": stamp(start),
                                  "available_table_ids": available, "available_options": options})
    return response


def route(method, path, query, body, headers):
    global STATE
    state = STATE
    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}
    if method == "POST" and path == "/_test/reset":
        STATE = build_fixture(body)
        return 204, None
    if method == "GET" and path == "/_test/export":
        return 200, {"track": "tablekeeper", "format_version": 1, "state": copy.deepcopy(state)}
    if method == "POST" and path == "/_test/import":
        if body.get("track") != "tablekeeper" or type(body.get("format_version")) is not int or body["format_version"] != 1:
            fail()
        STATE = valid_snapshot(body.get("state"))
        return 204, None
    if method == "POST" and path in ("/auth/signup", "/auth/login"):
        email, password = field(body, "email"), field(body, "password")
        email_valid(email)
        if path == "/auth/signup":
            display = field(body, "display_name")
            if len(password) < 8:
                fail()
            if any(u["email"] == email for u in state["users"].values()):
                fail("email_taken", 409)
            uid = "u_" + secrets.token_hex(16)
            state["users"][uid] = {"id": uid, "email": email, "display_name": display,
                                   "password_hash": password_hash(password)}
            status = 201
        else:
            user = next((u for u in state["users"].values() if u["email"] == email), None)
            if user is None or not password_matches(password, user["password_hash"]):
                fail("unauthenticated", 401)
            uid, status = user["id"], 200
        token = secrets.token_urlsafe(32)
        state["tokens"][token] = uid
        return status, {"user_id": uid, "display_name": state["users"][uid]["display_name"], "token": token}
    if method == "GET" and path == "/restaurants":
        return 200, {"restaurants": [{k: r[k] for k in ("id", "name", "timezone")} for r in state["restaurants"].values()]}
    if method == "GET" and path.startswith("/restaurants/"):
        return 200, restaurant(state, unquote(path[len("/restaurants/"):]))
    if method == "GET" and path == "/availability":
        return 200, availability(state, query)
    uid = authenticate(state, headers)
    receipt_key = None
    if method == "POST" and path in ("/reservations", "/reservation-moves"):
        key = headers.get("Idempotency-Key")
        if not key:
            fail("missing_idempotency_key", 400)
        if len(key) > 255:
            fail()
        receipt_key = (uid, method, path, key)
        receipt = next((r for r in state["receipts"]
                        if tuple(r[k] for k in ("user_id", "method", "path", "key")) == receipt_key), None)
        if receipt:
            if not json_equal(receipt["body"], body):
                fail("idempotency_key_reuse", 409)
            return 200, copy.deepcopy(receipt["response"])
    if method == "GET" and path == "/reservations":
        records = [r for r in state["reservations"].values() if r["user_id"] == uid]
        records.sort(key=lambda r: datetime.fromisoformat(r["starts_at"]), reverse=True)
        return 200, {"reservations": [public_reservation(r) for r in records]}
    if method == "POST" and path == "/reservations":
        r = proposed(state, identifier(field(body, "restaurant_id")), body)
        r.update({"reservation_id": "res_" + secrets.token_hex(16), "user_id": uid,
                  "status": "confirmed", "created_at": stamp(now())})
        ref = secrets.token_hex(5).upper()
        while ref in state["reservations"]:
            ref = secrets.token_hex(5).upper()
        r["reference"] = ref
        occupancy(state, [r])
        state["reservations"][ref] = r
        response = public_reservation(r)
    elif method == "POST" and path == "/reservation-moves":
        moves = body.get("moves")
        if not isinstance(moves, list) or not 1 <= len(moves) <= 8:
            fail()
        refs = []
        for move in moves:
            if not isinstance(move, dict) or not isinstance(move.get("reference"), str):
                fail()
            if move["reference"] in refs:
                fail()
            refs.append(move["reference"])
        candidates, rid = [], None
        for move in moves:
            r = owned(state, move["reference"], uid)
            if rid is not None and r["restaurant_id"] != rid:
                fail()
            rid = r["restaurant_id"]
            candidates.append(amend(state, r, move))
        occupancy(state, candidates, refs)
        for r in candidates:
            state["reservations"][r["reference"]] = r
        response = {"reservations": [public_reservation(r) for r in candidates]}
    else:
        match = re.fullmatch(r"/reservations/([^/]+)(/cancel)?", path)
        if match is None:
            fail("not_found", 404)
        r = owned(state, unquote(match[1]), uid)
        if method == "GET" and not match[2]:
            return 200, public_reservation(r)
        if method == "POST" and match[2]:
            if r["status"] != "cancelled":
                cutoff(state, r)
                r["status"] = "cancelled"
            return 200, public_reservation(r)
        if method == "PATCH" and not match[2]:
            candidate = amend(state, r, body)
            occupancy(state, [candidate], [r["reference"]])
            state["reservations"][r["reference"]] = candidate
            return 200, public_reservation(candidate)
        fail("not_found", 404)
    state["receipts"].append(dict(zip(("user_id", "method", "path", "key"), receipt_key),
                                 body=copy.deepcopy(body), response=copy.deepcopy(response)))
    return 201, response


def encode_json(value):
    """Preserve arbitrary finite JSON numbers in opaque successful receipts."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + encode_json(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(encode_json(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # No bodies, headers, credentials or private exports are logged.
        pass

    def send_error(self, code, message=None, explain=None):
        if code == 501:
            code, error = 404, "not_found"
        else:
            error = "malformed_request"
        data = json.dumps({"error": {"code": error, "message": error.replace("_", " ")}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def handle_request(self):
        try:
            parsed = urlsplit(self.path)
            static = {"/": ("index.html", "text/html"), "/signup": ("index.html", "text/html"),
                      "/login": ("index.html", "text/html"), "/lookup": ("index.html", "text/html"),
                      "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            if self.command == "GET" and parsed.path in static:
                filename, content_type = static[parsed.path]
                data = Path(__file__).with_name(filename).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            body = {}
            if self.command in ("POST", "PATCH", "PUT"):
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size < 0:
                        fail("malformed_request", 400)
                    data = self.rfile.read(size)
                    if not data and re.fullmatch(r"/reservations/[^/]+/cancel", parsed.path):
                        data = b"{}"
                    body = json.loads(data.decode("utf-8"), parse_float=Decimal,
                                      parse_constant=lambda _: fail("malformed_request", 400))
                    obj(body)
                except (ValueError, UnicodeError, RecursionError):
                    fail("malformed_request", 400)
            with LOCK:
                status, response = route(self.command, parsed.path,
                                         parse_qs(parsed.query, keep_blank_values=True), body, self.headers)
                encoded = b"" if response is None else encode_json(response).encode("utf-8")
        except APIError as exc:
            status = exc.status
            encoded = json.dumps({"error": {"code": exc.code, "message": exc.code.replace("_", " ")}}).encode()
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, OSError):
            status = 422
            encoded = b'{"error":{"code":"validation_failed","message":"Invalid request"}}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = handle_request


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True



# Stage 3 keeps immutable policy snapshots separate from fixture configuration.
base_build_fixture = build_fixture
base_valid_snapshot = valid_snapshot
base_route = route
base_proposed = proposed
base_availability = availability

def terms_zero(r):
    return dict(policy_version=0, **{k: copy.deepcopy(r[k]) for k in
        ('slot_minutes', 'reservation_duration_minutes', 'cancellation_cutoff_minutes', 'opening_hours')},
        capacities={t['id']: t['capacity'] for t in r['tables']})

def selected_terms(state, r, date):
    eligible = [p for p in state.get('policies', {}).get(r['id'], []) if p['effective_from'] <= date]
    return copy.deepcopy({k: v for k, v in max(eligible, key=lambda p: (p['effective_from'], p['policy_version'])).items()
                          if k != 'effective_from'}) if eligible else terms_zero(r)

def policy_restaurant(r, terms):
    result = copy.deepcopy(r)
    for key in ('slot_minutes', 'reservation_duration_minutes', 'cancellation_cutoff_minutes', 'opening_hours'):
        result[key] = copy.deepcopy(terms[key])
    for table in result['tables']:
        table['capacity'] = terms['capacities'][table['id']]
    return result

def proposed(state, rid, body, frozen=None):
    r = restaurant(state, rid)
    text = field(body, 'starts_at_local')
    # Format and nonexistent-time validation precede policy lookup.
    local_datetime(text, ZoneInfo(r['timezone']))
    terms = copy.deepcopy(frozen) if frozen is not None else selected_terms(state, r, text[:10])
    view = dict(state, restaurants=dict(state['restaurants']))
    view['restaurants'][rid] = policy_restaurant(r, terms)
    result = base_proposed(view, rid, body)
    result.update(revision=1, accepted_terms=terms)
    return result

def cutoff(state, r):
    if r['status'] == 'cancelled':
        fail('reservation_cancelled', 409)
    minutes = r['accepted_terms']['cancellation_cutoff_minutes']
    if (datetime.fromisoformat(r['starts_at']).astimezone(UTC) - now()).total_seconds() <= minutes * 60:
        fail('cutoff_passed', 409)

def bounded(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or value != int(value) or not low <= value <= high:
        fail()
    return int(value)

def amend(state, r, changes):
    if 'expected_revision' in changes:
        expected = bounded(changes['expected_revision'], 1, float('inf'))
        if expected != r['revision']:
            fail('stale_revision', 409)
    cutoff(state, r)
    merged = {k: changes.get(k, r[k]) for k in ('starts_at_local', 'party_size')}
    if 'table_id' in changes or 'table_ids' in changes:
        merged.update({k: changes[k] for k in ('table_id', 'table_ids') if k in changes})
    else:
        merged['table_ids'] = members(r)
    ids, _ = seating(restaurant(state, r['restaurant_id']), merged)
    size = party(merged['party_size'])
    local_datetime(merged['starts_at_local'], ZoneInfo(restaurant(state, r['restaurant_id'])['timezone']))
    if ids == members(r) and size == r['party_size'] and merged['starts_at_local'] == r['starts_at_local']:
        return copy.deepcopy(r)
    result = {k: copy.deepcopy(v) for k, v in r.items() if k not in ('table_id', 'table_ids')}
    result.update(proposed(state, r['restaurant_id'], merged))
    result['revision'] = r['revision'] + 1
    return result

def event_entry(r, previous=None):
    event = 'created' if previous is None else ('cancelled' if r['status'] == 'cancelled' else 'changed')
    changes = []
    if event != 'cancelled':
        before_ids = members(previous) if previous else None
        ids = members(r)
        pair = len(ids) == 2 or (before_ids is not None and len(before_ids) == 2)
        values = [('table_ids' if pair else 'table_id', before_ids if pair else (before_ids[0] if before_ids else None), ids if pair else ids[0]),
                  ('starts_at_local', previous['starts_at_local'] if previous else None, r['starts_at_local']),
                  ('party_size', previous['party_size'] if previous else None, r['party_size'])]
        changes = [dict(field=k, **{'from': old, 'to': new}) for k, old, new in values if previous is None or old != new]
    return dict(at=r['created_at'] if previous is None else stamp(now()), event=event, changes=changes,
                revision=r['revision'], accepted_terms=copy.deepcopy(r['accepted_terms']))

def initialize_metadata(state):
    state.setdefault('policies', {rid: [] for rid in state['restaurants']})
    state.setdefault('histories', {})
    state.setdefault('series', {})
    state.setdefault('restaurant_revisions', {rid: 0 for rid in state['restaurants']})
    for r in state['restaurants'].values():
        r.setdefault('manager_user_ids', [])
    for ref, record in state['reservations'].items():
        record.setdefault('revision', 1)
        record.setdefault('accepted_terms', terms_zero(state['restaurants'][record['restaurant_id']]))
        if ref not in state['histories']:
            entry = event_entry(record)
            entry['seq'] = 1
            state['histories'][ref] = [entry]
    return state

def build_fixture(body):
    result = base_build_fixture(body)
    for raw in body.get('restaurants', []):
        managers = raw.get('manager_user_ids', [])
        if not isinstance(managers, list):
            fail('malformed_request', 400)
        managers = [identifier(uid) for uid in managers]
        result['restaurants'][raw['id']]['manager_user_ids'] = managers
    return initialize_metadata(result)

def policy_body(r, body):
    try:
        if not isinstance(body, dict):
            fail()
        date = body['effective_from']
        if not isinstance(date, str):
            fail()
        parse_date(date)
        result = {'effective_from': date}
        for key, low, high in [('slot_minutes', 1, 1440), ('reservation_duration_minutes', 1, 1440), ('cancellation_cutoff_minutes', 0, 10080)]:
            result[key] = bounded(body[key], low, high)
        hours = body['opening_hours']
        if not isinstance(hours, list):
            fail()
        result['opening_hours'] = []
        days = set()
        for h in hours:
            if not isinstance(h, dict) or h.get('weekday') not in WEEKDAYS or h['weekday'] in days:
                fail()
            if clock(h['closes']) <= clock(h['opens']):
                fail()
            days.add(h['weekday'])
            result['opening_hours'].append({k: h[k] for k in ('weekday', 'opens', 'closes')})
        capacities = body['capacities']
        if not isinstance(capacities, dict) or set(capacities) != {t['id'] for t in r['tables']}:
            fail()
        result['capacities'] = {tid: bounded(value, 1, 100) for tid, value in capacities.items()}
        return result
    except (APIError, KeyError, TypeError, ValueError, OverflowError):
        fail()

def availability(state, query):
    if 'explain' in query and query['explain'] != ['true']:
        fail()
    for name in ('restaurant_id', 'date', 'party_size'):
        if name not in query:
            fail()
    date = query['date'][0]
    parse_date(date)
    r = restaurant(state, query['restaurant_id'][0])
    terms = selected_terms(state, r, date)
    view = dict(state, restaurants=dict(state['restaurants']))
    view['restaurants'][r['id']] = policy_restaurant(r, terms)
    result = base_availability(view, query)
    if 'explain' in query:
        size = int(query['party_size'][0])
        for slot in result['slots']:
            start = datetime.fromisoformat(slot['starts_at'])
            end = start.astimezone(UTC) + timedelta(minutes=terms['reservation_duration_minutes'])
            slot['explain'] = []
            for table in r['tables']:
                capacity = size <= terms['capacities'][table['id']]
                candidate = dict(status='confirmed', restaurant_id=r['id'], table_id=table['id'], starts_at=slot['starts_at'], ends_at=stamp(end))
                free = not any(overlap(candidate, x) for x in state['reservations'].values())
                slot['explain'].append(dict(table_id=table['id'], policy_version=terms['policy_version'], available=capacity and free,
                    rules=[dict(rule='capacity', holds=capacity), dict(rule='no_overlap', holds=free)]))
    return result


def series_public(state, series):
    return {k: copy.deepcopy(series[k]) for k in ('series_id', 'revision', 'interval_weeks')} | {
        'occurrences': [dict(index=i, reference=ref, exception=ref in series['exceptions'],
                             reservation=public_reservation(state['reservations'][ref]))
                        for i, ref in enumerate(series['references'])]}

def retry(state, uid, method, path, body, headers):
    key = headers.get('Idempotency-Key')
    if not key:
        fail('missing_idempotency_key', 400)
    if len(key) > 255:
        fail()
    identity = (uid, method, path, key)
    receipt = next((r for r in state['receipts'] if tuple(r[k] for k in ('user_id','method','path','key')) == identity), None)
    if receipt and not json_equal(receipt['body'], body):
        fail('idempotency_key_reuse', 409)
    return identity, copy.deepcopy(receipt['response']) if receipt else None

def save_receipt(state, identity, body, response):
    state['receipts'].append(dict(zip(('user_id','method','path','key'), identity), body=copy.deepcopy(body), response=copy.deepcopy(response)))

def valid_snapshot(raw):
    global build_fixture, proposed
    try:
        if not isinstance(raw, dict):
            fail()
        if 'histories' not in raw:
            # Validate legacy opaque format using its original rules, then migrate.
            current_builder, current_proposed = build_fixture, proposed
            build_fixture, proposed = base_build_fixture, base_proposed
            try:
                migrated = base_valid_snapshot(raw)
            finally:
                build_fixture, proposed = current_builder, current_proposed
            return initialize_metadata(migrated)
        if set(raw) != {'users','restaurants','reservations','tokens','receipts','policies','histories','series','restaurant_revisions'}:
            fail()
        result = copy.deepcopy(raw)
        if not all(isinstance(result[k], dict) for k in set(result) - {'receipts'}):
            fail()
        # Validate credentials and fixture rules without regenerating identities.
        legacy = {k: copy.deepcopy(result[k]) for k in ('users','restaurants','tokens')}
        legacy.update(reservations={}, receipts=[])
        for r in legacy['restaurants'].values():
            r.pop('manager_user_ids', None)
        current_builder, current_proposed = build_fixture, proposed
        build_fixture, proposed = base_build_fixture, base_proposed
        try:
            base_valid_snapshot(legacy)
        finally:
            build_fixture, proposed = current_builder, current_proposed
        if set(result['policies']) != set(result['restaurants']) or set(result['restaurant_revisions']) != set(result['restaurants']):
            fail()
        for rid, r in result['restaurants'].items():
            managers = r.get('manager_user_ids')
            if not isinstance(managers, list) or any(not isinstance(uid,str) or len(uid)>64 for uid in managers):
                fail()
            bounded(result['restaurant_revisions'][rid], 0, float('inf'))
            policies = result['policies'][rid]
            if not isinstance(policies,list):
                fail()
            for version,p in enumerate(policies,1):
                if p != dict(policy_body(r,p), policy_version=version):
                    fail()
        if set(result['histories']) != set(result['reservations']):
            fail()
        ids=set()
        for ref,r in result['reservations'].items():
            if ref != r['reference'] or not re.fullmatch(r'[A-Z0-9]{6,12}',ref) or r['user_id'] not in result['users']:
                fail()
            base_fields={'reservation_id','reference','restaurant_id','party_size','status','starts_at_local','starts_at','ends_at','created_at','user_id','revision','accepted_terms','table_ids'}
            if set(r) != base_fields | ({'table_id'} if len(members(r))==1 else set()):
                fail()
            identifier(r['reservation_id'])
            if r['reservation_id'] in ids or r['status'] not in ('confirmed','cancelled'):
                fail()
            ids.add(r['reservation_id'])
            bounded(r['revision'],1,float('inf'))
            config=restaurant(result,r['restaurant_id'])
            terms=r['accepted_terms']
            known=[terms_zero(config)]+[{k:v for k,v in p.items() if k!='effective_from'} for p in result['policies'][config['id']]]
            if terms not in known:
                fail()
            expected=proposed(result,r['restaurant_id'],dict(table_ids=members(r), starts_at_local=r['starts_at_local'],party_size=r['party_size']), frozen=terms)
            expected.pop('revision')
            if not fields_match(expected,r) or datetime.fromisoformat(r['created_at']).tzinfo is None:
                fail()
            entries=result['histories'][ref]
            if not isinstance(entries,list) or not entries or entries[0]['event']!='created':
                fail()
            prior=None
            for seq,e in enumerate(entries,1):
                if set(e)!={'seq','at','event','changes','revision','accepted_terms'} or e['seq']!=seq or e['revision']!=seq:
                    fail()
                instant=datetime.fromisoformat(e['at'])
                if instant.tzinfo is None or (prior is not None and instant<prior) or e['accepted_terms'] not in known:
                    fail()
                if e['event'] not in ('created','changed','cancelled') or (seq>1 and e['event']=='created') or (e['event']=='cancelled' and seq!=len(entries)):
                    fail()
                if not isinstance(e['changes'],list):
                    fail()
                fields=[c.get('field') if isinstance(c,dict) else None for c in e['changes']]
                if fields not in ([],['table_id'],['table_ids'],['starts_at_local'],['party_size'],
                    ['table_id','starts_at_local'],['table_ids','starts_at_local'],['table_id','party_size'],
                    ['table_ids','party_size'],['starts_at_local','party_size'],
                    ['table_id','starts_at_local','party_size'],['table_ids','starts_at_local','party_size']):
                    fail()
                if e['event']=='created' and fields not in (['table_id','starts_at_local','party_size'],['table_ids','starts_at_local','party_size']):
                    fail()
                if (e['event']=='changed' and not fields) or (e['event']=='cancelled' and fields):
                    fail()
                for c in e['changes']:
                    if set(c)!={'field','from','to'} or (e['event']=='created' and c['from'] is not None) or (e['event']=='changed' and c['from']==c['to']):
                        fail()
                prior=instant
            if entries[-1]['revision']!=r['revision'] or entries[-1]['accepted_terms']!=terms:
                fail()
        occupancy(empty_state(),list(result['reservations'].values()))
        adopted=set()
        for sid,s in result['series'].items():
            if set(s)!={'series_id','user_id','revision','interval_weeks','references','exceptions'} or s['series_id']!=sid or s['user_id'] not in result['users']:
                fail()
            identifier(sid)
            bounded(s['revision'],1,float('inf')); bounded(s['interval_weeks'],1,4)
            if not isinstance(s['references'],list) or not 2<=len(s['references'])<=12 or len(set(s['references']))!=len(s['references']):
                fail()
            if not isinstance(s['exceptions'],list) or len(set(s['exceptions']))!=len(s['exceptions']) or not set(s['exceptions'])<=set(s['references']):
                fail()
            if len({result['reservations'][ref]['restaurant_id'] for ref in s['references'] if ref in result['reservations']})!=1:
                fail()
            for ref in s['references']:
                if ref in adopted or ref not in result['reservations'] or result['reservations'][ref]['user_id']!=s['user_id']:
                    fail()
                adopted.add(ref)
        if not isinstance(result['receipts'],list):
            fail()
        keys=set()
        for receipt in result['receipts']:
            if set(receipt)!={'user_id','method','path','key','body','response'} or receipt['user_id'] not in result['users'] or receipt['method']!='POST':
                fail()
            path=receipt['path']
            if path not in ('/reservations','/reservation-moves','/series') and not re.fullmatch(r'/restaurants/[^/]+/policies',path):
                fail()
            if not isinstance(receipt['key'],str) or not 1<=len(receipt['key'])<=255 or not isinstance(receipt['body'],dict) or not isinstance(receipt['response'],dict):
                fail()
            key=tuple(receipt[k] for k in ('user_id','method','path','key'))
            if key in keys:
                fail()
            keys.add(key)
            response=receipt['response']
            if path in ('/reservations','/reservation-moves'):
                items=[response] if path=='/reservations' else response.get('reservations')
                if not isinstance(items,list) or not 1<=len(items)<=8:
                    fail()
                for item in items:
                    if not isinstance(item,dict) or item.get('reference') not in result['reservations']:
                        fail()
                    current=result['reservations'][item['reference']]
                    if current['user_id']!=receipt['user_id'] or item.get('status')!='confirmed':
                        fail()
                    for name in ('reservation_id','restaurant_id','created_at'):
                        if item.get(name)!=current[name]:
                            fail()
                    terms=item.get('accepted_terms',terms_zero(result['restaurants'][current['restaurant_id']]))
                    expected=proposed(result,current['restaurant_id'],dict(table_ids=members(item),starts_at_local=item['starts_at_local'],party_size=item['party_size']),frozen=terms)
                    expected.pop('revision')
                    if 'accepted_terms' not in item:
                        expected.pop('accepted_terms')
                    if not fields_match(expected,item):
                        fail()
            elif path=='/series':
                if response.get('series_id') not in result['series']:
                    fail()
            else:
                rid=unquote(path.split('/')[2])
                if response not in result['policies'].get(rid,[]):
                    fail()
        return result
    except (APIError,KeyError,ValueError,TypeError,OverflowError,AttributeError):
        fail()

def route(method, path, query, body, headers):
    global STATE
    # Copy-on-write protects all records, histories, counters and retry claims.
    before = STATE
    is_write = method in ('POST','PATCH','PUT','DELETE')
    if is_write:
        STATE=copy.deepcopy(before)
    try:
        result=stage3_route(method,path,query,body,headers)
        return result
    except Exception:
        STATE=before
        raise

def stage3_route(method,path,query,body,headers):
    state=STATE
    if method=='GET' and (re.fullmatch(r'/reservations/[^/]+/(history|decision)',path) or re.fullmatch(r'/series/[^/]+',path)):
        try:
            uid=authenticate(state,headers)
        except APIError:
            fail('not_found',404)
        if path.startswith('/series/'):
            s=state['series'].get(unquote(path.split('/')[2]))
            if s is None or s['user_id']!=uid:
                fail('not_found',404)
            return 200,series_public(state,s)
        ref=unquote(path.split('/')[2]); r=owned(state,ref,uid)
        if path.endswith('/history'):
            return 200,dict(reference=ref,entries=copy.deepcopy(state['histories'][ref]))
        return 200,dict(reference=ref,revision=r['revision'],accepted_terms=copy.deepcopy(r['accepted_terms']))
    policy_match=re.fullmatch(r'/restaurants/([^/]+)/policies',path)
    if policy_match:
        if method=='GET':
            r=restaurant(state,unquote(policy_match[1]))
            return 200,dict(policies=copy.deepcopy(state['policies'][r['id']]))
        if method=='POST':
            uid=authenticate(state,headers)
            identity,replay=retry(state,uid,method,path,body,headers)
            if replay is not None:
                return 200,replay
            r=restaurant(state,unquote(policy_match[1]))
            if uid not in r['manager_user_ids']:
                fail('forbidden',403)
            p=policy_body(r,body)
            p['policy_version']=len(state['policies'][r['id']])+1
            state['policies'][r['id']].append(p)
            state['restaurant_revisions'][r['id']]+=1
            save_receipt(state,identity,body,p)
            return 201,p
    if method=='POST' and path=='/series':
        uid=authenticate(state,headers)
        identity,replay=retry(state,uid,method,path,body,headers)
        if replay is not None:
            return 200,replay
        anchor=owned(state,identifier(field(body,'anchor_reference')),uid)
        if anchor['status']=='cancelled':
            fail('reservation_cancelled',409)
        if any(anchor['reference'] in s['references'] for s in state['series'].values()):
            fail('already_in_series',409)
        cutoff(state,anchor)
        count=bounded(body.get('count'),2,12)
        interval=bounded(body.get('interval_weeks'),1,4)
        wall=datetime.strptime(anchor['starts_at_local'],'%Y-%m-%dT%H:%M')
        generated=[]
        for index in range(1,count):
            start=wall+timedelta(days=index*interval*7)
            candidate=proposed(state,anchor['restaurant_id'],dict(table_ids=members(anchor),party_size=anchor['party_size'],starts_at_local=start.isoformat(timespec='minutes')))
            ref=secrets.token_hex(5).upper()
            while ref in state['reservations'] or any(x['reference']==ref for x in generated):
                ref=secrets.token_hex(5).upper()
            candidate.update(reference=ref,reservation_id='res_'+secrets.token_hex(16),user_id=uid,status='confirmed',created_at=stamp(now()))
            occupancy(state,[candidate]+generated)
            generated.append(candidate)
        for r in generated:
            state['reservations'][r['reference']]=r
            entry=event_entry(r); entry['seq']=1
            state['histories'][r['reference']]=[entry]
        sid='ser_'+secrets.token_hex(16)
        s=dict(series_id=sid,user_id=uid,revision=1,interval_weeks=interval,references=[anchor['reference']]+[r['reference'] for r in generated],exceptions=[])
        state['series'][sid]=s
        state['restaurant_revisions'][anchor['restaurant_id']]+=1
        response=series_public(state,s)
        save_receipt(state,identity,body,response)
        return 201,response
    # Original routes retain their authentication and receipt precedence.
    old_records=copy.deepcopy(state['reservations'])
    status,response=base_route(method,path,query,body,headers)
    if path in ('/_test/reset','/_test/import'):
        return status,response
    affected_series=set(); affected_restaurants=set()
    for ref,r in state['reservations'].items():
        old=old_records.get(ref)
        if old is None:
            entry=event_entry(r)
            entry['seq']=1
            state['histories'][ref]=[entry]
            affected_restaurants.add(r['restaurant_id'])
        elif r!=old:
            if r['status']!=old['status']:
                r['revision']=old['revision']+1
            entry=event_entry(r,old)
            entry['seq']=len(state['histories'][ref])+1
            # Monotonic event order even across clock adjustments.
            if entry['at']<state['histories'][ref][-1]['at']:
                entry['at']=state['histories'][ref][-1]['at']
            state['histories'][ref].append(entry)
            affected_restaurants.add(r['restaurant_id'])
            for sid,s in state['series'].items():
                if ref in s['references']:
                    affected_series.add(sid)
                    if r['status']!='cancelled' and ref not in s['exceptions']:
                        s['exceptions'].append(ref)
    for rid in affected_restaurants:
        state['restaurant_revisions'][rid]+=1
    for sid in affected_series:
        state['series'][sid]['revision']+=1
    # Cancel responses were constructed before the revision update.
    if method=='POST' and re.fullmatch(r'/reservations/[^/]+/cancel',path):
        response=public_reservation(state['reservations'][unquote(path.split('/')[2])])
    return status,response

STATE=initialize_metadata(STATE)

if __name__ == "__main__":
    Server(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
