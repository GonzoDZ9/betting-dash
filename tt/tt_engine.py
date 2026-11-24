"""Table tennis totals feed for the Betting Models site.

Once a day this reads the free public player and matchup pages on
tabletennistotals.com and writes tt_dash.json at the repo root:
for every upcoming match, how often each player's last 5 / 10 / 15 / 20
matches went over the 74.5 total, the average total and its gap to the line,
and the same for the two players' head-to-head history.

Standard library only. No API key needed.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zlib

SITE = "https://tabletennistotals.com"
LINE = 74.5
WINDOWS = (5, 10, 15, 20)
LEAGUES = ("TT Cup", "Setka Cup", "Czech Liga Pro", "TT Elite Series")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(HERE, "players.json")
OUTFILE = os.path.join(ROOT, "tt_dash.json")
UA = "betting-dash-tt/1.0 (+https://github.com/GonzoDZ9/betting-dash; once-daily personal stats page)"
PAUSE = 0.3            # seconds between requests
ACTIVE_DAYS = 10       # players who played this recently are re-read every day
SLATE_HOURS = 40       # how far ahead to list matches
MAX_RUN_SEC = 50 * 60  # stop reading new pages after this long

T0 = time.time()
STATS = {"requests": 0, "errors": 0, "not_found": 0}
_fail_streak = 0


class Blocked(Exception):
    pass


def fetch(path):
    """GET a site path. Returns text, or None for 404 / repeated failure."""
    global _fail_streak
    url = SITE + path
    for attempt in range(3):
        STATS["requests"] += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            _fail_streak = 0
            time.sleep(PAUSE)
            return body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                STATS["not_found"] += 1
                _fail_streak = 0
                time.sleep(PAUSE)
                return None
            STATS["errors"] += 1
            _fail_streak += 1
            if e.code in (401, 403) and _fail_streak >= 6:
                raise Blocked("site refused %d requests in a row (HTTP %d)" % (_fail_streak, e.code))
            time.sleep(4 * (attempt + 1) if e.code in (429, 500, 502, 503, 504) else 1)
        except Exception:
            STATS["errors"] += 1
            _fail_streak += 1
            time.sleep(2 * (attempt + 1))
        if _fail_streak >= 25:
            raise Blocked("25 failed requests in a row")
    return None


def decode(text):
    """Decode the site's page-data format (a flat array of cross-referenced values)."""
    arr = json.loads(text)
    memo = {}

    def val(i):
        if not isinstance(i, int) or i < 0:
            return None
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str):   # typed value (date, error...) - not needed
                memo[i] = None
                return None
            out = []
            memo[i] = out
            out.extend(val(x) for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            memo[i] = out
            for k, ref in v.items():
                out[arr[int(k[1:])]] = val(ref)
            return out
        memo[i] = v
        return v

    return val(0)


def get_player(slug):
    text = fetch("/player/%s.data" % slug)
    if not text:
        return None
    try:
        d = (decode(text).get("routes/player") or {}).get("data") or {}
    except Exception:
        STATS["errors"] += 1
        return None
    if not d.get("player"):
        return None
    return d


def get_matchup(slug):
    text = fetch("/match/%s.data" % slug)
    if not text:
        return None
    try:
        d = (decode(text).get("routes/match") or {}).get("data") or {}
    except Exception:
        STATS["errors"] += 1
        return None
    return d if d.get("h2hStats") else None


def match_total(m):
    return sum((s.get("player") or 0) + (s.get("opponent") or 0) for s in (m.get("sets") or []))


def r1(x):
    return round(x + 0.0, 1)


def player_stats(matches):
    """Over/under 74.5 record for the last 5/10/15/20 matches."""
    ms = sorted([m for m in matches if m.get("sets")], key=lambda m: -(m.get("timestamp") or 0))[:20]
    totals = [match_total(m) for m in ms]
    out = {"n": len(totals), "t": totals, "last": ms[0].get("timestamp") if ms else None, "w": {}}
    for w in WINDOWS:
        sub = totals[:w]
        if not sub:
            continue
        over = sum(1 for t in sub if t > LINE)
        avg = sum(sub) / len(sub)
        out["w"][str(w)] = {"n": len(sub), "o": over, "u": len(sub) - over,
                            "op": round(100.0 * over / len(sub)), "avg": r1(avg), "gap": r1(avg - LINE)}
    return out


def h2h_stats(d):
    hist = sorted((d.get("h2hStats") or {}).get("detailedHistory") or [], key=lambda x: -(x.get("timestamp") or 0))
    totals = [h.get("totalPoints") for h in hist if isinstance(h.get("totalPoints"), (int, float))]
    if not totals:
        return {"n": 0}
    out = {"n": len(totals), "w": {}}
    over = sum(1 for t in totals if t > LINE)
    avg = sum(totals) / len(totals)
    out["all"] = {"n": len(totals), "o": over, "u": len(totals) - over, "op": round(100.0 * over / len(totals)),
                  "avg": r1(avg), "gap": r1(avg - LINE)}
    for w in WINDOWS:
        sub = totals[:w]
        o = sum(1 for t in sub if t > LINE)
        a = sum(sub) / len(sub)
        out["w"][str(w)] = {"n": len(sub), "o": o, "u": len(sub) - o, "op": round(100.0 * o / len(sub)),
                            "avg": r1(a), "gap": r1(a - LINE)}
    out["recent"] = [{"ts": h.get("timestamp"), "tot": h.get("totalPoints"),
                      "sc": "%s-%s" % (h.get("homeSetScore"), h.get("awaySetScore"))} for h in hist[:10]]
    return out


def lean(a, b, h):
    """A plain agreement check, not a tested betting rule:
    both players went the same way in at least 60% of their last 10 and last 20."""
    def side(s):
        w10, w20 = s["w"].get("10"), s["w"].get("20")
        if not w10 or not w20 or w20["n"] < 10:
            return None
        if w10["op"] >= 60 and w20["op"] >= 60:
            return "O"
        if w10["op"] <= 40 and w20["op"] <= 40:
            return "U"
        return None
    sa, sb = side(a), side(b)
    vals = [s["w"][k]["op"] for s in (a, b) for k in ("10", "20") if s["w"].get(k)]
    score = r1(sum(vals) / len(vals) - 50) if vals else 0.0
    out = {"score": score, "side": sa if (sa and sa == sb) else None}
    if out["side"] and h.get("n", 0) >= 5:
        hp = h["all"]["op"]
        out["h2h"] = "agrees" if (hp >= 55 and out["side"] == "O") or (hp <= 45 and out["side"] == "U") else (
            "against" if (hp <= 45 and out["side"] == "O") or (hp >= 55 and out["side"] == "U") else "split")
    return out


def main():
    now = int(time.time())
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    players = cache.get("players", {})          # slug -> {id, name, lg, last}

    sm = fetch("/sitemap.xml") or ""
    slugs = re.findall(r"<loc>%s/player/([^<]+)</loc>" % re.escape(SITE), sm)
    if not slugs:
        raise Blocked("could not read the site's player list")
    slugset = set(slugs)

    weekday = dt.datetime.utcnow().weekday()
    first_run = len(players) < 200
    todo = []
    for s in slugs:
        p = players.get(s)
        if p is None or first_run:
            todo.append(s)                      # new player (or first full read)
        elif p.get("last") and now - p["last"] <= ACTIVE_DAYS * 86400:
            todo.append(s)                      # active: read every day
        elif zlib.crc32(s.encode()) % 7 == weekday:
            todo.append(s)                      # inactive: re-check once a week
    # active players first so the slate is complete even if time runs out
    todo.sort(key=lambda s: -((players.get(s) or {}).get("last") or 0))

    pages = {}                                  # slug -> page data read this run
    truncated = False

    def read(slug):
        d = get_player(slug)
        if not d:
            if slug in players:
                players[slug]["miss"] = players[slug].get("miss", 0) + 1
            else:
                players[slug] = {"id": None, "name": slug, "lg": None, "last": 0, "miss": 1}
            return None
        pl = d["player"]
        rm = d.get("recentMatches") or []
        last = max([m.get("timestamp") or 0 for m in rm] or [0])
        players[slug] = {"id": str(pl.get("id")), "name": pl.get("name"), "lg": pl.get("leagueName"), "last": last}
        pages[slug] = d
        return d

    for i, s in enumerate(todo):
        if time.time() - T0 > MAX_RUN_SEC:
            truncated = True
            break
        read(s)

    by_id = {p["id"]: s for s, p in players.items() if p.get("id")}

    # ----- slate: every upcoming match listed on a player page -----
    horizon = now + SLATE_HOURS * 3600
    slate = {}
    for slug, d in pages.items():
        me = players[slug]
        for u in d.get("upcomingMatches") or []:
            ts = u.get("timestamp") or 0
            if ts < now - 600 or ts > horizon:
                continue
            oid = str(u.get("opponentId"))
            key = str(u.get("id") or "") or "%s-%s-%s" % (min(me["id"], oid), max(me["id"], oid), ts)
            if key in slate:
                continue
            slate[key] = {"id": key, "ts": ts, "lg": u.get("league") or me.get("lg"),
                          "a": slug, "b_id": oid, "b_name": u.get("opponentName")}

    matches = []
    for key, m in sorted(slate.items(), key=lambda kv: kv[1]["ts"]):
        a_slug = m["a"]
        b_slug = by_id.get(m["b_id"])
        if b_slug and b_slug not in pages and time.time() - T0 < MAX_RUN_SEC + 300:
            read(b_slug)
        a_page, b_page = pages.get(a_slug), pages.get(b_slug) if b_slug else None
        a_id = players[a_slug]["id"]
        a = player_stats(a_page.get("recentMatches") or [])
        b = player_stats(b_page.get("recentMatches") or []) if b_page else {"n": 0, "t": [], "w": {}, "last": None}
        a["name"], a["slug"] = players[a_slug]["name"], a_slug
        b["name"], b["slug"] = (players[b_slug]["name"] if b_slug else m["b_name"]), b_slug

        h = {"n": 0}
        if b_slug and time.time() - T0 < MAX_RUN_SEC + 600:
            try:
                first = int(a_id) < int(m["b_id"])
            except Exception:
                first = True
            lo, hi = (a_slug, b_slug) if first else (b_slug, a_slug)
            d = get_matchup("%s-%s" % (lo, hi)) or get_matchup("%s-%s" % (hi, lo))
            if d:
                h = h2h_stats(d)
                h["slug"] = (d.get("matchup") or {}).get("slug")
        rec = {"id": key, "ts": m["ts"], "lg": m["lg"], "a": a, "b": b, "h2h": h}
        if a["w"].get("20") and b["w"].get("20"):
            ca = (a["w"]["20"]["avg"] + b["w"]["20"]["avg"]) / 2.0
            rec["avg20"] = r1(ca)
            rec["gap20"] = r1(ca - LINE)
        if a["w"].get("10") and b["w"].get("10"):
            ca = (a["w"]["10"]["avg"] + b["w"]["10"]["avg"]) / 2.0
            rec["avg10"] = r1(ca)
            rec["gap10"] = r1(ca - LINE)
        rec["lean"] = lean(a, b, h)
        matches.append(rec)

    out = {
        "generated_at": dt.datetime.utcfromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "line": LINE,
        "source": "tabletennistotals.com public player and matchup pages",
        "leagues": sorted({m["lg"] for m in matches if m.get("lg")}),
        "counts": {"matches": len(matches), "players_read": len(pages), "players_known": len(players),
                   "requests": STATS["requests"], "errors": STATS["errors"], "truncated": truncated},
        "matches": matches,
    }
    with open(OUTFILE, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    # drop players the site no longer lists
    players = {s: p for s, p in players.items() if s in slugset}
    with open(CACHE, "w") as f:
        json.dump({"updated": out["generated_at"], "players": players}, f, separators=(",", ":"), sort_keys=True)
    print("matches=%d players_read=%d requests=%d errors=%d truncated=%s secs=%d" % (
        len(matches), len(pages), STATS["requests"], STATS["errors"], truncated, time.time() - T0))


if __name__ == "__main__":
    try:
        main()
    except Blocked as e:
        # Leave yesterday's feed in place and fail loudly rather than publish an empty page.
        print("STOPPED: %s" % e)
        sys.exit(1)
