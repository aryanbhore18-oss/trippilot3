"""osm_places.py: free, key-less real places for TripPilot.

Data sources (all free, no API key):
  * Geoapify Places API (optional key)   -> hotels, restaurants, attractions (OSM-based, reliable)
  * OpenStreetMap via the Overpass API   -> same data, no key, but free servers are often busy
  * Nominatim (OSM)                      -> geocoding for destinations without built-in coordinates
  * Wikimedia Commons / Wikidata / Wikipedia -> photos linked from OSM tags
  * Leaflet + OSM tiles                  -> interactive map

Be a good citizen of these free services:
  * Set OSM_CONTACT in your .env (an email or your site) so requests are identifiable.
  * Results are cached in SQLite (db/osm_cache.db), so each destination is fetched at most
    once a week, and the app keeps working from stale data if the servers are down.

Attribution: map and place data (c) OpenStreetMap contributors, ODbL.
"""
import json
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import requests
import streamlit as st

# ----------------------------------------------------------------- config
ENDPOINTS = ["https://overpass-api.de/api/interpreter",
             "https://overpass.kumi.systems/api/interpreter",
             "https://overpass.private.coffee/api/interpreter"]
NOMINATIM = "https://nominatim.openstreetmap.org/search"

LODGING = "hotel|hostel|guest_house|resort|motel|apartment"
FOOD = "restaurant|cafe|fast_food|bar|pub|food_court"
SIGHTS = "attraction|museum|viewpoint|gallery|zoo|theme_park|aquarium"
LODGING_SET, FOOD_SET, SIGHT_SET = (set(x.split("|")) for x in (LODGING, FOOD, SIGHTS))

# (lat, lon, search radius in metres) for the built-in destinations.
GEO = {
    "Goa": (15.5439, 73.7553, 8000),
    "Manali": (32.2396, 77.1887, 6000),
    "Jaipur": (26.9124, 75.7873, 5000),
    "Bali": (-8.5069, 115.2625, 7000),
    "Dubai": (25.2048, 55.2708, 8000),
    "Tokyo": (35.6762, 139.6503, 6000),
    "Paris": (48.8566, 2.3522, 4000),
}
WIKI_TITLES = {"Manali": "Manali, Himachal Pradesh"}  # others use the destination name

DAY = 86400
PLACES_TTL = 7 * DAY
IMAGE_TTL = 30 * DAY
KIND_COLORS = {"hotel": "#0ea5e9", "restaurant": "#f97316", "attraction": "#16a34a"}
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


class OSMError(Exception):
    """Raised when OpenStreetMap data can't be fetched."""


def _cfg(name, default=""):
    """A setting from the environment (.env locally) or Streamlit secrets (when deployed)."""
    val = os.getenv(name)
    if not val:
        try:
            val = st.secrets.get(name)
        except Exception:  # noqa: BLE001 - no secrets file configured
            val = None
    return str(val).strip() if val else default


def _headers():
    # Read lazily so a .env loaded after import is still picked up.
    contact = _cfg("OSM_CONTACT", "set-OSM_CONTACT-in-your-env-file")
    return {"User-Agent": f"TripPilot/1.0 (personal project; {contact})"}


# ------------------------------------------------------------ SQLite cache
DB_PATH = Path(__file__).parent / "db" / "osm_cache.db"


def _db():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS cache "
              "(key TEXT PRIMARY KEY, data TEXT NOT NULL, fetched REAL NOT NULL)")
    return c


def cache_get(key, max_age_s):
    with closing(_db()) as c:
        r = c.execute("SELECT data, fetched FROM cache WHERE key=?", (key,)).fetchone()
    if r and time.time() - r[1] < max_age_s:
        return json.loads(r[0])
    return None


def cache_put(key, value):
    with closing(_db()) as c, c:
        c.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                  (key, json.dumps(value), time.time()))


# ---------------------------------------------------------------- geocoding
_last_nominatim = [0.0]


