"""Cumulative Stage 4 HTTP service. State mutations and snapshots are linearized by LOCK."""
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


def validate_saved_reservation(state, item, uid, creation=False):
    if not isinstance(item, dict) or item.get('reference') not in state['reservations']:
        fail()
    current = state['reservations'][item['reference']]
    if current['user_id'] != uid or item.get('status') != 'confirmed':
        fail()
    for name in ('reservation_id', 'restaurant_id', 'created_at'):
        if item.get(name) != current[name]:
            fail()
    enhanced = 'revision' in item or 'accepted_terms' in item
    shape = {k: v for k, v in item.items() if k not in ('revision', 'accepted_terms')}
    valid_record_shape(shape)
    r = state['restaurants'][current['restaurant_id']]
    if enhanced:
        if 'revision' not in item or 'accepted_terms' not in item:
            fail()
        revision = bounded(item['revision'], 1, current['revision'])
        if creation and revision != 1:
            fail()
        historical = state['histories'][item['reference']][revision - 1]
        if historical['event'] == 'cancelled' or not json_equal(item['accepted_terms'], historical['accepted_terms']):
            fail()
        # Reconstruct the seating/time/party values at the frozen revision.
        values = {}
        for event in state['histories'][item['reference']][:revision]:
            for change in event['changes']:
                key = change['field']
                if key == 'table_id':
                    values['table_ids'] = [change['to']]
                else:
                    values[key] = change['to']
        if members(item) != values.get('table_ids') or any(item.get(k) != values.get(k) for k in ('starts_at_local','party_size')):
            fail()
        terms = item['accepted_terms']
    else:
        # Migrated Stage 1/2 receipts deliberately retain their original schema.
        terms = terms_zero(r)
    expected = proposed(state, current['restaurant_id'], dict(table_ids=members(item),
                        starts_at_local=item['starts_at_local'], party_size=item['party_size']), frozen=terms)
    expected.pop('revision')
    if not enhanced:
        expected.pop('accepted_terms')
    if not fields_match(expected, item):
        fail()

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
                if not json_equal(p, dict(policy_body(r,p), policy_version=version)):
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
            if not any(json_equal(terms, p) for p in known):
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
                if set(e)!={'seq','at','event','changes','revision','accepted_terms'} or type(e['seq']) is not int or type(e['revision']) is not int or e['seq']!=seq or e['revision']!=seq:
                    fail()
                instant=datetime.fromisoformat(e['at'])
                if instant.tzinfo is None or (prior is not None and instant<prior) or not any(json_equal(e['accepted_terms'], p) for p in known):
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
            if entries[-1]['revision']!=r['revision'] or not json_equal(entries[-1]['accepted_terms'],terms):
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
                if path=='/reservation-moves':
                    moves=receipt['body'].get('moves')
                    if not isinstance(moves,list) or len(moves)!=len(items) or any(not isinstance(move,dict) for move in moves):
                        fail()
                    if [move.get('reference') for move in moves]!=[item.get('reference') for item in items]:
                        fail()
                    for move,item in zip(moves,items):
                        if 'revision' not in item and 'accepted_terms' not in item:
                            continue
                        r=result['restaurants'][item['restaurant_id']]
                        if 'table_id' in move or 'table_ids' in move:
                            if seating(r,move)[0]!=members(item):
                                fail()
                        if 'party_size' in move and party(move['party_size'])!=item['party_size']:
                            fail()
                        if 'starts_at_local' in move and move['starts_at_local']!=item['starts_at_local']:
                            fail()
                        if 'expected_revision' in move:
                            expected_revision=bounded(move['expected_revision'],1,float('inf'))
                            if expected_revision not in (item.get('revision'),item.get('revision',0)-1):
                                fail()
                elif 'revision' in items[0] or 'accepted_terms' in items[0]:
                    item=items[0]
                    if receipt['body'].get('restaurant_id')!=item['restaurant_id']:
                        fail()
                    terms=item.get('accepted_terms',terms_zero(result['restaurants'][item['restaurant_id']]))
                    expected=proposed(result,item['restaurant_id'],receipt['body'],frozen=terms)
                    expected.pop('revision')
                    if 'accepted_terms' not in item:
                        expected.pop('accepted_terms')
                    if not fields_match(expected,item):
                        fail()
                for item in items:
                    validate_saved_reservation(result,item,receipt['user_id'],creation=path=='/reservations')
            elif path=='/series':
                if response.get('series_id') not in result['series']:
                    fail()
                series=result['series'][response['series_id']]
                if set(response)!={'series_id','revision','interval_weeks','occurrences'} or series['user_id']!=receipt['user_id']:
                    fail()
                if bounded(response['revision'],1,1)!=1 or bounded(response['interval_weeks'],1,4)!=series['interval_weeks']:
                    fail()
                occurrences=response['occurrences']
                if not isinstance(occurrences,list) or len(occurrences)!=len(series['references']):
                    fail()
                if receipt['body'].get('anchor_reference')!=series['references'][0] or bounded(receipt['body'].get('count'),2,12)!=len(occurrences) or bounded(receipt['body'].get('interval_weeks'),1,4)!=series['interval_weeks']:
                    fail()
                for index,occurrence in enumerate(occurrences):
                    if not isinstance(occurrence,dict) or set(occurrence)!={'index','reference','exception','reservation'}:
                        fail()
                    if type(occurrence['index']) is not int or occurrence['index']!=index or occurrence['reference']!=series['references'][index] or occurrence['exception'] is not False:
                        fail()
                    if not isinstance(occurrence['reservation'],dict) or 'revision' not in occurrence['reservation'] or 'accepted_terms' not in occurrence['reservation'] or occurrence['reservation'].get('reference')!=occurrence['reference']:
                        fail()
                    validate_saved_reservation(result,occurrence['reservation'],receipt['user_id'],creation=index>0)
            else:
                rid=unquote(path.split('/')[2])
                if not any(json_equal(response, p) for p in result['policies'].get(rid, [])):
                    fail()
                if not json_equal({k:v for k,v in response.items() if k!='policy_version'}, policy_body(result['restaurants'][rid],receipt['body'])):
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


