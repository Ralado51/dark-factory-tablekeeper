"""Stage 1 HTTP service. State mutations and snapshots are linearized by LOCK."""
import copy
from decimal import Decimal
import sys
import hashlib
import hmac
import json
import os
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


def proposed(state, rid, body):
    r = restaurant(state, rid)
    tid = identifier(field(body, "table_id"))
    table = next((t for t in r["tables"] if t["id"] == tid), None)
    if table is None:
        fail("not_found", 404)
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
    if size > table["capacity"]:
        fail("party_exceeds_capacity")
    return {"restaurant_id": rid, "table_id": tid, "party_size": size,
            "starts_at_local": body["starts_at_local"], "starts_at": stamp(start), "ends_at": stamp(end)}


def overlap(a, b):
    return (a["status"] == b["status"] == "confirmed"
            and a["restaurant_id"] == b["restaurant_id"] and a["table_id"] == b["table_id"]
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
    merged = {k: changes.get(k, r[k]) for k in ("table_id", "starts_at_local", "party_size")}
    updated = proposed(state, r["restaurant_id"], merged)
    result = dict(r)
    result.update(updated)
    # In particular no-op moves retain the originally recorded timestamp strings.
    if all(json_equal(merged[k], r[k]) for k in merged):
        return dict(r)
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
             "opening_hours": [], "tables": []}
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
        r.update({"reservation_id": res_id, "reference": ref, "user_id": uid,
                  "status": "confirmed", "created_at": stamp(now())})
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
        if not json_equal(normalized, raw["restaurants"]):
            fail()
        ids = set()
        for ref, r in raw["reservations"].items():
            if set(r) != {"reservation_id", "reference", "user_id", "restaurant_id", "table_id", "party_size",
                          "status", "starts_at_local", "starts_at", "ends_at", "created_at"}:
                fail()
            if r["reference"] != ref or not re.fullmatch(r"[A-Z0-9]{6,12}", ref):
                fail()
            identifier(r["reservation_id"])
            if r["reservation_id"] in ids or r["user_id"] not in raw["users"]:
                fail()
            ids.add(r["reservation_id"])
            if r["status"] not in ("confirmed", "cancelled"):
                fail()
            expected = proposed(raw, r["restaurant_id"], r)
            if not json_equal(expected, {k: r[k] for k in expected}):
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
                if not isinstance(item, dict) or set(item) != set(public_reservation(next(iter(raw["reservations"].values()), {}))):
                    fail()
                if item.get("reference") not in raw["reservations"]:
                    fail()
                current = raw["reservations"][item["reference"]]
                for k in ("reservation_id", "reference", "restaurant_id", "created_at"):
                    if item[k] != current[k]:
                        fail()
                if current["user_id"] != receipt["user_id"] or item["status"] != "confirmed":
                    fail()
                expected = proposed(raw, item["restaurant_id"], item)
                if not json_equal(expected, {k: item[k] for k in expected}):
                    fail()
        return copy.deepcopy(raw)
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
        available = []
        for t in r["tables"]:
            candidate = {"status": "confirmed", "restaurant_id": r["id"], "table_id": t["id"],
                         "starts_at": stamp(start), "ends_at": stamp(end)}
            if t["capacity"] >= size and not any(overlap(candidate, x) for x in state["reservations"].values()):
                available.append(t["id"])
        response["slots"].append({"starts_at_local": local, "starts_at": stamp(start),
                                  "available_table_ids": available})
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


if __name__ == "__main__":
    Server(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
