"""Probe 2 of the OddsPapi table tennis data. Writes tt/probe.json.
Never prints or stores the API key or any account identity fields."""
import json, os, time, urllib.request, urllib.parse, urllib.error, datetime as dt

KEY = os.environ.get("ODDSPAPI_API_KEY", "").strip()
BASE = "https://api.oddspapi.io/v4"
OUT = {"ran_at": dt.datetime.utcnow().isoformat() + "Z", "calls": []}
SAFE_WORDS = ("request", "limit", "quota", "count", "usage", "used", "remaining")


def get(path, wait=2.2, **params):
    q = dict(params)
    q["apiKey"] = KEY
    url = BASE + path + "?" + urllib.parse.urlencode(q)
    rec = {"path": path, "params": params}
    data = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tt-engine"})
            with urllib.request.urlopen(req, timeout=60) as r:
                rec["status"] = r.status
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            rec["status"] = e.code
            try:
                body = e.read().decode("utf-8")[:400]
            except Exception:
                body = ""
            rec["error_body"] = body.replace(KEY, "***") if KEY else body
            if e.code == 429:
                time.sleep(5)
                continue
            break
        except Exception as e:
            rec["status"] = "EXC " + type(e).__name__
            break
    OUT["calls"].append(rec)
    time.sleep(wait)
    return data, rec


def trunc(obj, n=3000):
    s = json.dumps(obj)
    return obj if len(s) <= n else s[:n] + "...<truncated>"


def numbers_only(obj, depth=0):
    """Keep only numeric usage fields and short plan labels; never identity fields."""
    if isinstance(obj, list):
        subs = [numbers_only(x, depth + 1) for x in obj[:3]]
        return [s for s in subs if s]
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = k.lower()
            if isinstance(v, (int, float, bool)) and any(w in lk for w in SAFE_WORDS):
                out[k] = v
            elif isinstance(v, str) and lk in ("plan", "plan_name", "tier", "status", "plan_slug", "interval") and "@" not in v and len(v) < 40:
                out[k] = v
            elif isinstance(v, (dict, list)) and depth < 3:
                sub = numbers_only(v, depth + 1)
                if sub:
                    out[k] = sub
        out["_keys"] = sorted(obj.keys())
    return out


def account(label):
    acct, rec = get("/account", wait=0.5)
    rec["label"] = label
    if isinstance(acct, dict):
        rec["usage"] = numbers_only(acct)