# Final-stage repairs and recurring edits use the same copy-on-write transaction.
stage3_dispatch = stage3_route
stage3_fixture = build_fixture
stage3_snapshot = valid_snapshot
stage3_availability = availability
stage3_occupancy = occupancy

def interval(raw):
    try:
        start, end = raw['from'], raw['to']
        instants=[]
        for text in (start,end):
            if not isinstance(text,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})',text):
                fail()
            instant=datetime.fromisoformat(text.replace('Z','+00:00'))
            if instant.tzinfo is None:
                fail()
            instants.append(instant)
        if instants[0]>=instants[1]:
            fail()
        return instants
    except (KeyError,TypeError,ValueError,OverflowError):
        fail()

def intervals_overlap(a_start,a_end,b_start,b_end):
    return a_start<b_end and b_start<a_end

def closed(record,closure):
    return (record['status']=='confirmed' and closure['table_id'] in members(record) and
            intervals_overlap(datetime.fromisoformat(record['starts_at']),datetime.fromisoformat(record['ends_at']),*interval(closure)))

def closure_conflict(state,record,extra=None):
    return any(closed(record,c) for c in state.get('closures',{}).get(record['restaurant_id'],[])) or (extra is not None and closed(record,extra))

def occupancy(state,candidates,excluded=()):
    stage3_occupancy(state,candidates,excluded)
    if any(closure_conflict(state,r) for r in candidates):
        fail('table_unavailable',409)

def initialize_final(state):
    state.setdefault('closures',{rid:[] for rid in state['restaurants']})
    state.setdefault('plans',{})
    for sid,s in state['series'].items():
        if 'scheduled_dates' not in s:
            receipt=next((r for r in state['receipts'] if r['path']=='/series' and r['response'].get('series_id')==sid),None)
            if receipt is None:
                fail()
            s['scheduled_dates']=[o['reservation']['starts_at_local'][:10] for o in receipt['response']['occurrences']]
    return state

def build_fixture(body):
    return initialize_revision_log(initialize_final(stage3_fixture(body)))

def availability(state,query):
    response=stage3_availability(state,query)
    r=restaurant(state,response['restaurant_id'])
    terms=selected_terms(state,r,response['date'])
    for slot in response['slots']:
        end=stamp(datetime.fromisoformat(slot['starts_at']).astimezone(UTC)+timedelta(minutes=terms['reservation_duration_minutes']))
        def option_closed(ids):
            return closure_conflict(state,dict(status='confirmed',restaurant_id=r['id'],table_ids=ids,starts_at=slot['starts_at'],ends_at=end))
        slot['available_table_ids']=[tid for tid in slot['available_table_ids'] if not option_closed([tid])]
        slot['available_options']=[o for o in slot['available_options'] if not option_closed(o['table_ids'])]
        for explanation in slot.get('explain',[]):
            if option_closed([explanation['table_id']]):
                explanation['rules'][1]['holds']=False
                explanation['available']=False
    return response

def set_tables(record,ids):
    result=copy.deepcopy(record)
    result['table_ids']=list(ids)
    result.pop('table_id',None)
    if len(ids)==1:
        result['table_id']=ids[0]
    return result

