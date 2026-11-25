"""Table tennis totals feed for the Betting Models site.

Once a day this reads the free public player and matchup pages on
tabletennistotals.com and writes tt_dash.json at the repo root:
for every upcoming match, the total points of each player's last 20 matches
and of every head-to-head meeting, so the page can work out over/under rates
at any bar from 62.5 to 80.5. It also recommends matches (at least one player on one side of a bar in more
than 75% of their last 20, unless the other player is above 75% the opposite
way) and tracks how those recommendations finish (tt/track.json).

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
REC_PCT = 0.75                            # a player is "consistent" above this share, either side
REC_MIN_MATCHES = 15                      # each player needs at least this many recent matches
SKIP_LEAGUES = ("Setka Cup Women",)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(HERE, "players.json")
OUTFILE = os.path.join(ROOT, "tt_dash.json")
TRACK = os.path.join(HERE, "track.json")
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


def recommend(ta, tb):
    """{bar: 'O'|'U'} for every bar where at least one player finished on one side
    in more than 75% of their last 20 matches. The pick follows that player.
    No pick when the two players are both above 75% on opposite sides."""
    out = {}
    if len(ta) < REC_MIN_MATCHES or len(tb) < REC_MIN_MATCHES:
        return out
    for bar in BARS:
        pa = sum(1 for t in ta if t > bar) / float(len(ta))
        pb = sum(1 for t in tb if t > bar) / float(len(tb))
        over = pa > REC_PCT or pb > REC_PCT
        under = (1 - pa) > REC_PCT or (1 - pb) > REC_PCT
        if over and not under:
            out["%.1f" % bar] = "O"
        elif under and not over:
            out["%.1f" % bar] = "U"
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


def grade(track, pages, read, now):
    """Settle open recommendations whose match has finished."""
    still = []
    for p in track["open"]:
        if p["ts"] > now - 3600:
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

        recs = recommend(ta, tb)
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
        "rule": {"pct": REC_PCT, "min_matches": REC_MIN_MATCHES},
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
        main()
    except Blocked as e:
        # Leave yesterday's feed in place and fail loudly rather than publish an empty page.
        print("STOPPED: %s" % e)
        sys.exit(1)