def main():
    account("start")
    today = dt.datetime.utcnow().date()

    fx, rec = get("/fixtures", tournamentId=32129, statusId=2,
                  **{"from": (today - dt.timedelta(days=1)).isoformat(), "to": today.isoformat()})
    fin = []
    if isinstance(fx, list):
        rec["count"] = len(fx)
        fin = sorted(fx, key=lambda f: f.get("startTime") or "")
    fx30, rec = get("/fixtures", tournamentId=32129, statusId=2,
                    **{"from": (today - dt.timedelta(days=30)).isoformat(), "to": today.isoformat()})
    if isinstance(fx30, list):
        rec["count"] = len(fx30)
        t = sorted(f.get("startTime") for f in fx30 if f.get("startTime"))
        rec["first_start"], rec["last_start"] = (t[0], t[-1]) if t else (None, None)

    if len(fin) > 3:
        a, b = fin[len(fin) // 2], fin[len(fin) // 2 + 1]
        OUT["picked"] = [a.get("fixtureId"), b.get("fixtureId")]
        sc, rec = get("/scores", wait=1.3, fixtureId=a["fixtureId"])
        rec["body"] = trunc(sc)
        sc2, rec = get("/scores", wait=1.3, fixtureId=a["fixtureId"] + "," + b["fixtureId"])
        rec["body"] = trunc(sc2, 1500)
        sc3, rec = get("/scores", wait=1.3, fixtureIds=a["fixtureId"] + "," + b["fixtureId"])
        rec["body"] = trunc(sc3, 1500)
        sc4, rec = get("/scores", wait=1.3, tournamentId=32129)
        rec["body"] = trunc(sc4, 1200)
        st, rec = get("/settlements", wait=1.3, fixtureId=a["fixtureId"])
        rec["body"] = trunc(st, 2500)
        f1, rec = get("/fixture", wait=1.3, fixtureId=a["fixtureId"])
        rec["body"] = trunc(f1, 2500)
        for books in ("bet365,hardrockbet,1xbet", "betano,bwin,superbet"):
            ho, rec = get("/historical-odds", wait=5, fixtureId=a["fixtureId"], bookmakers=books)
            if isinstance(ho, dict):
                bm = ho.get("bookmakers", {}) or {}
                rec["top_keys"] = sorted(ho.keys())
                rec["books"] = {}
                for s, bk in bm.items():
                    info = {}
                    for mid, m in (bk.get("markets") or {}).items():
                        snaps = 0
                        for oid, o in (m.get("outcomes") or {}).items():
                            p = (o.get("players") or {}).get("0")
                            if isinstance(p, list):
                                snaps = max(snaps, len(p))
                        info[mid] = snaps
                    rec["books"][s] = info
                for s, bk in bm.items():
                    for mid, m in (bk.get("markets") or {}).items():
                        if mid != "251" and "sample" not in rec:
                            rec["sample"] = {"book": s, "marketId": mid, "market": trunc(m, 1800)}
            else:
                rec["body"] = trunc(ho, 600)
        ph, rec = get("/fixtures", participantId=a.get("participant1Id"), statusId=2)
        if isinstance(ph, list):
            rec["count"] = len(ph)
            t = sorted(f.get("startTime") for f in ph if f.get("startTime"))
            rec["first_start"], rec["last_start"] = (t[0], t[-1]) if t else (None, None)
        else:
            rec["body"] = trunc(ph, 500)

    up, rec = get("/fixtures", tournamentId=36349, statusId=0, hasOdds="true",
                  **{"from": today.isoformat(), "to": (today + dt.timedelta(days=2)).isoformat()})
    if isinstance(up, list) and up:
        rec["count"] = len(up)
        up = sorted(up, key=lambda f: f.get("startTime") or "")
        pick = up[min(3, len(up) - 1)]
        od, rec = get("/odds", wait=1.3, fixtureId=pick["fixtureId"])
        if isinstance(od, dict):
            rec["start"] = pick.get("startTime")
            bo = od.get("bookmakerOdds", {}) or {}
            tot = {}
            for s, bk in bo.items():
                lines = []
                for mid, m in (bk.get("markets") or {}).items():
                    if mid == "251":
                        continue
                    main = any(((o.get("players") or {}).get("0") or {}).get("mainLine") for o in (m.get("outcomes") or {}).values())
                    lines.append(mid + ("*" if main else ""))
                if lines:
                    tot[s] = lines[:14]
            rec["books_total"] = len(bo)
            rec["non_winner_by_book"] = tot
            rec["hardrock"] = trunc(bo.get("hardrockbet"), 1500)

    for book in ("hardrockbet", "1xbet", "betano"):
        ob, rec = get("/odds-by-tournaments", wait=1.3, tournamentIds="32129,36349,36377", bookmaker=book)
        if isinstance(ob, list):
            rec["count"] = len(ob)
            withtot = 0
            byt = {}
            starts = []
            for f in ob:
                bo = (f.get("bookmakerOdds") or {}).get(book) or {}
                if any(m != "251" for m in (bo.get("markets") or {})):
                    withtot += 1
                    starts.append(f.get("startTime"))
                byt[str(f.get("tournamentId"))] = byt.get(str(f.get("tournamentId")), 0) + 1
            rec["with_non_winner_markets"] = withtot
            rec["by_tournament"] = byt
            starts = sorted(s for s in starts if s)
            rec["totals_first_start"], rec["totals_last_start"] = (starts[0], starts[-1]) if starts else (None, None)
            rec["bytes"] = len(json.dumps(ob))
        else:
            rec["body"] = trunc(ob, 400)

    account("end")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        OUT["fatal"] = type(e).__name__ + " line " + str(e.__traceback__.tb_lineno)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.json"), "w") as f:
        json.dump(OUT, f, indent=1)