def optimize(state,r,closure):
    start,end=interval(closure)
    bookings=sorted((record for record in state['reservations'].values() if record['restaurant_id']==r['id'] and record['status']=='confirmed' and
        intervals_overlap(datetime.fromisoformat(record['starts_at']),datetime.fromisoformat(record['ends_at']),start,end)),key=lambda b:b['reference'])
    if len(r['tables'])>6 or len(r['combinable'])>4 or len(bookings)>6:
        fail('planning_limit')
    fixed=[record for record in state['reservations'].values() if record['restaurant_id']==r['id'] and record['reference'] not in {b['reference'] for b in bookings}]
    options=[[t['id']] for t in r['tables']]+r['combinable']
    choices=[]
    for b in bookings:
        candidates=[]
        for rank,ids in enumerate(options):
            capacity=sum(b['accepted_terms']['capacities'][tid] for tid in ids)
            if capacity<b['party_size']:
                continue
            candidate=set_tables(b,ids)
            if closure_conflict(state,candidate,closure) or any(overlap(candidate,other) for other in fixed):
                continue
            candidates.append((int(set(ids)!=set(members(b))),capacity-b['party_size'],rank,candidate))
        if not candidates:
            fail('no_feasible_plan',409)
        candidates.sort(key=lambda c:c[:3])
        choices.append(candidates)
    best=None;best_records=None
    minimum_changes=[0]*(len(bookings)+1);minimum_unused=[0]*(len(bookings)+1)
    for i in range(len(bookings)-1,-1,-1):
        minimum_changes[i]=minimum_changes[i+1]+min(c[0] for c in choices[i])
        minimum_unused[i]=minimum_unused[i+1]+min(c[1] for c in choices[i])
    def search(index,changed,unused,ranks,assigned):
        nonlocal best,best_records
        if best is not None:
            lower_changes=changed+minimum_changes[index]
            if lower_changes>best[0] or (lower_changes==best[0] and unused+minimum_unused[index]>best[1]):
                return
        if index==len(bookings):
            score=(changed,unused,tuple(ranks))
            if best is None or score<best:
                best=score;best_records=list(assigned)
            return
        for delta,waste,rank,candidate in choices[index]:
            if any(overlap(candidate,previous) for previous in assigned):
                continue
            search(index+1,changed+delta,unused+waste,ranks+[rank],assigned+[candidate])
    search(0,0,0,[],[])
    if best is None:
        fail('no_feasible_plan',409)
    assignments=[dict(reference=b['reference'],table_ids=members(candidate),changed=set(members(candidate))!=set(members(b))) for b,candidate in zip(bookings,best_records)]
    return copy.deepcopy(bookings),assignments,best[0],best[1]

def plan_context_matches(state,plan):
    rid=plan['restaurant_id']
    return ('checkpoint' not in plan['context'] or json_equal(plan['context']['checkpoint'],revision_checkpoint(state,rid))) and json_equal(plan['context']['reservations'],{ref:r for ref,r in state['reservations'].items() if r['restaurant_id']==rid}) and json_equal(plan['context']['closures'],state['closures'][rid])

def plan_public(plan):
    return {key:copy.deepcopy(plan[key]) for key in ('plan_id','restaurant_revision','closure','assignments','moved_count','unused_seats')}

def append_change(state,new,old,plan_id=None):
    entry=event_entry(new,old)
    if plan_id is not None:
        entry.update(event='reassigned',plan_id=plan_id,changes=[dict(field='table_ids',**{'from':members(old),'to':members(new)})])
    entry['seq']=len(state['histories'][new['reference']])+1
    if entry['at']<state['histories'][new['reference']][-1]['at']:
        entry['at']=state['histories'][new['reference']][-1]['at']
    state['histories'][new['reference']].append(entry)
    state['reservations'][new['reference']]=new

