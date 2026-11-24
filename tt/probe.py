"""One-off probe of the OddsPapi table tennis data. Writes tt/probe.json.
Never prints or stores the API key."""
import json, os, time, urllib.request, urllib.parse, urllib.error, datetime as dt

KEY = os.environ.get("ODDSPAPI_API_KEY", "").strip()
BASE = "https://api.oddspapi.io/v4"
OUT = {"ran_at": dt.datetime.utcnow().isoformat() + "Z", "key_present": bool(KEY), "calls": []}


def get(path, wait=2.2, **params):
    q = dict(params)
    q["apiKey"] = KEY
    url = BASE + path + "?" + urllib.parse.urlencode(q)
    rec = {"path": path, "params": params}
    data = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tt-engine"})
            with urllib.request.urlopen(req, timeout=40) as r:
                rec["status"] = r.status
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            rec["status"] = e.code
            try:
                body = e.read().decode("utf-8")[:600]
            except Exception:
                body = ""
            rec["error_body"] = body.replace(KEY, "***") if KEY else body
            if e.code == 429:
                time.sleep(5)
                continue
            break
        except Exception as e:  # never record the URL
            rec["status"] = "EXC " + type(e).__name__
            break
    OUT["calls"].append(rec)
    time.sleep(wait)
    return data, rec


def trunc(obj, n=3500):
    s = json.dumps(obj)
    return obj if len(s) <= n else s[:n] + "...<truncated>"


