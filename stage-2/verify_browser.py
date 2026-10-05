"""Browser recovery, actual Stage 1 upgrade and product checks.
Run in df-harness-runner with stage-1/ and stage-2/ mounted as siblings.
Evidence directory is controlled by BROWSER_EVIDENCE; no credentials/snapshots logged.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright, expect
from verify_contract import request, port, DAY
from verify_stage2 import paired_fixture

EVIDENCE = Path(os.environ.get("BROWSER_EVIDENCE", "/evidence"))
EVIDENCE.mkdir(parents=True, exist_ok=True)
checks = []


def check(condition, name):
    if not condition:
        raise AssertionError(name)
    checks.append(name)
    print("PASS:", name, flush=True)


def product_fixture():
    seeded = paired_fixture()
    seeded["restaurants"][0]["name"] = "Juniper House"
    second = copy.deepcopy(seeded["restaurants"][0])
    second["id"], second["name"] = "r2", "Saffron Court"
    for t in second["tables"]:
        t["label"] = {"t1":"Patio", "t2":"Loft", "t3":"Lounge", "t4":"Courtyard"}[t["id"]]
    seeded["restaurants"].append(second)
    return seeded


processes, bases = [], []
for index, app in enumerate([Path(__file__).with_name("app.py"), Path(__file__).parent.parent/"stage-1"/"app.py"]):
    if index == 0 and os.environ.get("BROWSER_STAGE2_URL"):
        bases.append(os.environ["BROWSER_STAGE2_URL"])
        continue
    number = port()
    process = subprocess.Popen([sys.executable,str(app)],env=dict(os.environ,PORT=str(number),PYTHONDONTWRITEBYTECODE="1"),
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    processes.append(process)
    base = f"http://127.0.0.1:{number}"
    bases.append(base)
    for _ in range(100):
        try:
            if request(base,"GET","/health")[0] == 200:
                break
        except OSError:
            time.sleep(.05)

base, legacy = bases
try:
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=["--no-sandbox"])
        context=browser.new_context(viewport={"width":1280,"height":900})
        page=context.new_page()
        page_errors=[]
        page.on("pageerror",lambda error: page_errors.append(str(error)))
        mode={"legacy":False,"drop":False,"slow":False}
        held=[]
        submissions=[]
        originals=[]

        def interception(route):
            parsed=urlsplit(route.request.url)
            path=parsed.path
            is_api = path.startswith(("/auth/","/restaurants","/availability","/reservations"))
            if not is_api:
                route.continue_();return
            source=legacy if mode["legacy"] else base
            response=route.fetch(url=source+path+("?" + parsed.query if parsed.query else ""))
            if mode["slow"] and ((path=="/restaurants/r1") or
                                (path=="/availability" and parse_qs(parsed.query).get("restaurant_id")==["r1"])):
                held.append((route,response));return
            if path=="/reservations" and route.request.method=="POST":
                submissions.append((route.request.headers.get("idempotency-key"),route.request.post_data))
                if mode["drop"]:
                    check(response.status==201,"Fault injection reached a committed booking")
                    originals.append(response.json())
                    mode["drop"]=False
                    route.abort("failed");return
            route.fulfill(response=response)

        context.route("**/*",interception)

        def reset():
            request(base,"POST","/_test/reset",product_fixture())
            mode.update(legacy=False,drop=False,slow=False)
            page.goto(base+"/login")
            page.get_by_test_id("login-email").fill("one@example.com")
            page.get_by_test_id("login-password").fill("password1")
            page.get_by_test_id("login-submit").click()
            page.wait_for_url(base+"/")
            expect(page.get_by_test_id("current-user")).to_contain_text("One")
            expect(page.get_by_test_id("restaurant-select").locator("option[value='r1']")).to_have_count(1)

        def search(rid="r1",size="2"):
            page.get_by_test_id("restaurant-select").select_option(rid)
            page.get_by_test_id("date-input").fill(DAY)
            page.get_by_test_id("party-size-input").fill(size)
            page.get_by_test_id("search-button").click()
            expect(page.get_by_test_id("availability-grid")).to_be_visible()

        # Out-of-order search A includes both configuration labels and availability.
        reset()
        mode["slow"]=True
        page.get_by_test_id("restaurant-select").select_option("r1")
        page.get_by_test_id("date-input").fill(DAY)
        page.get_by_test_id("search-button").click()
        page.wait_for_function("document.querySelector('.loading') !== null")
        search("r2","2")
        expect(page.get_by_test_id("slot-t1-12:00")).to_contain_text("Patio")
        mode["slow"]=False
        for route,response in held:
            route.fulfill(response=response)
        held.clear()
        page.wait_for_timeout(100)
        page.get_by_test_id("slot-t1-12:00").click()
        expect(page.get_by_test_id("booking-summary")).to_contain_text("Saffron Court")
        expect(page.get_by_test_id("booking-summary")).to_contain_text("Patio")
        check("Juniper" not in page.get_by_test_id("booking-summary").inner_text(),
              "Late search A cannot replace B grid, labels or booking form")
        check(page.get_by_test_id("slot-t1-12:00").evaluate("el=>getComputedStyle(el).backgroundColor==='rgb(36, 86, 70)'"),"Selected hovered table retains dark high-contrast background")
        page.evaluate("scrollTo(0,0)")
        page.screenshot(path=str(EVIDENCE/"desktop-search.png"),full_page=False)

        # Pair lost response, state replacement without reload, same body/key retry.
        reset();search(size="5")
        page.get_by_test_id("slot-t2+t1-12:00").click()
        expect(page.get_by_test_id("booking-summary")).to_contain_text("Garden")
        expect(page.get_by_test_id("booking-summary")).to_contain_text("Window")
        mode["drop"]=True
        page.get_by_test_id("booking-submit").click()
        expect(page.get_by_test_id("booking-uncertain")).to_be_visible()
        check(page.get_by_test_id("booking-error").count()==0 and page.get_by_test_id("confirmation").count()==0,
              "Lost pair response shows uncertainty without error or confirmation")
        page.evaluate("window.savedBookingForm=document.querySelector('[data-testid=booking-form]')")
        page.get_by_test_id("slot-t2+t1-12:00").click()
        expect(page.get_by_test_id("booking-uncertain")).to_be_visible()
        check(page.evaluate("window.savedBookingForm===document.querySelector('[data-testid=booking-form]')"),"Unchanged pair reselection retains exact form DOM and uncertainty")
        unchanged=page.get_by_test_id("booking-party-size").input_value()
        snapshot=request(base,"GET","/_test/export")[1]
        request(base,"POST","/_test/reset",product_fixture())
        check(request(base,"POST","/_test/import",snapshot)[0]==204,"Pair pending retry survives state replacement")
        page.set_viewport_size({"width":375,"height":812})
        page.get_by_test_id("booking-form").scroll_into_view_if_needed()
        page.screenshot(path=str(EVIDENCE/"mobile-uncertain.png"),full_page=False)
        check(page.evaluate("document.documentElement.scrollWidth <= innerWidth"),"375px uncertain flow has no horizontal page scrolling")
        page.get_by_test_id("booking-submit").click()
        expect(page.get_by_test_id("confirmation-reference")).to_have_text(originals[-1]["reference"])
        check(submissions[-1]==submissions[-2],"Pair retry uses exactly the same idempotency key and body")
        check(page.get_by_test_id("booking-party-size").input_value()==unchanged,"Pair retry preserves unchanged form input")
        check(page.get_by_test_id("booking-uncertain").count()==0 and page.get_by_test_id("booking-error").count()==0,
              "Successful pair retry clears both uncertainty and error")
        expect(page.get_by_test_id("confirmation-tables")).to_contain_text("Garden")
        expect(page.get_by_test_id("confirmation-tables")).to_contain_text("Window")
        page.get_by_test_id("slot-t2+t1-12:00").click()
        expect(page.get_by_test_id("confirmation-reference")).to_have_text(originals[-1]["reference"])
        check(page.get_by_test_id("confirmation").is_visible(),"Unchanged pair reselection retains successful confirmation")
        page.get_by_test_id("booking-submit").click()
        expect(page.get_by_test_id("confirmation-reference")).to_have_text(originals[-1]["reference"])
        check(submissions[-1]==submissions[-2],"Unchanged success resubmission preserves retry identity")
        before=len(submissions)
        page.get_by_test_id("booking-party-size").fill("6")
        page.get_by_test_id("booking-party-size").fill("5")
        page.get_by_test_id("booking-submit").click()
        expect(page.get_by_test_id("booking-error")).to_be_visible()
        check(len(submissions)==before+1 and submissions[-1][0]!=submissions[-2][0],
              "Changing a field and returning to its old value creates a new request identity")
        check(page.get_by_test_id("booking-form").is_visible() and page.get_by_test_id("booking-party-size").input_value()=="5",
              "Pair conflict keeps selected form and edited inputs")
        check(page.get_by_test_id("confirmation").count()==0,"Confirmed rejection does not show a new confirmation")

        # Single conflict caused by a separate client, availability refresh, preserved form.
        reset();search()
        page.get_by_test_id("slot-t1-12:00").click()
        other=request(base,"POST","/auth/login",{"email":"two@example.com","password":"password2"})[1]["token"]
        conflict={"restaurant_id":"r1","table_id":"t1","starts_at_local":DAY+"T12:00","party_size":2}
        check(request(base,"POST","/reservations",conflict,other,"other-client")[0]==201,"Competing client took selected single table")
        page.get_by_test_id("booking-submit").click()
        expect(page.get_by_test_id("booking-error")).to_be_visible()
        expect(page.get_by_test_id("slot-t1-12:00")).to_have_attribute("data-available","false")
        check(page.get_by_test_id("booking-form").is_visible() and page.get_by_test_id("booking-party-size").input_value()=="2",
              "Single conflict refreshes availability and preserves form")
        expect(page.get_by_test_id("booking-summary")).to_contain_text("Window")

        # Actual accepted Stage 1 backend -> Stage 2 backend, pending single retry unchanged.
        request(legacy,"POST","/_test/reset",product_fixture())
        request(base,"POST","/_test/reset",product_fixture())
        mode["legacy"]=True
        page.goto(base+"/login")
        page.get_by_test_id("login-email").fill("one@example.com")
        page.get_by_test_id("login-password").fill("password1")
        page.get_by_test_id("login-submit").click();page.wait_for_url(base+"/")
        search()
        page.get_by_test_id("slot-t1-12:00").click()
        mode["drop"]=True
        page.get_by_test_id("booking-submit").click()
        expect(page.get_by_test_id("booking-uncertain")).to_be_visible()
        page.evaluate("window.savedBookingForm=document.querySelector('[data-testid=booking-form]')")
        page.get_by_test_id("slot-t1-12:00").click()
        expect(page.get_by_test_id("booking-uncertain")).to_be_visible()
        check(page.evaluate("window.savedBookingForm===document.querySelector('[data-testid=booking-form]')"),"Unchanged single reselection retains exact form DOM and uncertainty")
        legacy_reference=originals[-1]["reference"]
        current_token=page.evaluate("JSON.parse(localStorage.getItem('gather-user')).token")
        exported=request(legacy,"GET","/_test/export")[1]
        check(request(base,"POST","/_test/import",exported)[0]==204,"Stage 2 accepts actual accepted Stage 1 export")
        mode["legacy"]=False
        check(page.evaluate("JSON.parse(localStorage.getItem('gather-user')).token")==current_token,
              "Browser token survives upgrade with no reload or login")
        expect(page.get_by_test_id("current-user")).to_contain_text("One")
        expect(page.get_by_test_id("booking-form")).to_be_visible()
        page.get_by_test_id("booking-submit").click()
        expect(page.get_by_test_id("confirmation-reference")).to_have_text(legacy_reference)
        check(submissions[-1]==submissions[-2],"Stage 1 lost single receipt recovers with exact pending body/key after upgrade")
        check(page.get_by_test_id("booking-uncertain").count()==0,"Upgrade retry clears uncertainty")
        page.goto(base+"/lookup")
        page.get_by_test_id("lookup-reference-input").fill(legacy_reference)
        page.get_by_test_id("lookup-submit").click()
        expect(page.get_by_test_id("reservation-status")).to_have_text("confirmed")
        expect(page.get_by_test_id("reservation-tables")).to_contain_text("Window")
        check(page.get_by_test_id("current-user").inner_text()=="One","Retained reference lookup works with original upgraded session")
        page.get_by_test_id("reservation-cancel-button").click()
        expect(page.get_by_test_id("reservation-status")).to_have_text("cancelled")
        check(page.get_by_test_id("reservation-cancel-button").count()==0,"Cancellation removes cancellation action")
        page.screenshot(path=str(EVIDENCE/"mobile-lookup.png"),full_page=False)

        # Product checks: usable routes, focus, visible labels, no overflow.
        reset();search()
        page.get_by_test_id("slot-t2+t1-12:00").click()
        page.get_by_test_id("booking-form").scroll_into_view_if_needed()
        page.screenshot(path=str(EVIDENCE/"mobile-booking.png"),full_page=False)
        check(page.evaluate("document.documentElement.scrollWidth <= innerWidth"),"375px booking/search flow has no horizontal page scrolling")
        check(page.evaluate("Array.from(document.querySelectorAll('input,select')).every(el=>document.querySelector('label[for=\"'+el.id+'\"]')?.textContent.trim())"),
              "Every visible search/booking input has an explicit text label")
        page.get_by_test_id("booking-party-size").focus()
        check(page.get_by_test_id("booking-party-size").evaluate("el=>parseFloat(getComputedStyle(el).outlineWidth)>=3"),
              "Keyboard input focus has an apparent outline")
        for route in ["/signup","/login","/lookup"]:
            page.goto(base+route)
            check(page.evaluate("document.documentElement.scrollWidth <= innerWidth"),"375px "+route+" has no horizontal scrolling")
            check(page.get_by_test_id("current-user").is_visible(),"Signed-in name is visible on "+route)
        check(not page_errors,"No uncaught browser JavaScript errors")
        context.close();browser.close()
    print(f"All {len(checks)} browser assertions passed.",flush=True)
finally:
    for process in processes:
        process.terminate();process.wait(timeout=5)