def stage3_route(method,path,query,body,headers):
    state=STATE
    match=re.fullmatch(r'/restaurants/([^/]+)/replans(?:/([^/]+)/apply)?',path)
    if method=='POST' and match:
        uid=authenticate(state,headers)
        identity,replay=retry(state,uid,method,path,body,headers)
        if replay is not None:
            return 200,replay
        r=restaurant(state,unquote(match[1]))
        if uid not in r['manager_user_ids']:
            fail('forbidden',403)
        rid=r['id']
        if match[2] is None:
            tid=identifier(field(body,'table_id'))
            if tid not in {t['id'] for t in r['tables']}:
                fail('not_found',404)
            interval(body)
            closure={k:body[k] for k in ('table_id','from','to')}
            bookings,assignments,moved,unused=optimize(state,r,closure)
            pid='plan_'+secrets.token_hex(16)
            plan=dict(plan_id=pid,restaurant_id=rid,user_id=uid,restaurant_revision=state['restaurant_revisions'][rid],closure=closure,
                      assignments=assignments,moved_count=moved,unused_seats=unused,bookings=bookings,applied=False,
                      context=dict(checkpoint=revision_checkpoint(state,rid),reservations=copy.deepcopy({ref:b for ref,b in state['reservations'].items() if b['restaurant_id']==rid}),closures=copy.deepcopy(state['closures'][rid])))
            state['plans'][pid]=plan
            response=plan_public(plan)
        else:
            pid=unquote(match[2]);plan=state['plans'].get(pid)
            if plan is None or plan['restaurant_id']!=rid:
                fail('not_found',404)
            if plan['applied']:
                fail('plan_already_applied',409)
            if state['restaurant_revisions'][rid]!=plan['restaurant_revision'] or not plan_context_matches(state,plan):
                fail('stale_plan',409)
            # Validate the complete proposed occupancy before changing any record.
            view=copy.deepcopy(state)
            for assignment in plan['assignments']:
                view['reservations'][assignment['reference']]=set_tables(view['reservations'][assignment['reference']],assignment['table_ids'])
            view['closures'][rid].append(dict(plan['closure'],plan_id=pid))
            occupancy(view,list(view['reservations'].values()),view['reservations'].keys())
            affected=set()
            for assignment in plan['assignments']:
                old=state['reservations'][assignment['reference']]
                if assignment['changed']:
                    new=set_tables(old,assignment['table_ids']);new['revision']=old['revision']+1
                    append_change(state,new,old,pid)
                    for sid,s in state['series'].items():
                        if old['reference'] in s['references']:
                            affected.add(sid)
            for sid in affected:
                state['series'][sid]['revision']+=1
            state['closures'][rid].append(dict(plan['closure'],plan_id=pid))
            state['restaurant_revisions'][rid]+=1
            plan['applied']=True
            response=dict(plan_id=pid,restaurant_revision=state['restaurant_revisions'][rid],
                          reservations=[public_reservation(state['reservations'][a['reference']]) for a in plan['assignments']])
        save_receipt(state,identity,body,response)
        return 201,response
    match=re.fullmatch(r'/series/([^/]+)/amend',path)
    if method=='POST' and match:
        uid=authenticate(state,headers)
        identity,replay=retry(state,uid,method,path,body,headers)
        if replay is not None:
            return 200,replay
        sid=unquote(match[1]);s=state['series'].get(sid)
        if s is None or s['user_id']!=uid:
            fail('not_found',404)
        expected=bounded(body.get('expected_revision'),1,float('inf'))
        index=bounded(body.get('from_index'),0,len(s['references'])-1)
        text=body.get('local_time')
        try:
            clock(text)
        except APIError:
            fail()
        if expected!=s['revision']:
            fail('stale_revision',409)
        candidates=[];old_records=[]
        for i in range(index,len(s['references'])):
            ref=s['references'][i];old=state['reservations'][ref]
            if old['status']=='cancelled' or ref in s['exceptions']:
                continue
            target=s['scheduled_dates'][i]+'T'+text
            candidate=copy.deepcopy(old) if target==old['starts_at_local'] else amend(state,old,dict(starts_at_local=target))
            candidates.append(candidate);old_records.append(old)
        occupancy(state,candidates,[r['reference'] for r in old_records])
        changed=False
        for new,old in zip(candidates,old_records):
            if new!=old:
                append_change(state,new,old);changed=True
        if changed:
            s['revision']+=1
            state['restaurant_revisions'][state['reservations'][s['references'][0]]['restaurant_id']]+=1
        response=series_public(state,s)
        save_receipt(state,identity,body,response)
        return 201,response
    result=stage3_dispatch(method,path,query,body,headers)
    if method=='POST' and path in ('/_test/reset','/_test/import','/series'):
        initialize_final(STATE)
    return result


