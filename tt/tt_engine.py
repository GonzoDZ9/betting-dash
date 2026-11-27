"""Table tennis totals feed for the Betting Models site.

Two modes:
  python tt/tt_engine.py           full pull, once a day (about 2,000 page reads)
  python tt/tt_engine.py --grade   results only, every 15 minutes: reads just the
                                   pages of recommended matches that should have
                                   finished, and settles them (a handful of reads)

Once a day the full pull reads the free public player and matchup pages on
tabletennistotals.com and writes tt_dash.json at the repo root:
for every upcoming match, the total points of each player's last 20 matches
and of every head-to-head meeting, so the page can work out over/under rates
at any bar from 62.5 to 80.5. It also recommends matches (the two players combined
on one side of a bar in more than 70% of their last 20 matches each, or at least
70% of 10 or more head-to-head meetings on one side) and tracks how those
recommendations finish (tt/track.json).

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
BARS = [62.5 + i for i in range(19)]      # 62.5 ... 80.5
REC_PCT = 0.70                            # the two players combined must be above this share on one side
REC_MIN_MATCHES = 15                      # each player needs at least this many recent matches for that test
H2H_PCT = 0.70                            # or: their head-to-head meetings at or above this share on one side
H2H_MIN_MATCHES = 10                      # ... with at least this many meetings
SKIP_LEAGUES = ("Setka Cup Women",)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(HERE, "players.json")
OUTFILE = os.path.join(ROOT, "tt_dash.json")
TRACK = os.path.join(HERE, "track.json")
UA = "betting-dash-tt/1.0 (+https://github.com/GonzoDZ9/betting-dash; personal stats page: one daily pull plus light result checks)"
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


def completed(m):
    """A finished best-of-five: someone won three sets."""
    sets = m.get("sets") or []
    w = sum(1 for s in sets if (s.get("player") or 0) > (s.get("opponent") or 0))
    return max(w, len(sets) - w) == 3


def recent_totals(matches):
    ms = sorted([m for m in matches if m.get("sets")], key=lambda m: -(m.get("timestamp") or 0))[:20]
    return [match_total(m) for m in ms]


def h2h_totals(d):
    hist = sorted((d.get("h2hStats") or {}).get("detailedHistory") or [], key=lambda x: -(x.get("timestamp") or 0))
    return [int(h["totalPoints"]) for h in hist if isinstance(h.get("totalPoints"), (int, float))]


def recommend(ta, tb, h=()):
    """{bar: 'O'|'U'} for every bar where either test passes:
      1. the two players combined (their last 20 matches each, pooled) finished on one
         side of the bar in more than 70% of those matches, or
      2. they have met at least 10 times and at least 70% of those meetings finished
         on one side of the bar.
    No pick when the two tests point in opposite directions."""
    out = {}
    form_ok = len(ta) >= REC_MIN_MATCHES and len(tb) >= REC_MIN_MATCHES
    h2h_ok = len(h) >= H2H_MIN_MATCHES
    if not form_ok and not h2h_ok:
        return out
    for bar in BARS:
        form = meet = None
        if form_ok:
            over = (sum(1 for t in ta if t > bar) + sum(1 for t in tb if t > bar)) / float(len(ta) + len(tb))
            form = "O" if over > REC_PCT else "U" if (1 - over) > REC_PCT else None
        if h2h_ok:
            over = sum(1 for t in h if t > bar) / float(len(h))
            meet = "O" if over >= H2H_PCT else "U" if (1 - over) >= H2H_PCT else None
        if form and meet and form != meet:
            continue
        side = form or meet
        if side:
            out["%.1f" % bar] = side
    return out


def pick_key(id_a, id_b, ts):
    lo, hi = sorted([str(id_a), str(id_b)])
    return "%s-%s-%s" % (lo, hi, ts)


def find_result(page, opp_id, ts):
    """Total points of the finished match vs opp_id that started near ts, or None."""
    best = None
    for m in (page or {}).get("recentMatches") or []:
        if str(m.get("opponentId")) != str(opp_id):
            continue
        gap = abs((m.get("timestamp") or 0) - ts)
        if gap <= 3 * 3600 and (best is None or gap < best[0]):
            best = (gap, m)
    if not best:
        return None
    m = best[1]
    return match_total(m) if completed(m) else "void"


GRADE_AFTER = 25 * 60      # light mode: first look for a result this long after the start
GRADE_MAX_READS = 150      # light mode: page reads allowed per run


def grade(track, pages, read, now, light=False):
    """Settle open recommendations whose match has finished. Returns how many were settled.

    light=True is the every-15-minutes pass: it looks sooner after the start, and a match
    whose result is still missing after two hours is only re-read about once an hour
    (once every six hours after six hours), so a missing result is not re-read every run."""
    still = []
    settled = 0
    for p in track["open"]:
        if p["ts"] > now - (GRADE_AFTER if light else 3600):
            still.append(p)
            continue
        if light:
            age, t = now - p["ts"], time.gmtime(now)
            wait = (age > 2 * 3600 and t.tm_min >= 15) or (age > 6 * 3600 and t.tm_hour % 6 != 0)
            if wait or STATS["requests"] >= GRADE_MAX_READS:
                still.append(p)
                continue
        page = pages.get(p["a_slug"]) or read(p["a_slug"])
        res = find_result(page, p["b_id"], p["ts"])
        if res is None and p.get("b_slug"):
            page_b = pages.get(p["b_slug"]) or read(p["b_slug"])
            res = find_result(page_b, p["a_id"], p["ts"])
        if res is None:
            if now - p["ts"] < 3 * 86400:
                still.append(p)          # result not posted yet
            continue                     # never posted: drop without grading
        settled += 1
        if res == "void":
            continue
        day = dt.datetime.utcfromtimestamp(p["ts"]).strftime("%Y-%m-%d")
        d = track["days"].setdefault(day, {})
        for bar, side in p["recs"].items():
            won = (res > float(bar)) == (side == "O")
            wl = d.setdefault(bar, [0, 0])
            wl[0 if won else 1] += 1
        track["recent"].append({"ts": p["ts"], "lg": p["lg"], "a": p["a"], "b": p["b"], "tot": res, "recs": p["recs"]})
    track["open"] = still
    track["recent"] = sorted([r for r in track["recent"] if r["ts"] > now - 4 * 86400], key=lambda r: -r["ts"])
    return settled


def grade_only():
    """Every-15-minutes pass: settle finished recommendations and refresh the record in
    tt_dash.json. The match list and its numbers are left exactly as the daily pull built them."""
    now = int(time.time())
    try:
        track = json.load(open(TRACK))
        out = json.load(open(OUTFILE))
    except Exception:
        print("grade: no feed yet, nothing to do")
        return
    track.setdefault("open", [])
    track.setdefault("days", {})
    track.setdefault("recent", [])
    pages = {}

    def read(slug):
        if not slug:
            return None
        if slug not in pages:
            pages[slug] = get_player(slug)
        return pages[slug]

    settled = grade(track, pages, read, now, light=True)
    if settled:
        with open(TRACK, "w") as f:
            json.dump(track, f, separators=(",", ":"), sort_keys=True)
        out["graded_at"] = dt.datetime.utcfromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%SZ")
        out["track"] = {"since": track.get("since"), "days": track["days"], "recent": track["recent"],
                        "open": [{"ts": p["ts"], "recs": p["recs"]} for p in track["open"]]}
        with open(OUTFILE, "w") as f:
            json.dump(out, f, separators=(",", ":"))
    print("grade: settled=%d open=%d requests=%d errors=%d secs=%d" % (
        settled, len(track["open"]), STATS["requests"], STATS["errors"], time.time() - T0))


def main():
    now = int(time.time())
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    players = cache.get("players", {})          # slug -> {id, name, lg, last}
    try:
        track = json.load(open(TRACK))
    except Exception:
        track = {}
    track.setdefault("since", dt.datetime.utcfromtimestamp(now).strftime("%Y-%m-%d"))
    track.setdefault("open", [])
    track.setdefault("days", {})
    track.setdefault("recent", [])

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
        elif p.get("lg") in SKIP_LEAGUES:
            if zlib.crc32(s.encode()) % 7 == weekday:
                todo.append(s)
        elif p.get("last") and now - p["last"] <= ACTIVE_DAYS * 86400:
            todo.append(s)                      # active: read every day
        elif zlib.crc32(s.encode()) % 7 == weekday:
            todo.append(s)                      # inactive: re-check once a week
    todo.sort(key=lambda s: -((players.get(s) or {}).get("last") or 0))

    pages = {}
    truncated = False

    def read(slug):
        if not slug or slug in pages:
            return pages.get(slug)
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

    for s in todo:
        if time.time() - T0 > MAX_RUN_SEC:
            truncated = True
            break
        read(s)

    by_id = {p["id"]: s for s, p in players.items() if p.get("id")}

    # ----- settle yesterday's recommendations -----
    grade(track, pages, read, now)
    open_by_key = {p["key"]: p for p in track["open"]}

    # ----- slate: every upcoming match listed on a player page -----
    horizon = now + SLATE_HOURS * 3600
    slate = {}
    for slug, d in pages.items():
        me = players[slug]
        for u in d.get("upcomingMatches") or []:
            ts = u.get("timestamp") or 0
            lg = u.get("league") or me.get("lg")
            if ts < now - 600 or ts > horizon or lg in SKIP_LEAGUES:
                continue
            oid = str(u.get("opponentId"))
            key = pick_key(me["id"], oid, ts)
            if key in slate:
                continue
            slate[key] = {"ts": ts, "lg": lg, "a": slug, "b_id": oid, "b_name": u.get("opponentName")}

    matches = []
    for key, m in sorted(slate.items(), key=lambda kv: kv[1]["ts"]):
        a_slug = m["a"]
        b_slug = by_id.get(m["b_id"])
        if b_slug and b_slug not in pages and time.time() - T0 < MAX_RUN_SEC + 300:
            read(b_slug)
        a_page, b_page = pages.get(a_slug), pages.get(b_slug) if b_slug else None
        a_id = players[a_slug]["id"]
        ta = recent_totals(a_page.get("recentMatches") or [])
        tb = recent_totals(b_page.get("recentMatches") or []) if b_page else []
        a_name = players[a_slug]["name"]
        b_name = players[b_slug]["name"] if b_slug and b_slug in players else m["b_name"]

        ht = []
        if b_slug and time.time() - T0 < MAX_RUN_SEC + 600:
            try:
                first = int(a_id) < int(m["b_id"])
            except Exception:
                first = True
            lo, hi = (a_slug, b_slug) if first else (b_slug, a_slug)
            d = get_matchup("%s-%s" % (lo, hi)) or get_matchup("%s-%s" % (hi, lo))
            if d:
                ht = h2h_totals(d)

        recs = recommend(ta, tb, ht)
        if key in open_by_key:
            # still to be played: keep the record in step with the latest numbers
            if recs:
                open_by_key[key]["recs"] = recs
            else:
                track["open"] = [p for p in track["open"] if p["key"] != key]
                del open_by_key[key]
        else:
            if recs and m["ts"] > now:
                p = {"key": key, "ts": m["ts"], "lg": m["lg"], "a": a_name, "b": b_name,
                     "a_slug": a_slug, "b_slug": b_slug, "a_id": a_id, "b_id": m["b_id"], "recs": recs}
                track["open"].append(p)
                open_by_key[key] = p
        matches.append({"id": key, "ts": m["ts"], "lg": m["lg"],
                        "a": {"name": a_name, "t": ta}, "b": {"name": b_name, "t": tb},
                        "h": ht, "rec": recs})

    # keep 120 days of daily records
    cutoff = dt.datetime.utcfromtimestamp(now - 120 * 86400).strftime("%Y-%m-%d")
    track["days"] = {d: v for d, v in track["days"].items() if d >= cutoff}

    out = {
        "generated_at": dt.datetime.utcfromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "v": 2,
        "bars": BARS,
        "rule": {"pct": REC_PCT, "min_matches": REC_MIN_MATCHES, "h2h_pct": H2H_PCT, "h2h_min": H2H_MIN_MATCHES, "mode": "combined_or_h2h"},
        "source": "tabletennistotals.com public player and matchup pages",
        "leagues": sorted({m["lg"] for m in matches if m.get("lg")}),
        "counts": {"matches": len(matches), "players_read": len(pages), "players_known": len(players),
                   "requests": STATS["requests"], "errors": STATS["errors"], "truncated": truncated},
        "track": {"since": track["since"], "days": track["days"], "recent": track["recent"],
                  "open": [{"ts": p["ts"], "recs": p["recs"]} for p in track["open"]]},
        "matches": matches,
    }
    with open(OUTFILE, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    with open(TRACK, "w") as f:
        json.dump(track, f, separators=(",", ":"), sort_keys=True)
    players = {s: p for s, p in players.items() if s in slugset}
    with open(CACHE, "w") as f:
        json.dump({"updated": out["generated_at"], "players": players}, f, separators=(",", ":"), sort_keys=True)
    print("matches=%d recs=%d open=%d players_read=%d requests=%d errors=%d truncated=%s secs=%d" % (
        len(matches), sum(1 for m in matches if m["rec"]), len(track["open"]), len(pages),
        STATS["requests"], STATS["errors"], truncated, time.time() - T0))


if __name__ == "__main__":
    try:
        if "--grade" in sys.argv[1:]:
            grade_only()
        else:
            main()
    except Blocked as e:
        # Leave yesterday's feed in place and fail loudly rather than publish an empty page.
        print("STOPPED: %s" % e)
        sys.exit(1)