def centre(destination):
    """(lat, lon, radius_m) for a destination."""
    if destination in GEO:
        return GEO[destination]
    key = f"geo:{destination.lower()}"
    hit = cache_get(key, 90 * DAY)
    if hit:
        return tuple(hit)
    wait = 1.1 - (time.time() - _last_nominatim[0])  # Nominatim: max 1 request/second
    if wait > 0:
        time.sleep(wait)
    _last_nominatim[0] = time.time()
    try:
        r = requests.get(NOMINATIM, params={"q": destination, "format": "jsonv2", "limit": 1},
                         headers=_headers(), timeout=15)
        r.raise_for_status()
        js = r.json()
    except (requests.RequestException, ValueError):
        raise OSMError("Couldn't look up that destination on OpenStreetMap.")
    if not js:
        raise OSMError("OpenStreetMap doesn't know that destination.")
    out = (float(js[0]["lat"]), float(js[0]["lon"]), 7000)
    cache_put(key, list(out))
    return out


# ----------------------------------------------------------------- Overpass
GEOAPIFY = "https://api.geoapify.com/v2/places"
# kind -> (specific categories, fallback parent category, max results)
GEO_CATS = {
    "hotels": ("accommodation.hotel,accommodation.hostel,accommodation.guest_house,"
               "accommodation.motel,accommodation.apartment", "accommodation", 60),
    "restaurants": ("catering.restaurant,catering.cafe,catering.fast_food,catering.bar,catering.pub",
                    "catering", 100),
    "attractions": ("tourism.sights,tourism.attraction,entertainment.museum", "tourism", 60),
}


def _geo_element(f):
    """Turn one Geoapify GeoJSON feature into an Overpass-style element."""
    pr = f.get("properties") or {}
    raw = (pr.get("datasource") or {}).get("raw") or {}
    tags = {k: str(v) for k, v in raw.items()
            if isinstance(v, (str, int, float)) and k not in ("osm_type", "osm_id")}
    tags["name"] = str(pr.get("name") or tags.get("name") or "")
    contact = pr.get("contact") or {}
    for k, v in (("website", pr.get("website") or contact.get("website")),
                 ("opening_hours", pr.get("opening_hours")),
                 ("phone", contact.get("phone")),
                 ("cuisine", (pr.get("catering") or {}).get("cuisine")),
                 ("addr:street", pr.get("street")), ("addr:housenumber", pr.get("housenumber")),
                 ("addr:suburb", pr.get("suburb")), ("addr:city", pr.get("city"))):
        if v and not tags.get(k):
            tags[k] = v
    cats = pr.get("categories") or []
    for c in sorted(cats, key=lambda x: -x.count(".")):  # most specific category first
        head, _, sub = c.partition(".")
        sub = sub.split(".")[0]
        if head == "accommodation":
            tags["tourism"] = sub if sub in LODGING_SET else "guest_house"
            break
        if head == "catering":
            tags["amenity"] = sub if sub in FOOD_SET else "restaurant"
            break
        if head in ("tourism", "entertainment", "heritage"):
            tags["tourism"] = "museum" if "museum" in c else "attraction"
            break
    if any(c.startswith("heritage") for c in cats):
        tags.setdefault("historic", "yes")
    lat, lon = pr.get("lat"), pr.get("lon")
    if lat is None or lon is None:
        coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
    if not tags["name"] or lat is None or lon is None:
        return None
    osm_type = {"n": "node", "w": "way", "r": "relation"}.get(str(raw.get("osm_type", ""))[:1])
    el = {"type": osm_type or "geo", "id": raw.get("osm_id") or str(pr.get("place_id", ""))[:16],
          "lat": lat, "lon": lon, "tags": tags}
    if not osm_type:
        el["_url"] = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}"
    return el