def valid_snapshot(raw):
    try:
        if not isinstance(raw,dict):
            fail()
        if 'plans' not in raw and 'closures' not in raw:
            migrated=initialize_revision_log(initialize_final(stage3_snapshot(raw)))
            validate_revision_log(migrated)
            return migrated
        required={'users','restaurants','reservations','tokens','receipts','policies','histories','series','restaurant_revisions','plans','closures'}
        if set(raw) not in (required,required|{'revision_log'}) or not isinstance(raw['plans'],dict) or not isinstance(raw['closures'],dict) or not isinstance(raw['receipts'],list):
            fail()
        core=copy.deepcopy({k:raw[k] for k in required-{'plans','closures'}})
        for entries in core['histories'].values():
            for entry in entries:
                if entry.get('event')=='reassigned':
                    if set(entry)!={'seq','at','event','changes','revision','accepted_terms','plan_id'} or not isinstance(entry['plan_id'],str):
                        fail()
                    if len(entry['changes'])!=1 or entry['changes'][0].get('field')!='table_ids':
                        fail()
                    entry.pop('plan_id');entry['event']='changed'
        for s in core['series'].values():
            s.pop('scheduled_dates',None)
        new_receipts=[];old_receipts=[]
        for receipt in core['receipts']:
            if not isinstance(receipt,dict) or not isinstance(receipt.get('path'),str):
                fail()
            if re.fullmatch(r'/restaurants/[^/]+/replans(?:/[^/]+/apply)?',receipt['path']) or re.fullmatch(r'/series/[^/]+/amend',receipt['path']):
                new_receipts.append(receipt)
            else:
                old_receipts.append(receipt)
        core['receipts']=old_receipts
        stage3_snapshot(core)
        result=copy.deepcopy(raw)
        if set(result['closures'])!=set(result['restaurants']):
            fail()
        for sid,s in result['series'].items():
            if set(s)!={'series_id','user_id','revision','interval_weeks','references','exceptions','scheduled_dates'}:
                fail()
            dates=s['scheduled_dates']
            receipt=next((r for r in old_receipts if r['path']=='/series' and r['response'].get('series_id')==sid),None)
            if not isinstance(dates,list) or len(dates)!=len(s['references']) or receipt is None:
                fail()
            expected=[o['reservation']['starts_at_local'][:10] for o in receipt['response']['occurrences']]
            if dates!=expected:
                fail()
            for date in dates:
                parse_date(date)
        for pid,plan in result['plans'].items():
            keys={'plan_id','restaurant_id','user_id','restaurant_revision','closure','assignments','moved_count','unused_seats','bookings','applied','context'}
            if not isinstance(plan,dict) or set(plan)!=keys or plan['plan_id']!=pid or plan['restaurant_id'] not in result['restaurants'] or plan['user_id'] not in result['users'] or type(plan['applied']) is not bool:
                fail()
            identifier(pid)
            rid=plan['restaurant_id'];r=result['restaurants'][rid]
            bounded(plan['restaurant_revision'],0,result['restaurant_revisions'][rid])
            closure=plan['closure']
            if not isinstance(closure,dict) or set(closure)!={'table_id','from','to'} or closure['table_id'] not in {t['id'] for t in r['tables']}:
                fail()
            interval(closure)
            context=plan['context']
            if not isinstance(context,dict) or set(context) not in ({'reservations','closures'},{'reservations','closures','checkpoint'}) or not isinstance(context['reservations'],dict) or not isinstance(context['closures'],list):
                fail()
            for ref,b in context['reservations'].items():
                if not isinstance(b,dict) or b.get('reference')!=ref or b.get('restaurant_id')!=rid or ref not in result['reservations']:
                    fail()
                public=public_reservation(b)
                if b['status']=='confirmed':
                    validate_saved_reservation(result,public,b['user_id'])
                else:
                    # Cancelled fixed records carry no occupancy but keep validated identity.
                    if b['status']!='cancelled' or b['revision']!=result['reservations'][ref]['revision'] or not json_equal(b,result['reservations'][ref]):
                        fail()
            for c in context['closures']:
                if not isinstance(c,dict) or set(c)!={'table_id','from','to','plan_id'} or c['plan_id'] not in result['plans']:
                    fail()
                previous=result['plans'][c['plan_id']]
                if not previous['applied'] or previous['restaurant_id']!=rid or not json_equal({k:v for k,v in c.items() if k!='plan_id'},previous['closure']):
                    fail()
                interval(c)
            view=dict(restaurants={rid:r},reservations=context['reservations'],closures={rid:context['closures']})
            bookings,assignments,moved,unused=optimize(view,r,closure)
            if not json_equal(plan['bookings'],bookings) or not json_equal(plan['assignments'],assignments) or type(plan['moved_count']) is not int or plan['moved_count']!=moved or type(plan['unused_seats']) is not int or plan['unused_seats']!=unused:
                fail()
        seen=set()
        for rid,closures in result['closures'].items():
            if not isinstance(closures,list):
                fail()
            for closure in closures:
                if not isinstance(closure,dict) or set(closure)!={'plan_id','table_id','from','to'} or closure['plan_id'] in seen:
                    fail()
                pid=closure['plan_id'];plan=result['plans'].get(pid)
                if plan is None or not plan['applied'] or plan['restaurant_id']!=rid or not json_equal(plan['closure'],{k:v for k,v in closure.items() if k!='plan_id'}):
                    fail()
                interval(closure);seen.add(pid)
        if seen!={pid for pid,p in result['plans'].items() if p['applied']}:
            fail()
        occupancy(result,list(result['reservations'].values()),result['reservations'].keys())
        for ref,entries in result['histories'].items():
            for entry in entries:
                if entry['event']=='reassigned':
                    plan=result['plans'].get(entry['plan_id'])
                    if plan is None or not plan['applied'] or ref not in {a['reference'] for a in plan['assignments'] if a['changed']}:
                        fail()
                    assignment=next(a for a in plan['assignments'] if a['reference']==ref)
                    old=next(b for b in plan['bookings'] if b['reference']==ref)
                    if entry['revision']!=old['revision']+1 or not json_equal(entry['accepted_terms'],old['accepted_terms']) or entry['changes']!=[dict(field='table_ids',**{'from':members(old),'to':assignment['table_ids']})]:
                        fail()
        all_keys=set()
        for receipt in result['receipts']:
            identity=tuple(receipt.get(k) for k in ('user_id','method','path','key'))
            if identity in all_keys:
                fail()
            all_keys.add(identity)
        for receipt in new_receipts:
            if set(receipt)!={'user_id','method','path','key','body','response'} or receipt['user_id'] not in result['users'] or receipt['method']!='POST' or not isinstance(receipt['key'],str) or not 1<=len(receipt['key'])<=255 or not isinstance(receipt['body'],dict) or not isinstance(receipt['response'],dict):
                fail()
            path=receipt['path'];response=receipt['response'];body=receipt['body']
            match=re.fullmatch(r'/restaurants/([^/]+)/replans(?:/([^/]+)/apply)?',path)
            if match:
                rid=unquote(match[1]);pid=response.get('plan_id');plan=result['plans'].get(pid)
                if plan is None or plan['restaurant_id']!=rid or receipt['user_id'] not in result['restaurants'][rid].get('manager_user_ids',[]):
                    fail()
                if match[2] is None:
                    if not json_equal(response,plan_public(plan)) or not json_equal({k:body.get(k) for k in ('table_id','from','to')},plan['closure']):
                        fail()
                else:
                    if unquote(match[2])!=pid or not plan['applied'] or set(response)!={'plan_id','restaurant_revision','reservations'} or bounded(response['restaurant_revision'],1,result['restaurant_revisions'][rid])!=plan['restaurant_revision']+1:
                        fail()
                    records=response['reservations']
                    if not isinstance(records,list) or [b.get('reference') for b in records]!=[a['reference'] for a in plan['assignments']]:
                        fail()
                    for b,assignment,old in zip(records,plan['assignments'],plan['bookings']):
                        validate_saved_reservation(result,b,old['user_id'])
                        expected=set_tables(old,assignment['table_ids']);expected['revision']+=int(assignment['changed'])
                        if not json_equal(b,public_reservation(expected)):
                            fail()
            else:
                sid=unquote(path.split('/')[2]);s=result['series'].get(sid)
                if s is None or s['user_id']!=receipt['user_id'] or set(response)!={'series_id','revision','interval_weeks','occurrences'} or response['series_id']!=sid:
                    fail()
                bounded(body.get('expected_revision'),1,s['revision']);bounded(body.get('from_index'),0,len(s['references'])-1)
                clock(body.get('local_time'))
                revision=bounded(response['revision'],1,s['revision'])
                if revision not in (body['expected_revision'],body['expected_revision']+1) or bounded(response['interval_weeks'],1,4)!=s['interval_weeks']:
                    fail()
                occurrences=response['occurrences']
                if not isinstance(occurrences,list) or len(occurrences)!=len(s['references']):
                    fail()
                for i,o in enumerate(occurrences):
                    if set(o)!={'index','reference','exception','reservation'} or type(o['index']) is not int or o['index']!=i or o['reference']!=s['references'][i] or type(o['exception']) is not bool:
                        fail()
                    record=o['reservation']
                    if not isinstance(record,dict) or record.get('reference')!=o['reference'] or (o['exception'] and o['reference'] not in s['exceptions']):
                        fail()
                    if i>=body['from_index'] and not o['exception'] and record.get('status')=='confirmed' and record.get('starts_at_local')!=s['scheduled_dates'][i]+'T'+body['local_time']:
                        fail()
                    if record['status']=='confirmed':
                        validate_saved_reservation(result,record,s['user_id'])
                    else:
                        current=result['reservations'][o['reference']]
                        if record['status']!='cancelled' or not json_equal(record,public_reservation(current)):
                            fail()
        initialize_revision_log(result)
        validate_revision_log(result)
        for plan in result['plans'].values():
            if not plan['applied'] and plan['restaurant_revision']==result['restaurant_revisions'][plan['restaurant_id']] and not plan_context_matches(result,plan):
                fail()
        return result
    except (APIError,KeyError,TypeError,ValueError,OverflowError,AttributeError,IndexError):
        fail()