def main():
    acct, rec = get("/account")
    if isinstance(acct, dict):
        safe = {}
        for k, v in acct.items():
            lk = k.lower()
            if any(w in lk for w in ("mail", "key", "name", "user", "token", "id")):
                continue
            safe[k] = v
        rec["account"] = trunc(safe)

    tours, rec = get("/tournaments", sportId=25)
    if isinstance(tours, list):
        rec["count"] = len(tours)
        rec["sample_keys"] = sorted(tours[0].keys()) if tours else []
        keep = ("tournamentId", "tournamentName", "categoryName", "futureFixtures", "upcomingFixtures", "liveFixtures")
        rec["tournaments"] = [{k: t.get(k) for k in keep if k in t} for t in tours][:400]

    mk, rec = get("/markets", sportId=25)
    if isinstance(mk, list):
        rec["count"] = len(mk)
        tp = [m for m in mk if "total" in str(m.get("marketName", "")).lower()]
        rec["names"] = sorted({str(m.get("marketName")) + " | " + str(m.get("period")) for m in mk})
        rec["total_first3"] = tp[:3]
        rec["total_lines"] = {str(m.get("marketId")): m.get("handicap") for m in tp}
        rec["winner"] = [m for m in mk if m.get("marketId") == 251][:1]

    bk, rec = get("/bookmakers")
    if isinstance(bk, list):
        rec["count"] = len(bk)
        rec["sample"] = bk[:2]
        rec["slugs"] = [b.get("slug") for b in bk]

    today = dt.datetime.utcnow().date()
    frm = (today - dt.timedelta(days=1)).isoformat()
    to = (today + dt.timedelta(days=2)).isoformat()
    fin = upc = part = None
    for tid in (32129, 36349, 36377):
        fx, rec = get("/fixtures", tournamentId=tid, **{"from": frm, "to": to})
        if isinstance(fx, list):
            rec["count"] = len(fx)
            st = {}
            for f in fx:
                key = "%s:%s:odds=%s" % (f.get("statusId"), f.get("statusName"), f.get("hasOdds"))
                st[key] = st.get(key, 0) + 1
            rec["status_counts"] = st
            rec["sample"] = fx[:1]
            times = sorted(f.get("startTime") for f in fx if f.get("startTime"))
            rec["first_start"], rec["last_start"] = (times[0], times[-1]) if times else (None, None)
            rec["upcoming_with_odds"] = sum(1 for f in fx if f.get("statusId") == 0 and f.get("hasOdds"))
            rec["upcoming_total"] = sum(1 for f in fx if f.get("statusId") == 0)
            if fin is None:
                done = [f for f in fx if f.get("statusId") == 2 and f.get("hasOdds")]
                if done:
                    fin = done[len(done) // 2]
                    part = fin.get("participant1Id")
            if upc is None:
                nxt = sorted([f for f in fx if f.get("statusId") == 0 and f.get("hasOdds")],
                             key=lambda f: f.get("startTime") or "")
                if nxt:
                    upc = nxt[0]

    OUT["picked"] = {"finished": fin and fin.get("fixtureId"), "upcoming": upc and upc.get("fixtureId"),
                     "participant": part}

    if fin:
        sc, rec = get("/scores", wait=1.3, fixtureId=fin["fixtureId"])
        rec["body"] = trunc(sc)
        st, rec = get("/settlements", wait=1.3, fixtureId=fin["fixtureId"])
        rec["body"] = trunc(st, 2500)
        ho, rec = get("/historical-odds", wait=5, fixtureId=fin["fixtureId"], bookmakers="bet365,pinnacle,1xbet")
        if isinstance(ho, dict):
            bm = ho.get("bookmakers", {}) or {}
            rec["top_keys"] = sorted(ho.keys())
            rec["books"] = {s: sorted((b.get("markets") or {}).keys())[:40] for s, b in bm.items()}
            for s, b in bm.items():
                mkts = b.get("markets") or {}
                for mid, m in mkts.items():
                    if mid != "251":
                        rec["sample_non_winner"] = {"book": s, "marketId": mid, "market": trunc(m, 1500)}
                        break
                if "sample_non_winner" in rec:
                    break
        else:
            rec["body"] = trunc(ho, 800)

    if part:
        ph, rec = get("/fixtures", participantId=part, statusId=2)
        if isinstance(ph, list):
            rec["count"] = len(ph)
            times = sorted(f.get("startTime") for f in ph if f.get("startTime"))
            rec["first_start"], rec["last_start"] = (times[0], times[-1]) if times else (None, None)
        else:
            rec["body"] = trunc(ph, 600)

    if upc:
        od, rec = get("/odds", wait=1.3, fixtureId=upc["fixtureId"])
        if isinstance(od, dict):
            bo = od.get("bookmakerOdds", {}) or {}
            rec["start"] = upc.get("startTime")
            rec["books"] = {s: len((b.get("markets") or {})) for s, b in bo.items()}
            for s, b in bo.items():
                mkts = b.get("markets") or {}
                non = [m for m in mkts if m != "251"]
                if non:
                    rec["sample"] = {"book": s, "marketIds": sorted(mkts.keys())[:60],
                                     "one": trunc({non[0]: mkts[non[0]]}, 2500)}
                    break
        else:
            rec["body"] = trunc(od, 800)

    for book in ("bet365", "pinnacle"):
        ob, rec = get("/odds-by-tournaments", wait=1.3, tournamentIds="32129,36349,36377", bookmaker=book)
        if isinstance(ob, list):
            rec["count"] = len(ob)
            st = {}
            withtot = 0
            for f in ob:
                st[str(f.get("statusId"))] = st.get(str(f.get("statusId")), 0) + 1
                bo = (f.get("bookmakerOdds") or {}).get(book) or {}
                if any(m != "251" for m in (bo.get("markets") or {})):
                    withtot += 1
            rec["status_counts"] = st
            rec["with_non_winner_markets"] = withtot
            rec["sample"] = trunc(ob[0], 3000) if ob else None
        else:
            rec["body"] = trunc(ob, 800)

    acct2, rec = get("/account")
    if isinstance(acct2, dict):
        rec["account"] = {k: v for k, v in acct2.items() if "request" in k.lower() or "plan" in k.lower()}


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        OUT["fatal"] = type(e).__name__
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.json"), "w") as f:
        json.dump(OUT, f, indent=1)