def _geoapify(kind, lat, lon, radius, key):
    """Elements for one kind of place from Geoapify. Never puts the key into an error."""
    cats, parent, limit = GEO_CATS[kind]
    params = {"categories": cats, "filter": f"circle:{lon},{lat},{radius}",
              "bias": f"proximity:{lon},{lat}", "limit": limit, "lang": "en", "apiKey": key}
    try:
        r = requests.get(GEOAPIFY, params=params, headers=_headers(), timeout=20)
        if r.status_code == 400:  # a category name was rejected: retry with the broad parent
            params["categories"] = parent
            r = requests.get(GEOAPIFY, params=params, headers=_headers(), timeout=20)
        if r.status_code in (401, 403):
            raise OSMError("Geoapify rejected the key (check GEOAPIFY_KEY in .env)")
        if r.status_code != 200:
            raise OSMError(f"Geoapify HTTP {r.status_code}")
        feats = r.json().get("features", [])
    except (requests.RequestException, ValueError) as e:
        raise OSMError(f"Geoapify {type(e).__name__}")
    return [e for e in (_geo_element(f) for f in feats) if e]


def _query(kind, lat, lon, radius):
    a = f"(around:{radius},{lat},{lon})"
    if kind == "hotels":
        body, n = f'nwr{a}["tourism"~"^({LODGING})$"]["name"];', 60
    elif kind == "restaurants":
        body, n = f'nwr{a}["amenity"~"^({FOOD})$"]["name"];', 100
    else:
        body = f'(nwr{a}["tourism"~"^({SIGHTS})$"]["name"];nwr{a}["historic"]["name"];);'
        n = 60
    return f"[out:json][timeout:25];\n{body}\nout center tags {n};"


def _overpass(kind, lat, lon, radius):
    """Elements for one kind of place. Raises OSMError (with the reasons) if every server fails."""
    reasons = []
    for url in ENDPOINTS:
        host = url.split("/")[2]
        try:
            resp = requests.post(url, data={"data": _query(kind, lat, lon, radius)},
                                 headers=_headers(), timeout=30)
            if resp.status_code == 200:
                return resp.json().get("elements", [])
            reasons.append(f"{host} HTTP {resp.status_code}")
        except (requests.RequestException, ValueError) as e:
            reasons.append(f"{host} {type(e).__name__}")
        time.sleep(1)
    raise OSMError("; ".join(reasons))


NOMINATIM_TERMS = {"hotels": ["hotel", "hostel", "guest house"],
                   "restaurants": ["restaurant", "cafe"],
                   "attractions": ["museum", "tourist attraction", "viewpoint"]}


def _nominatim_wait():
    wait = 1.1 - (time.time() - _last_nominatim[0])  # Nominatim: max 1 request/second
    if wait > 0:
        time.sleep(wait)
    _last_nominatim[0] = time.time()


def _nominatim_elements(kind, lat, lon, radius):
    """Backup source: OSM's own search server, shaped like Overpass elements."""
    dlat = radius / 111000
    dlon = radius / (111000 * max(math.cos(math.radians(lat)), 0.2))
    box = f"{lon - dlon},{lat + dlat},{lon + dlon},{lat - dlat}"
    out = []
    for term in NOMINATIM_TERMS[kind]:
        _nominatim_wait()
        r = requests.get(NOMINATIM, headers=_headers(), timeout=20,
                         params={"q": term, "format": "jsonv2", "limit": 40, "viewbox": box,
                                 "bounded": 1, "extratags": 1, "addressdetails": 1})
        r.raise_for_status()
        for it in r.json():
            ad = it.get("address") or {}
            tags = dict(it.get("extratags") or {})
            tags.update({"name": it.get("name") or "", it.get("category", ""): it.get("type", ""),
                         "addr:housenumber": ad.get("house_number", ""),
                         "addr:street": ad.get("road", ""), "addr:suburb": ad.get("suburb", ""),
                         "addr:city": ad.get("city") or ad.get("town") or ad.get("village") or ""})
            out.append({"type": it.get("osm_type", "node"), "id": it.get("osm_id", 0),
                        "lat": it["lat"], "lon": it["lon"], "tags": tags})
    return out