def revision_checkpoint(state,rid):
    return dict(reservations={ref:r['revision'] for ref,r in state['reservations'].items() if r['restaurant_id']==rid},
                policies=len(state['policies'][rid]),
                series={sid:s['revision'] for sid,s in state['series'].items() if state['reservations'][s['references'][0]]['restaurant_id']==rid},
                plans=sorted(pid for pid,p in state.get('plans',{}).items() if p['restaurant_id']==rid and p['applied']))

def validate_baseline_counter(state,rid,checkpoint,counter):
    # Older formats do not distinguish individual writes from collective writes.
    # Count mandatory operations and the possible groupings of recorded changes.
    mandatory=checkpoint['policies']+len(checkpoint['series'])+len(checkpoint['plans'])
    for receipt in state['receipts']:
        if receipt['path']=='/reservations' and 'revision' in receipt['response'] and receipt['response']['reference'] in checkpoint['reservations']:
            mandatory+=1
    changes=[]
    for ref,version in checkpoint['reservations'].items():
        entries=state['histories'][ref][:version]
        mandatory+=sum(e['event']=='cancelled' for e in entries)
        changes.append(sum(e['event']=='changed' for e in entries))
    if not mandatory+max(changes,default=0)<=counter<=mandatory+sum(changes):
        fail()
    for sid,revision in checkpoint['series'].items():
        original=next(r['response'] for r in state['receipts'] if r['path']=='/series' and r['response']['series_id']==sid)
        cancel_count=0;repair_ids=set();member_changes=[]
        for occurrence in original['occurrences']:
            ref=occurrence['reference'];initial=occurrence['reservation']['revision']
            entries=state['histories'][ref][initial:checkpoint['reservations'][ref]]
            cancel_count+=sum(e['event']=='cancelled' for e in entries)
            repair_ids.update(e['plan_id'] for e in entries if e['event']=='reassigned')
            member_changes.append(sum(e['event']=='changed' for e in entries))
        minimum=1+cancel_count+len(repair_ids)+max(member_changes,default=0)
        maximum=1+cancel_count+len(repair_ids)+sum(member_changes)
        if not minimum<=revision<=maximum:
            fail()

