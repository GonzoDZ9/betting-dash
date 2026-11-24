"""Key diagnostic for OddsPapi. Writes tt/probe.json. Never prints or stores the key itself."""
import json, os, re, urllib.request, urllib.error, datetime as dt

RAW = os.environ.get("ODDSPAPI_API_KEY", "")
KEY = RAW.strip().strip('"').strip("'")
OUT = {"ran_at": dt.datetime.utcnow().isoformat() + "Z",
       "shape": {"raw_len": len(RAW), "clean_len": len(KEY),
                 "has_whitespace": bool(re.search(r"\s", RAW)),
                 "has_quotes": ('"' in RAW or "'" in RAW),
                 "looks_like_uuid": bool(re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", KEY)),
                 "only_hex_and_dashes": bool(re.fullmatch(r"[0-9a-fA-F-]+", KEY)),
                 "only_alnum_dash_underscore": bool(re.fullmatch(r"[A-Za-z0-9_-]+", KEY)),
                 "has_url_parts": ("http" in RAW.lower() or "=" in RAW or "&" in RAW or "?" in RAW),
                 "has_at_sign": "@" in RAW},
       "calls": []}


def call(label, url, headers=None):
    rec = {"label": label}
    try:
        req = urllib.request.Request(url, headers=dict({"User-Agent": "tt-engine"}, **(headers or {})))
        with urllib.request.urlopen(req, timeout=30) as r:
            rec["status"] = r.status
            rec["body"] = r.read().decode("utf-8")[:300].replace(KEY, "***")
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        try:
            rec["body"] = e.read().decode("utf-8")[:300].replace(KEY, "***")
        except Exception:
            pass
    except Exception as e:
        rec["status"] = "EXC " + type(e).__name__
    OUT["calls"].append(rec)


call("v4 account, cleaned key", "https://api.oddspapi.io/v4/account?apiKey=" + urllib.request.quote(KEY))
call("v4 account, no key (baseline)", "https://api.oddspapi.io/v4/account")
call("v5 sports, header key", "https://v5.oddspapi.io/en/sports", {"X-API-Key": KEY})
call("v5 sports, no key (baseline)", "https://v5.oddspapi.io/en/sports")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.json"), "w") as f:
    json.dump(OUT, f, indent=1)