def _haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _parse(el, clat, clon):
    """Turn one Overpass element into a compact place dict (or None)."""
    t = {str(k): str(v) for k, v in (el.get("tags") or {}).items() if v is not None}
    name = (t.get("name:en") or t.get("name") or "").strip()
    c = el.get("center", {})
    lat, lon = el.get("lat", c.get("lat")), el.get("lon", c.get("lon"))
    if not name or lat is None or lon is None:
        return None
    tour, amen = t.get("tourism", ""), t.get("amenity", "")
    if tour in LODGING_SET:
        kind, sub = "hotel", tour
    elif amen in FOOD_SET:
        kind, sub = "restaurant", amen
    elif tour in SIGHT_SET or t.get("historic"):
        kind, sub = "attraction", tour or "historic"
    else:
        return None

    site = t.get("website") or t.get("contact:website") or ""
    if not site.startswith(("http://", "https://")):  # never keep javascript: etc.
        site = ""
    m = re.match(r"[1-5]", t.get("stars", ""))
    stars = int(m.group()) if m else None
    street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
    address = ", ".join(x for x in (street, t.get("addr:suburb"), t.get("addr:city")) if x)
    cuisine = t.get("cuisine", "").replace(";", ", ").replace("_", " ")
    phone = t.get("phone") or t.get("contact:phone") or ""
    hints = bool(t.get("wikimedia_commons") or t.get("image") or t.get("wikidata"))
    dist = _haversine(clat, clon, float(lat), float(lon))

    richness = sum(bool(x) for x in (site, t.get("opening_hours"), phone,
                                     cuisine or stars, address, t.get("wikidata")))
    score = richness + (2 if hints else 0) - min(dist, 10) * 0.2 - (1.5 if sub == "fast_food" else 0)
    return {
        "id": f"{el['type']}{el['id']}", "name": name, "kind": kind, "sub": sub,
        "lat": round(float(lat), 5), "lon": round(float(lon), 5), "dist_km": round(dist, 1),
        "cuisine": cuisine, "stars": stars, "address": address, "website": site,
        "hours": t.get("opening_hours", ""), "phone": phone,
        "veg": t.get("diet:vegetarian") in ("yes", "only") or t.get("diet:vegan") in ("yes", "only"),
        "url": el.get("_url") or f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
        "commons": t.get("wikimedia_commons", ""), "image": t.get("image", ""),
        "wikidata": t.get("wikidata", ""), "score": round(score, 2),
    }


def _build(data, clat, clon):
    out = {"hotels": [], "restaurants": [], "attractions": []}
    key_of = {"hotel": "hotels", "restaurant": "restaurants", "attraction": "attractions"}
    seen = set()
    def safe_parse(el):
        try:
            return _parse(el, clat, clon)
        except (TypeError, ValueError, KeyError, AttributeError):
            return None  # skip a malformed place instead of crashing

    parsed = (safe_parse(el) for el in data.get("elements", []))
    for p in sorted((p for p in parsed if p), key=lambda p: (-p["score"], p["dist_km"])):
        sig = (p["kind"], p["name"].lower())
        if sig in seen:  # chains and duplicate mappings: keep the best-scored one
            continue
        seen.add(sig)
        out[key_of[p["kind"]]].append(p)
    for k in out:
        out[k] = out[k][:40]
    out["center"] = [clat, clon]
    return out


def get_places(destination):
    """Hotels, restaurants and attractions around a destination (cached; see 'expires')."""
    key = f"places:{destination.lower()}"
    hit = cache_get(key, float("inf"))
    if hit and hit.get("expires", 0) > time.time():
        return hit
    failed = cache_get(f"fail:{key}", 60)  # don't hammer busy servers on every rerun
    if failed:
        raise OSMError(failed[0])
    lat, lon, radius = centre(destination)
    elements, problems, overpass_ok = [], [], True
    complete, sources = True, set()
    geo_key = _cfg("GEOAPIFY_KEY")
    for kind in ("hotels", "restaurants", "attractions"):
        els = []
        if geo_key:
            try:
                els = _geoapify(kind, lat, lon, radius, geo_key)
                if els:
                    sources.add("geoapify")
            except OSMError as e:
                problems.append(str(e))
                geo_key = ""  # don't retry a failing key for the other kinds
        if not els and overpass_ok:
            try:
                els = _overpass(kind, lat, lon, radius)
                sources.add("osm")
            except OSMError as e:
                problems.append(str(e))
                overpass_ok = False  # don't wait through every server again for the next kind
        if not els:
            try:
                els = _nominatim_elements(kind, lat, lon, radius)
                sources.add("osm")
            except (requests.RequestException, ValueError, KeyError) as e:
                problems.append(f"nominatim {type(e).__name__}")
        complete = complete and bool(els)
        elements += els
    result = _build({"elements": elements}, lat, lon)
    if not any(result[k] for k in ("hotels", "restaurants", "attractions")):
        stale = hit or cache_get(key, float("inf"))  # an old answer beats no answer
        if stale:
            return stale
        reason = "Couldn't reach OpenStreetMap (" + "; ".join(problems or ["no places found"]) + ")"
        cache_put(f"fail:{key}", [reason])
        raise OSMError(reason)
    # Full results are kept for a week; partial ones are retried after an hour.
    result["sources"] = sorted(sources)
    result["expires"] = time.time() + (PLACES_TTL if complete else 3600)
    cache_put(key, result)
    return result