def initialize_revision_log(state):
    if 'revision_log' not in state:
        state['revision_log']={rid:dict(base=state['restaurant_revisions'][rid],checkpoint=revision_checkpoint(state,rid),events=[]) for rid in state['restaurants']}
    return state

transaction_route=route

def route(method,path,query,body,headers):
    global STATE
    before=STATE
    try:
        response=transaction_route(method,path,query,body,headers)
        if method in ('POST','PATCH','PUT','DELETE') and path not in ('/_test/reset','/_test/import'):
            for rid in STATE['restaurants']:
                previous=before['restaurant_revisions'][rid];current=STATE['restaurant_revisions'][rid]
                if current!=previous:
                    if current!=previous+1:
                        fail()
                    old=revision_checkpoint(before,rid);new=revision_checkpoint(STATE,rid)
                    event=dict(method=method,path=path,
                               reservations={ref:rev for ref,rev in new['reservations'].items() if old['reservations'].get(ref)!=rev},
                               policies=new['policies'],
                               series={sid:rev for sid,rev in new['series'].items() if old['series'].get(sid)!=rev},
                               plans=[pid for pid in new['plans'] if pid not in old['plans']])
                    STATE['revision_log'][rid]['events'].append(event)
        return response
    except Exception:
        STATE=before
        raise