def tier_for(p):
    """Map an OSM hotel to one of TripPilot's price tiers (None if unknown)."""
    if p.get("sub") == "hostel":
        return "Hostel"
    if p.get("sub") == "resort":
        return "Resort"
    if p.get("stars"):
        return {1: "Budget hotel", 2: "Budget hotel", 3: "3-star",
                4: "4-star", 5: "5-star"}[p["stars"]]
    if p.get("sub") in ("guest_house", "motel", "apartment"):
        return "Budget hotel"
    return None


# ------------------------------------------------------------------- images
def _commons_file_url(filename, width=480):
    fn = re.sub(r"^(File|Image):", "", filename.strip(), flags=re.I).replace(" ", "_")
    if not fn.lower().endswith(IMG_EXT):
        return None
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(fn)}?width={width}"


def _find_image(p):
    img = p.get("image", "")
    if re.match(r"https?://upload\.wikimedia\.org/", img) and img.lower().split("?")[0].endswith(IMG_EXT):
        return img
    if "File:" in img:
        return _commons_file_url(img.split("File:")[-1])
    commons = p.get("commons", "")
    if commons.startswith("File:"):
        return _commons_file_url(commons)
    if commons.startswith("Category:"):
        r = requests.get("https://commons.wikimedia.org/w/api.php", headers=_headers(), timeout=10,
                         params={"action": "query", "generator": "categorymembers",
                                 "gcmtitle": commons, "gcmtype": "file", "gcmlimit": 5,
                                 "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": 480,
                                 "format": "json"})
        for pg in r.json().get("query", {}).get("pages", {}).values():
            info = (pg.get("imageinfo") or [{}])[0]
            if info.get("mime", "").startswith("image/") and info.get("thumburl"):
                return info["thumburl"]
    wd = p.get("wikidata", "")
    if re.fullmatch(r"Q\d+", wd):
        r = requests.get("https://www.wikidata.org/w/api.php", headers=_headers(), timeout=10,
                         params={"action": "wbgetclaims", "entity": wd, "property": "P18",
                                 "format": "json"})
        claims = r.json().get("claims", {}).get("P18", [])
        if claims:
            return _commons_file_url(claims[0]["mainsnak"]["datavalue"]["value"])
    return None


def place_image(p):
    """A free photo URL for a place, or None. Never raises."""
    if not (p.get("commons") or p.get("image") or p.get("wikidata")):
        return None  # nothing links this place to a photo, so skip the network
    key = f"img:{p['id']}"
    hit = cache_get(key, IMAGE_TTL)
    if hit is not None:
        return hit.get("url")
    try:
        url = _find_image(p)
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None  # don't cache failures
    cache_put(key, {"url": url})
    return url


def destination_image(destination):
    """Lead photo for a destination from Wikipedia. Never raises."""
    key = f"destimg:{destination.lower()}"
    hit = cache_get(key, IMAGE_TTL)
    if hit is not None:
        return hit.get("url")
    title = WIKI_TITLES.get(destination, destination).replace(" ", "_")
    try:
        r = requests.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title)}",
                         headers=_headers(), timeout=10)
        r.raise_for_status()
        js = r.json()
        url = (js.get("thumbnail") or {}).get("source")
        if url and (js.get("originalimage") or {}).get("width", 0) >= 960:
            url = re.sub(r"/\d+px-", "/960px-", url)
    except (requests.RequestException, ValueError):
        return None
    cache_put(key, {"url": url})
    return url


# -------------------------------------------------------- trip enrichment
def _meal_pick(pool, used, name, desc, prefs):
    want_cafe = name.lower().startswith("breakfast")
    words = set(re.findall(r"[a-z]{4,}", desc.lower())) - {"suggested", "style", "demo", "kitchen"}
    veg = prefs.get("food") in ("Vegetarian", "Vegan")
    best, best_s = None, None
    for i, p in enumerate(pool):
        if p["name"] in used:
            continue
        is_cafe = p["sub"] == "cafe"
        s = 4 if (want_cafe and is_cafe) else (2 if (not want_cafe and not is_cafe) else 0)
        s += 3 if p["cuisine"] and any(w in p["cuisine"].lower() for w in words) else 0
        s += 3 if veg and p["veg"] else 0
        s -= i * 0.01  # pool is already ranked by quality
        if best_s is None or s > best_s:
            best, best_s = p, s
    return best


def enrich_with_osm(trip):
    """Attach real restaurants (and nearby ideas) from OpenStreetMap to a trip.

    Never raises: if OSM is unreachable the trip is returned unchanged."""
    try:
        places = get_places(trip["summary"]["destination"])
        pool = [p for p in places["restaurants"] if p["sub"] != "fast_food"] or places["restaurants"]
        sights = list(places["attractions"])
        prefs = trip.get("prefs", {})
        items = [it for d in trip["days"] for it in d["items"]]
        used = {it["place"]["name"] for it in items if it.get("place")}
        si = 0
        for it in items:
            if it.get("place"):
                continue
            if it["type"] == "meal" and pool:
                p = _meal_pick(pool, used, it["name"], it.get("description", ""), prefs)
                if p is None:
                    used.clear()
                    p = _meal_pick(pool, used, it["name"], it.get("description", ""), prefs)
                if p:
                    used.add(p["name"])
                    it["place"] = {"name": p["name"], "lat": p["lat"], "lon": p["lon"], "url": p["url"]}
                    it["description"] = (p["name"] + (f" · {p['cuisine']}" if p["cuisine"] else "")
                                         + " · from OpenStreetMap")
            elif it["type"] == "free" and sights:
                s = sights[si % len(sights)]
                si += 1
                it["place"] = {"name": s["name"], "lat": s["lat"], "lon": s["lon"], "url": s["url"]}
                it["description"] = f"{it.get('description', '')} · Nearby idea: {s['name']}".strip(" ·")
        trip["places_source"] = "osm"
        note = ("Restaurant, hotel and place names come from OpenStreetMap contributors "
                "and may be incomplete or out of date. Prices remain estimates.")
        if note not in trip.get("notes", []):
            trip.setdefault("notes", []).append(note)
    except Exception:  # noqa: BLE001 - enrichment must never break trip planning
        trip.setdefault("places_source", "none")
    return trip


# ---------------------------------------------------------------------- UI
_MAP_HTML = """<meta name="referrer" content="strict-origin-when-cross-origin">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<div id="m" style="height:440px;border-radius:12px"></div>
<script>
const pts = __PTS__, centre = __CENTRE__, colors = __COLORS__;
const map = L.map('m').setView(centre, 13);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
const bounds = [];
pts.forEach(p => {
  const m = L.circleMarker([p.lat, p.lon], {radius: 7, color: '#fff', weight: 1.5,
    fillColor: colors[p.kind] || '#555', fillOpacity: 0.95}).addTo(map);
  const box = document.createElement('div');
  const b = document.createElement('b'); b.textContent = p.name; box.appendChild(b);
  const s = document.createElement('div'); s.textContent = p.sub; box.appendChild(s);
  m.bindPopup(box); bounds.push([p.lat, p.lon]);
});
if (bounds.length > 1) map.fitBounds(bounds, {padding: [20, 20]});
</script>"""