def validate_revision_log(state):
    logs=state['revision_log']
    if not isinstance(logs,dict) or set(logs)!=set(state['restaurants']):
        fail()
    for rid,log in logs.items():
        if not isinstance(log,dict) or set(log)!={'base','checkpoint','events'} or not isinstance(log['events'],list):
            fail()
        base=bounded(log['base'],0,float('inf'));checkpoint=copy.deepcopy(log['checkpoint'])
        def check_point(c):
            if not isinstance(c,dict) or set(c)!={'reservations','policies','series','plans'} or not isinstance(c['reservations'],dict) or not isinstance(c['series'],dict) or not isinstance(c['plans'],list) or len(c['plans'])!=len(set(c['plans'])):
                fail()
            bounded(c['policies'],0,len(state['policies'][rid]))
            for ref,rev in c['reservations'].items():
                if ref not in state['reservations'] or state['reservations'][ref]['restaurant_id']!=rid:
                    fail()
                bounded(rev,1,state['reservations'][ref]['revision'])
            for sid,rev in c['series'].items():
                if sid not in state['series'] or state['reservations'][state['series'][sid]['references'][0]]['restaurant_id']!=rid:
                    fail()
                bounded(rev,1,state['series'][sid]['revision'])
            for pid in c['plans']:
                if pid not in state['plans'] or state['plans'][pid]['restaurant_id']!=rid or not state['plans'][pid]['applied']:
                    fail()
        check_point(checkpoint);validate_baseline_counter(state,rid,checkpoint,base)
        checkpoints={base:copy.deepcopy(checkpoint)}
        for event in log['events']:
            if not isinstance(event,dict) or set(event)!={'method','path','reservations','policies','series','plans'} or not isinstance(event['path'],str) or event['method'] not in ('POST','PATCH') or not isinstance(event['reservations'],dict) or not isinstance(event['series'],dict) or not isinstance(event['plans'],list):
                fail()
            old=copy.deepcopy(checkpoint)
            for ref,rev in event['reservations'].items():
                if type(rev) is not int or rev!=checkpoint['reservations'].get(ref,0)+1:
                    fail()
                checkpoint['reservations'][ref]=rev
            for sid,rev in event['series'].items():
                if type(rev) is not int or rev!=checkpoint['series'].get(sid,0)+1:
                    fail()
                checkpoint['series'][sid]=rev
            if type(event['policies']) is not int or event['policies'] not in (checkpoint['policies'],checkpoint['policies']+1) or any(pid in checkpoint['plans'] for pid in event['plans']):
                fail()
            checkpoint['policies']=event['policies'];checkpoint['plans']=sorted(checkpoint['plans']+event['plans'])
            check_point(checkpoint)
            path=event['path'];refs=event['reservations']
            events=[state['histories'][ref][rev-1]['event'] for ref,rev in refs.items()]
            if path=='/reservations':
                good=event['method']=='POST' and len(refs)==1 and events==['created']
            elif path=='/series':
                good=event['method']=='POST' and len(event['series'])==1 and all(e=='created' for e in events)
            elif re.fullmatch(r'/restaurants/[^/]+/policies',path):
                good=event['method']=='POST' and not refs and checkpoint['policies']==old['policies']+1
            elif re.fullmatch(r'/restaurants/[^/]+/replans/[^/]+/apply',path):
                good=event['method']=='POST' and len(event['plans'])==1 and all(e=='reassigned' for e in events)
                plan=state['plans'][unquote(path.split('/')[4])]
                good=good and event['plans']==[plan['plan_id']] and set(refs)=={a['reference'] for a in plan['assignments'] if a['changed']}
            elif path=='/reservation-moves' or re.fullmatch(r'/series/[^/]+/amend',path):
                good=event['method']=='POST' and bool(refs) and all(e=='changed' for e in events)
            elif re.fullmatch(r'/reservations/[^/]+/cancel',path):
                good=event['method']=='POST' and list(refs)==[unquote(path.split('/')[2])] and events==['cancelled']
            elif re.fullmatch(r'/reservations/[^/]+',path):
                good=event['method']=='PATCH' and list(refs)==[unquote(path.split('/')[2])] and events==['changed']
            else:good=False
            policy_change=checkpoint['policies']!=old['policies']
            if not re.fullmatch(r'/restaurants/[^/]+/policies',path) and policy_change:
                good=False
            if not path.endswith('/apply') and event['plans']:
                good=False
            if path=='/series':
                sid=next(iter(event['series']),None)
                good=good and sid is not None and sid not in old['series'] and set(refs)==set(state['series'][sid]['references'][1:])
            else:
                affected={sid for sid,s in state['series'].items() if sid in old['series'] and set(refs)&set(s['references'])}
                good=good and set(event['series'])==affected
            checkpoints[base+len(checkpoints)]=copy.deepcopy(checkpoint)
            if not good or json_equal(old,checkpoint):
                fail()
        for plan in state['plans'].values():
            if plan['restaurant_id']==rid and 'checkpoint' in plan['context']:
                expected=checkpoints.get(plan['restaurant_revision'])
                if expected is None or not json_equal(plan['context']['checkpoint'],expected) or plan['context']['checkpoint']['reservations']!={ref:r['revision'] for ref,r in plan['context']['reservations'].items()}:
                    fail()
        if not json_equal(checkpoint,revision_checkpoint(state,rid)) or state['restaurant_revisions'][rid]!=base+len(log['events']):
            fail()

STATE=initialize_revision_log(initialize_final(STATE))

if __name__ == "__main__":
    Server(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