def _label(p):
    if p["kind"] == "hotel":
        return p["sub"].replace("_", " ").title() + (f" · {p['stars']}★" if p["stars"] else "")
    if p["kind"] == "restaurant":
        return p["cuisine"].title() if p["cuisine"] else p["sub"].replace("_", " ").title()
    return p["sub"].replace("_", " ").title()


def map_html(places, itinerary_places=()):
    pts = []
    for kind, key in (("hotel", "hotels"), ("restaurant", "restaurants"), ("attraction", "attractions")):
        for p in places[key][:20]:
            pts.append({"name": p["name"], "sub": f"{kind.title()} · {_label(p)}",
                        "lat": p["lat"], "lon": p["lon"], "kind": kind})
    for p in itinerary_places:
        pts.append({"name": p["name"], "sub": "In your itinerary", "lat": p["lat"],
                    "lon": p["lon"], "kind": "restaurant"})
    # Encode every "<" so a volunteer-entered name can never close or confuse the <script> tag.
    safe = lambda obj: json.dumps(obj).replace("<", "\\u003c")
    return (_MAP_HTML.replace("__PTS__", safe(pts)).replace("__CENTRE__", safe(places["center"]))
            .replace("__COLORS__", safe(KIND_COLORS)))


def render_map(trip):
    try:
        places = get_places(trip["summary"]["destination"])
    except OSMError as e:
        st.info("The live map is unavailable right now. Try again in a minute.")
        st.caption(f"Details: {e}")
        return
    itin = [it["place"] for d in trip["days"] for it in d["items"] if it.get("place")]
    html = map_html(places, itin)
    if hasattr(st, "iframe"):  # newer Streamlit
        st.iframe(html, height=460)
    else:  # older Streamlit
        import streamlit.components.v1 as components
        components.html(html, height=460)
    st.caption("🔵 hotels · 🟠 restaurants · 🟢 things to see. Map data © OpenStreetMap contributors."
               + (" Places by [Geoapify](https://www.geoapify.com/)." if "geoapify" in places.get("sources", []) else ""))


def _md(text):
    """Escape markdown so volunteer-entered names can't inject links or formatting."""
    return re.sub(r"([\\`*_{}\[\]()#+!|<>~$])", r"\\\1", str(text))


def _card(p, select_cb):
    with st.container(border=True):
        img = place_image(p)
        if img:
            st.image(img)
        st.markdown(f"**{_md(p['name'])}**")
        st.caption(f"{_md(_label(p))} · {p['dist_km']} km from the centre")
        if p["address"]:
            st.caption(_md(p["address"]))
        if p["hours"]:
            st.caption(f"Hours: {_md(p['hours'][:60])}")
        links = [f"[OpenStreetMap]({p['url']})"]
        if p["website"]:
            links.insert(0, f"[Website]({p['website']})")
        st.markdown(" · ".join(links))
        if select_cb and p["kind"] == "hotel":
            st.button("Pick this hotel", key=f"pick_{p['id']}", on_click=select_cb, args=(p,))


def render_places(trip, cur, select_cb=None):
    """Real hotels / restaurants / sights. Returns False if OSM data isn't available."""
    try:
        places = get_places(trip["summary"]["destination"])
    except OSMError:
        return False
    st.subheader("Real places near the centre")
    st.caption("From OpenStreetMap volunteers: names and locations are real, but there are no "
               "prices or ratings, and details can be incomplete or out of date.")
    tabs = st.tabs([f"Hotels ({len(places['hotels'])})",
                    f"Restaurants ({len(places['restaurants'])})",
                    f"Things to see ({len(places['attractions'])})"])
    for tab, key in zip(tabs, ("hotels", "restaurants", "attractions")):
        with tab:
            top = places[key][:9]
            if not top:
                st.write("Nothing found nearby.")
            cols = st.columns(3)
            for i, p in enumerate(top):
                with cols[i % 3]:
                    _card(p, select_cb)
    return True
