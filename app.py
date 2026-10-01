"""TripPilot: AI-powered personalized travel planner (single-file Streamlit MVP).

HOW TO RUN (Windows / VS Code terminal):
    pip install streamlit pandas python-dotenv openai requests
    streamlit run trippilot.py

Optional AI: create a file named .env next to this file containing
    OPENAI_API_KEY=your_key_here
Without a key, a built-in demo planner is used (works offline).

IMPORTANT: hotels, activities, restaurants, prices and exchange rates are DEMO
estimates, not live data. Nothing is ever booked.
"""
import copy
import json
import math
import os
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import osm_places  # free OpenStreetMap places, photos and map

st.set_page_config(page_title="TripPilot", page_icon="🧭", layout="wide")
st.markdown("""
<style>
.block-container {max-width: 1100px; padding-top: 2rem;}
.hero {background: linear-gradient(135deg,#0f766e,#0ea5e9); color: white;
       padding: 3rem 2rem; border-radius: 24px; margin-bottom: 1.5rem;}
.hero h1 {color: white; font-size: 2.6rem; margin-bottom: .5rem;}
.hero p {font-size: 1.1rem; opacity: .95; max-width: 640px;}
.pill {display:inline-block; padding: 2px 10px; border-radius: 999px;
       background:#e0f2fe; color:#075985; font-size:.8rem; margin-right:6px;}
</style>""", unsafe_allow_html=True)


# ======================================================================
# 1. DEMO DATA (mock, not real inventory)
# ======================================================================
# Demo exchange rates from INR. NOT live rates.
CURRENCY_RATES = {"INR": 1.0, "USD": 0.012, "EUR": 0.011, "GBP": 0.0095}
CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}

# Accommodation tiers, cheapest to most expensive, with price multipliers
# applied to each destination's base 3-star price.
TIERS = ["Hostel", "Budget hotel", "3-star", "4-star", "Boutique", "Resort", "5-star"]
TIER_MULT = {"Hostel": 0.35, "Budget hotel": 0.7, "3-star": 1.0, "4-star": 1.6,
             "Boutique": 1.9, "Resort": 2.5, "5-star": 3.0}

TRAVEL_TYPES = ["Solo", "Couple", "Friends", "Family", "Business"]
STYLES = ["Relaxed", "Adventure", "Luxury", "Budget", "Party", "Family",
          "Romantic", "Culture", "Food-focused"]
INTERESTS = ["Beaches", "Mountains", "Nature", "Food", "Nightlife", "Shopping",
             "History", "Culture", "Adventure", "Photography", "Cafes",
             "Wellness", "Sports", "Local experiences"]
PACES = ["Relaxed", "Balanced", "Packed"]
WAKE = ["Early", "Normal", "Late"]
TRANSPORT = ["Mixed", "Public transport", "Taxi", "Rental car"]
FOOD = ["Mixed", "Local", "Street food", "Vegetarian", "Vegan", "Fine dining"]
FLEXIBILITY = ["Strict", "Somewhat flexible", "Flexible"]

# Map user-facing choices to activity categories used in the data below.
INTEREST_TO_CAT = {
    "Beaches": "Nature", "Mountains": "Nature", "Nature": "Nature",
    "Food": "Food", "Cafes": "Food", "Nightlife": "Nightlife",
    "Shopping": "Shopping", "History": "Culture", "Culture": "Culture",
    "Local experiences": "Culture", "Adventure": "Adventure",
    "Sports": "Adventure", "Photography": "Nature", "Wellness": "Relaxation",
}
STYLE_TO_CAT = {
    "Relaxed": "Relaxation", "Adventure": "Adventure", "Luxury": "Relaxation",
    "Party": "Nightlife", "Family": "Nature", "Romantic": "Relaxation",
    "Culture": "Culture", "Food-focused": "Food",
}

# Rough per-person meal cost in INR at a food multiplier of 1.0 (lunch).
MEAL_BASE = {"Street food": 250, "Local": 400, "Mixed": 500, "Vegetarian": 400,
             "Vegan": 450, "Fine dining": 1500}
MEAL_FACTOR = {"Breakfast": 0.6, "Lunch": 1.0, "Dinner": 1.4}

# Each activity: (name, category, price_inr_per_person, hours, slot)
DESTINATIONS = {
    "Goa": {
        "country": "India", "vibe": "Beaches, food and nightlife",
        "base3": 4500, "intercity": 6000, "local_day": 500, "food_mult": 1.0,
        "cuisines": ["Goan", "Seafood", "North Indian", "Cafe"],
        "activities": [
            ("Beach morning and swim", "Nature", 0, 3, "morning"),
            ("Old-town heritage walk", "Culture", 600, 2.5, "morning"),
            ("Spice plantation visit", "Nature", 900, 3, "morning"),
            ("Water sports session", "Adventure", 1800, 2, "afternoon"),
            ("Local seafood food trail", "Food", 1200, 2.5, "afternoon"),
            ("Spa and wellness hour", "Relaxation", 2000, 2, "afternoon"),
            ("Sunset at a beachfront spot", "Nature", 300, 1.5, "evening"),
            ("Beach club night", "Nightlife", 1500, 4, "evening"),
            ("Night market stroll", "Shopping", 500, 2, "evening"),
        ],
    },
    "Manali": {
        "country": "India", "vibe": "Mountains, adventure and cafes",
        "base3": 3500, "intercity": 5000, "local_day": 600, "food_mult": 0.9,
        "cuisines": ["Himachali", "Tibetan", "North Indian", "Cafe"],
        "activities": [
            ("Valley viewpoint day trip", "Nature", 1500, 4, "morning"),
            ("Paragliding experience", "Adventure", 3000, 1.5, "morning"),
            ("Old-town temple and village walk", "Culture", 300, 2.5, "morning"),
            ("River rafting session", "Adventure", 1500, 2, "afternoon"),
            ("Local Himachali food trail", "Food", 900, 2.5, "afternoon"),
            ("Hot spring and spa break", "Relaxation", 1500, 2, "afternoon"),
            ("Market shopping stroll", "Shopping", 500, 2, "afternoon"),
            ("Cafe-hopping evening", "Food", 700, 2, "evening"),
            ("Bonfire evening", "Nightlife", 800, 3, "evening"),
        ],
    },
    "Jaipur": {
        "country": "India", "vibe": "Forts, bazaars and street food",
        "base3": 3500, "intercity": 4500, "local_day": 500, "food_mult": 0.9,
        "cuisines": ["Rajasthani", "Street food", "North Indian", "Cafe"],
        "activities": [
            ("Fort and palace visit", "Culture", 1000, 3, "morning"),
            ("Craft workshop (block printing)", "Culture", 1200, 2, "morning"),
            ("Hot-air balloon ride", "Adventure", 12000, 1.5, "morning"),
            ("Old-city bazaar walk", "Shopping", 500, 2.5, "afternoon"),
            ("Street food trail", "Food", 900, 2.5, "afternoon"),
            ("Spa hour", "Relaxation", 2000, 2, "afternoon"),
            ("Sunset viewpoint", "Nature", 200, 1.5, "evening"),
            ("Cultural dinner and folk show", "Culture", 1800, 3, "evening"),
            ("Rooftop evening", "Nightlife", 1500, 3, "evening"),
        ],
    },
    "Bali": {
        "country": "Indonesia", "vibe": "Surf, temples and wellness",
        "base3": 5500, "intercity": 35000, "local_day": 900, "food_mult": 1.3,
        "cuisines": ["Balinese", "Seafood", "Vegetarian cafe", "Western"],
        "activities": [
            ("Rice terrace morning walk", "Nature", 1200, 3, "morning"),
            ("Temple visit", "Culture", 1000, 2.5, "morning"),
            ("Surf lesson", "Adventure", 3000, 2, "morning"),
            ("Waterfall trek", "Adventure", 2500, 3, "afternoon"),
            ("Cooking class", "Food", 3500, 3, "afternoon"),
            ("Spa and massage", "Relaxation", 2500, 2, "afternoon"),
            ("Sunset beach walk", "Nature", 0, 1.5, "evening"),
            ("Beach club sunset", "Nightlife", 2500, 3, "evening"),
            ("Night market visit", "Shopping", 600, 2, "evening"),
        ],
    },
    "Dubai": {
        "country": "UAE", "vibe": "Skyline, desert and shopping",
        "base3": 11000, "intercity": 30000, "local_day": 2000, "food_mult": 2.5,
        "cuisines": ["Arabic", "Indian", "Seafood", "Fine dining"],
        "activities": [
            ("Old-town and souk walk", "Culture", 1500, 3, "morning"),
            ("Beach morning", "Nature", 0, 3, "morning"),
            ("Desert safari", "Adventure", 6000, 5, "afternoon"),
            ("Food tour", "Food", 5000, 3, "afternoon"),
            ("Mall and shopping afternoon", "Shopping", 0, 3, "afternoon"),
            ("Spa hour", "Relaxation", 6000, 2, "afternoon"),
            ("Observation deck visit", "Culture", 4500, 1.5, "evening"),
            ("Marina evening walk", "Nature", 0, 2, "evening"),
            ("Rooftop lounge evening", "Nightlife", 5000, 3, "evening"),
        ],
    },
    "Tokyo": {
        "country": "Japan", "vibe": "Food, culture and neon nights",
        "base3": 12000, "intercity": 55000, "local_day": 1800, "food_mult": 2.8,
        "cuisines": ["Sushi", "Ramen", "Izakaya", "Cafe"],
        "activities": [
            ("Shrine and old-district walk", "Culture", 0, 3, "morning"),
            ("Fish market breakfast trail", "Food", 3000, 2.5, "morning"),
            ("Day trip to a nearby mountain town", "Nature", 6500, 6, "morning"),
            ("Museum visit", "Culture", 1800, 2.5, "afternoon"),
            ("Neighbourhood shopping stroll", "Shopping", 0, 3, "afternoon"),
            ("Cooking or sushi class", "Food", 7000, 3, "afternoon"),
            ("Bath house visit", "Relaxation", 2500, 2, "afternoon"),
            ("Izakaya night", "Nightlife", 4000, 3, "evening"),
            ("City viewpoint at dusk", "Nature", 1800, 1.5, "evening"),
            ("Arcade evening", "Adventure", 2500, 3, "evening"),
        ],
    },
    "Paris": {
        "country": "France", "vibe": "Museums, cafes and romance",
        "base3": 15000, "intercity": 60000, "local_day": 2500, "food_mult": 3.5,
        "cuisines": ["Bistro", "Bakery", "Creperie", "Fine dining"],
        "activities": [
            ("Museum morning", "Culture", 2200, 3, "morning"),
            ("Riverside walk", "Nature", 0, 2, "morning"),
            ("Bakery and cafe trail", "Food", 3000, 2.5, "morning"),
            ("Day trip to a nearby palace town", "Culture", 6000, 6, "morning"),
            ("Historic quarter walk", "Culture", 0, 3, "afternoon"),
            ("Cooking class", "Food", 9000, 3, "afternoon"),
            ("Market and boutique shopping", "Shopping", 0, 3, "afternoon"),
            ("Garden picnic", "Relaxation", 1500, 2, "afternoon"),
            ("Bike tour", "Adventure", 3000, 3, "afternoon"),
            ("Evening river cruise", "Nature", 1800, 1.5, "evening"),
            ("Jazz or wine bar evening", "Nightlife", 4500, 3, "evening"),
        ],
    },
}


# ======================================================================
# 2. PLANNER (demo + optional OpenAI)
# ======================================================================
try:  # optional: load OPENAI_API_KEY from a .env file
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class PlannerError(Exception):
    """Friendly, user-safe error message."""


# ---------------------------------------------------------------- helpers
def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def fmt_time(minutes):
    minutes = int(minutes) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def travelers_of(prefs):
    return prefs["adults"] + prefs["children"]


def budget_per_person(prefs):
    amount = float(prefs["budget_amount"])
    return amount if prefs["budget_basis"] == "per person" else amount / travelers_of(prefs)


def validate_prefs(p):
    """Returns a list of (field, message). Empty list means valid."""
    errs = []
    if p.get("destination") not in DESTINATIONS:
        errs.append(("destination", "Please choose a destination."))
    try:
        start, end = parse_date(p["start_date"]), parse_date(p["end_date"])
        if start < date.today():
            errs.append(("dates", "The start date can't be in the past."))
        if end < start:
            errs.append(("dates", "The end date must be on or after the start date."))
        elif (end - start).days > 30:
            errs.append(("dates", "Trips longer than 30 days aren't supported yet."))
    except (KeyError, ValueError, TypeError):
        errs.append(("dates", "Please choose valid dates."))
    if p.get("adults", 0) < 1:
        errs.append(("travelers", "At least one adult is required."))
    try:
        if float(p.get("budget_amount", 0)) <= 0:
            errs.append(("budget", "Budget must be greater than zero."))
    except (TypeError, ValueError):
        errs.append(("budget", "Please enter a valid budget."))
    if p.get("currency") not in CURRENCY_RATES:
        errs.append(("budget", "Unsupported currency."))
    return errs


# --------------------------------------------------------- mock "services"
def inventory(destination, currency):
    """Mock hotel / activity / restaurant service. All demo data."""
    d, rate = DESTINATIONS[destination], CURRENCY_RATES[currency]
    hotels = [{"name": f"Demo {t} stay", "tier": t,
               "price_per_night": round(d["base3"] * TIER_MULT[t] * rate),
               "source": "demo"} for t in TIERS]
    activities = [{"name": a[0], "category": a[1], "price": round(a[2] * rate),
                   "hours": a[3], "slot": a[4], "source": "demo"} for a in d["activities"]]
    restaurants = [{"name": f"Demo {c} Kitchen", "cuisine": c,
                    "price": round(MEAL_BASE["Mixed"] * d["food_mult"] * rate),
                    "source": "demo"} for c in d["cuisines"]]
    return {"hotels": hotels, "activities": activities, "restaurants": restaurants}


# ------------------------------------------------------------------ budget
def compute_budget(trip):
    """Per-person budget by category, recomputed from the itinerary itself."""
    p, rate = trip["prefs"], trip["rate"]
    n = travelers_of(p)
    nights = trip["summary"]["nights"]
    rooms = math.ceil(n / 2)
    accommodation = trip["hotel"]["price_per_night"] * nights * rooms / n
    food = activities = 0.0
    for day in trip["days"]:
        for it in day["items"]:
            if it["type"] == "meal":
                food += it["cost"]
            elif it["type"] in ("activity", "transfer"):
                activities += it["cost"]
    local = trip["local_cost_pp_day"] * trip["summary"]["days"]
    shopping = 2000 * rate if "Shopping" in p.get("interests", []) else 0
    subtotal = (accommodation + trip["transport_cost_pp"] + food + activities
                + local + shopping)
    misc = subtotal * 0.04
    b = {"Accommodation": accommodation, "Transport": trip["transport_cost_pp"],
         "Food": food, "Activities": activities, "Local transport": local,
         "Shopping": shopping, "Miscellaneous": misc}
    return {k: round(v) for k, v in b.items()}


def refresh(trip):
    trip["budget"] = compute_budget(trip)
    trip["total_pp"] = sum(trip["budget"].values())
    return trip


# ------------------------------------------------------------ demo planner
def _wake_minutes(p):
    base = {"Early": 450, "Normal": 540, "Late": 630}.get(p.get("wake"), 540)
    m = re.search(r"(?:before|till|until|after)\s*(\d{1,2})\s*(?:am)?",
                  (p.get("notes") or "").lower())
    if m and 5 <= int(m.group(1)) <= 11:
        base = max(base, int(m.group(1)) * 60 + 30)
    return base


def _wanted_categories(p):
    cats = [INTEREST_TO_CAT[i] for i in p.get("interests", []) if i in INTEREST_TO_CAT]
    cats += [STYLE_TO_CAT[s] for s in p.get("styles", []) if s in STYLE_TO_CAT]
    return cats or ["Nature", "Food", "Culture"]


def _pick(pool, wanted, slot, used):
    cands = [a for a in pool if a[4] == slot and a[0] not in used]
    if not cands:
        cands = [a for a in pool if a[0] not in used]
    if not cands:
        used.clear()
        cands = [a for a in pool if a[4] == slot] or list(pool)
    cands.sort(key=lambda a: a[1] not in wanted)  # stable: preferred first
    used.add(cands[0][0])
    return cands[0]


def _tier_for(p, budget_pp, dest, rate):
    choice = p.get("accommodation")
    if choice in TIERS:
        return choice
    best = TIERS[0]  # auto-pick: hotel share <= ~40% of budget
    nights = max((parse_date(p["end_date"]) - parse_date(p["start_date"])).days, 1)
    rooms_share = math.ceil(travelers_of(p) / 2) / travelers_of(p)
    for t in TIERS:
        if dest["base3"] * TIER_MULT[t] * rate * nights * rooms_share <= budget_pp * 0.4:
            best = t
    return best


def _activity_item(a, rate, start):
    return {"time": fmt_time(start), "type": "activity", "name": a[0],
            "description": f"{a[1]} · demo activity, estimated price",
            "category": a[1], "duration_h": a[3], "cost": round(a[2] * rate)}


def _meal(kind, start, p, dest, rate, i):
    cuisine = dest["cuisines"][i % len(dest["cuisines"])]
    base = MEAL_BASE.get(p.get("food"), 500)
    cost = round(base * MEAL_FACTOR[kind] * dest["food_mult"] * rate)
    return {"time": fmt_time(start), "type": "meal", "name": kind,
            "description": f"Suggested style: {cuisine} (demo: Demo {cuisine} Kitchen)",
            "duration_h": 1, "cost": cost}


def _free(start, label="Free time: rest, pool or wander"):
    return {"time": fmt_time(start), "type": "free", "name": label,
            "description": "Unplanned on purpose", "duration_h": 2, "cost": 0}


def demo_generate(p):
    dest = DESTINATIONS[p["destination"]]
    cur, rate = p["currency"], CURRENCY_RATES[p["currency"]]
    start, end = parse_date(p["start_date"]), parse_date(p["end_date"])
    nights = (end - start).days
    ndays = nights + 1
    wake = _wake_minutes(p)
    wanted = _wanted_categories(p)
    notes = (p.get("notes") or "").lower()
    free_pm = "free" in notes and "afternoon" in notes
    slots = {"Relaxed": ["evening"], "Balanced": ["morning", "afternoon"],
             "Packed": ["morning", "afternoon", "evening"]}.get(p.get("pace"), ["morning", "evening"])
    if free_pm:
        slots = [s for s in slots if s != "afternoon"]
    tier = _tier_for(p, budget_per_person(p), dest, rate)
    used, days, mi = set(), [], 0

    for d in range(ndays):
        items, cursor = [], wake
        first, last = d == 0 and ndays > 1, d == ndays - 1 and ndays > 1
        if first:
            items.append({"time": "11:00", "type": "transfer",
                          "name": "Arrive and transfer to your stay",
                          "description": "Check in and freshen up", "duration_h": 2, "cost": 0})
            cursor = 13 * 60
        else:
            items.append(_meal("Breakfast", cursor, p, dest, rate, mi)); mi += 1
            cursor += 90
        if last:
            items.append({"time": fmt_time(cursor), "type": "transfer",
                          "name": "Check out and depart", "description": "Head home",
                          "duration_h": 2, "cost": 0})
            title = "Last morning and departure"
        else:
            title = "Arrival and settling in" if first else None
            if "morning" in slots and not first:
                a = _pick(dest["activities"], wanted, "morning", used)
                items.append(_activity_item(a, rate, cursor)); cursor += int(a[3] * 60)
                title = title or a[0]
            cursor = max(cursor + 15, 13 * 60)
            items.append(_meal("Lunch", cursor, p, dest, rate, mi)); mi += 1
            cursor = max(cursor + 90, 15 * 60 + 30)
            if "afternoon" in slots:
                a = _pick(dest["activities"], wanted, "afternoon", used)
                items.append(_activity_item(a, rate, cursor)); cursor += int(a[3] * 60)
                title = title or a[0]
            else:
                items.append(_free(cursor)); cursor += 120
            cursor = max(cursor + 30, 18 * 60 + 30)
            ev = _pick(dest["activities"], wanted, "evening", used) if ("evening" in slots or first) else None
            if ev:
                title = title or ev[0]
            if ev and ev[1] == "Nightlife":  # dinner first, then go out
                dinner_at = max(cursor, 19 * 60 + 30)
                items.append(_meal("Dinner", dinner_at, p, dest, rate, mi)); mi += 1
                items.append(_activity_item(ev, rate, dinner_at + 90))
            else:
                if ev:
                    items.append(_activity_item(ev, rate, cursor)); cursor += int(ev[3] * 60)
                items.append(_meal("Dinner", max(cursor + 15, 20 * 60 + 30), p, dest, rate, mi)); mi += 1
        days.append({"day": d + 1, "date": (start + timedelta(days=d)).isoformat(),
                     "title": title or "Explore at your pace", "items": items})

    trip = {
        "source": "demo", "currency": cur, "rate": rate, "prefs": copy.deepcopy(p),
        "summary": {"destination": p["destination"], "start_date": p["start_date"],
                    "end_date": p["end_date"], "nights": nights, "days": ndays,
                    "travelers": travelers_of(p), "styles": p.get("styles", []),
                    "overview": f"{ndays} days in {p['destination']}: {dest['vibe'].lower()}."},
        "hotel": {"name": f"Demo {tier} stay", "tier": tier,
                  "price_per_night": round(dest["base3"] * TIER_MULT[tier] * rate),
                  "location_note": "Demo placeholder, not a real property"},
        "transport_cost_pp": round(dest["intercity"] * rate),
        "local_cost_pp_day": round(dest["local_day"] * rate),
        "days": days,
        "notes": ["Demo itinerary: hotels, activities and prices are illustrative estimates, not live data.",
                  "Nothing here is booked. Check real prices and availability before you travel."],
    }
    refresh(trip)
    if p.get("flexibility") == "Strict":
        target = budget_per_person(p)
        for _ in range(12):
            if trip["total_pp"] <= target or not _cheapen_step(trip):
                break
    return trip


# --------------------------------------------------- demo customization
def _cheapen_step(trip):
    """One cost-cutting step. Returns a description, or None if nothing left."""
    tier = trip["hotel"]["tier"]
    if TIERS.index(tier) > 0:
        new = TIERS[TIERS.index(tier) - 1]
        dest = DESTINATIONS[trip["summary"]["destination"]]
        trip["hotel"].update(tier=new, name=f"Demo {new} stay",
                             price_per_night=round(dest["base3"] * TIER_MULT[new] * trip["rate"]))
        refresh(trip)
        return f"Hotel moved down to {new}"
    paid = [(it, day) for day in trip["days"] for it in day["items"]
            if it["type"] == "activity" and it["cost"] > 0]
    if not paid:
        return None
    it, day = max(paid, key=lambda x: x[0]["cost"])
    old = it["name"]
    it.update(type="free", name="Free time: beach, walk or local wander",
              description=f"Replaced '{old}' to save money", cost=0, duration_h=2)
    refresh(trip)
    return f"Replaced '{old}' on Day {day['day']} with free time"


def _day_no(text, ndays):
    m = re.search(r"day\s*(\d+)", text)
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= ndays else None


def _add_activity(trip, day, category, slot="evening"):
    dest = DESTINATIONS[trip["summary"]["destination"]]
    have = {it["name"] for it in day["items"]}
    pool = [a for a in dest["activities"] if a[1] == category and a[0] not in have]
    if not pool:
        return None
    a = next((x for x in pool if x[4] == slot), pool[0])
    start = {"morning": 10 * 60 + 30, "afternoon": 15 * 60 + 30, "evening": 21 * 60}[a[4]]
    while any(it["time"] == fmt_time(start) for it in day["items"]):
        start += 90
    item = _activity_item(a, trip["rate"], start)
    free_idx = next((i for i, it in enumerate(day["items"])
                     if it["type"] == "free" and a[4] == "afternoon"), None)
    if free_idx is not None:  # an afternoon activity takes over free time
        day["items"][free_idx] = item
    else:
        day["items"].append(item)
    day["items"].sort(key=lambda it: it["time"])
    return a[0]


def demo_modify(trip, request):
    t = copy.deepcopy(trip)
    text, changes = request.lower().strip(), []
    ndays = t["summary"]["days"]
    dn = _day_no(text, ndays)
    target_days = [t["days"][dn - 1]] if dn else t["days"]
    before = t["total_pp"]

    m = re.search(r"under\s*[₹$€£]?\s*([\d,]+)", text)
    if m:
        target = float(m.group(1).replace(",", ""))
        if t["prefs"]["budget_basis"] == "total":
            target /= travelers_of(t["prefs"])
        for _ in range(15):
            if t["total_pp"] <= target:
                break
            c = _cheapen_step(t)
            if not c:
                break
            changes.append(c)
        if t["total_pp"] > target:
            changes.append("Couldn't reach that budget with the demo planner; showing the cheapest version")
    elif re.search(r"cheap|save|less expensive|expensive", text):
        for _ in range(2):
            c = _cheapen_step(t)
            if c:
                changes.append(c)

    if "relax" in text:
        for day in target_days:
            acts = [it for it in day["items"] if it["type"] == "activity"]
            if len(acts) > 1:
                it = acts[-1] if acts[-1]["time"] > "17:00" and len(acts) > 2 else acts[0]
                old = it["name"]
                it.update(type="free", name="Free time: rest and recharge", cost=0,
                          description=f"Replaced '{old}' for a slower pace", duration_h=2)
                changes.append(f"Day {day['day']}: replaced '{old}' with free time")

    m = re.search(r"remove (?:the )?([a-z' \-]+)", text)
    if m:
        words = [w for w in m.group(1).split() if len(w) > 3 and w not in ("expensive", "activities")]
        removed_before = len(changes)
        for day in target_days:
            for it in day["items"]:
                if it["type"] == "activity" and any(w in it["name"].lower() for w in words):
                    old = it["name"]
                    it.update(type="free", name="Free time", cost=0,
                              description=f"Removed '{old}' as requested", duration_h=2)
                    changes.append(f"Day {day['day']}: removed '{old}'")
        if words and len(changes) == removed_before and not changes:
            raise PlannerError(f"I couldn't find '{' '.join(words)}' in this itinerary.")

    if re.search(r"free (afternoon|time)", text):
        days = target_days if dn else [t["days"][min(1, ndays - 1)]]
        for day in days:
            done = False
            for it in day["items"]:
                if it["type"] == "activity" and "12:00" < it["time"] < "18:00":
                    old = it["name"]
                    it.update(type="free", name="Free afternoon", cost=0, duration_h=3,
                              description=f"Kept open (was '{old}')")
                    changes.append(f"Day {day['day']}: free afternoon (removed '{old}')")
                    done = True
            if not done:
                changes.append(f"Day {day['day']}: afternoon was already free")

    if re.search(r"nightlife|party|\bbar\b|club", text):
        days = target_days if dn else [d for d in t["days"][:-1]][:3]
        for day in days:
            name = _add_activity(t, day, "Nightlife")
            if name:
                changes.append(f"Day {day['day']}: added '{name}'")

    if "adventur" in text:
        days = target_days if dn else t["days"][1:2] or t["days"]
        for day in days:
            name = _add_activity(t, day, "Adventure", "afternoon")
            if name:
                changes.append(f"Day {day['day']}: added '{name}'")

    if "food" in text and "remove" not in text:
        for day in (target_days if dn else t["days"][:-1][:3]):
            name = _add_activity(t, day, "Food", "afternoon")
            if name:
                changes.append(f"Day {day['day']}: added '{name}'")

    if "hotel" in text:
        if re.search(r"beach|closer|near", text):
            t["hotel"]["location_note"] = "Beach-side area (demo placeholder)"
            changes.append("Hotel: prefer a beach-side location (demo placeholder)")
        else:
            i = TIERS.index(t["hotel"]["tier"])
            new = TIERS[i + 1] if i + 1 < len(TIERS) and "upgrade" in text else TIERS[max(i - 1, 0)]
            dest = DESTINATIONS[t["summary"]["destination"]]
            t["hotel"].update(tier=new, name=f"Demo {new} stay",
                              price_per_night=round(dest["base3"] * TIER_MULT[new] * t["rate"]))
            changes.append(f"Hotel changed to {new}")

    if not changes:
        raise PlannerError(
            "Demo mode understands requests like 'make it cheaper', 'make it more relaxed', "
            "'add nightlife', 'remove the fort', 'give me a free afternoon', 'make Day 2 "
            "adventurous' or 'keep it under 20000'. Add an OpenAI API key for free-form edits.")
    refresh(t)
    return {"trip": t, "changes": changes, "before": before, "after": t["total_pp"]}


# ------------------------------------------------------------ AI planner
SCHEMA_HINT = """{
  "summary": {"destination": str, "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD",
              "nights": int, "days": int, "travelers": int, "styles": [str], "overview": str},
  "hotel": {"name": str, "tier": str, "price_per_night": number, "location_note": str},
  "transport_cost_pp": number, "local_cost_pp_day": number,
  "days": [{"day": int, "date": "YYYY-MM-DD", "title": str,
            "items": [{"time": "HH:MM", "type": "activity|meal|transfer|free",
                       "name": str, "description": str, "duration_h": number,
                       "cost": number}]}],
  "notes": [str]
}"""

SYSTEM_PROMPT = f"""You are the planning engine of TripPilot, a travel planner.
Return ONLY one JSON object matching this schema:
{SCHEMA_HINT}
Rules:
- All money values are PER PERSON in the requested currency; hotel price_per_night is per room.
- Never claim anything is booked. Never state live prices. Prices are estimates.
- Do not invent precise street addresses or coordinates.
- Respect every stated preference (wake-up time, pace, food, budget flexibility).
- Include breakfast, lunch and dinner as type "meal" items each day.
"""


def _client():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    return OpenAI(api_key=key)


def ai_available():
    return _client() is not None


def _call_ai(user_payload):
    client = _client()
    try:
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
            timeout=90)
        return json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        raise PlannerError("The AI returned an unreadable answer. Please try again.")
    except Exception:  # network, auth, quota... never leak details
        raise PlannerError("The AI service isn't reachable right now. Please try again, "
                           "or use the demo planner.")


def validate_trip(t, expected_days):
    try:
        assert isinstance(t["summary"]["destination"], str)
        assert isinstance(t["hotel"]["name"], str)
        float(t["hotel"]["price_per_night"]); float(t["transport_cost_pp"])
        float(t["local_cost_pp_day"])
        assert len(t["days"]) == expected_days
        for day in t["days"]:
            assert day["items"], "empty day"
            for it in day["items"]:
                assert isinstance(it["name"], str) and isinstance(it["time"], str)
                it["cost"] = float(it["cost"])
                it.setdefault("description", ""); it.setdefault("duration_h", 1)
                if it.get("type") not in ("activity", "meal", "transfer", "free"):
                    it["type"] = "activity"
        t.setdefault("notes", [])
    except (KeyError, TypeError, ValueError, AssertionError):
        raise PlannerError("The AI's plan didn't pass our checks. Please try again.")
    return t


def _finish_ai_trip(t, prefs, expected_days):
    t = validate_trip(t, expected_days)
    t.update(source="ai", currency=prefs["currency"],
             rate=CURRENCY_RATES[prefs["currency"]], prefs=copy.deepcopy(prefs))
    t["hotel"]["price_per_night"] = round(float(t["hotel"]["price_per_night"]))
    t["hotel"].setdefault("tier", "3-star"); t["hotel"].setdefault("location_note", "")
    t["summary"]["nights"] = expected_days - 1
    t["summary"]["days"] = expected_days
    t["summary"]["travelers"] = travelers_of(prefs)
    t["notes"] = list(t["notes"]) + [
        "AI-generated plan with estimated prices. Not verified or live data. Nothing is booked."]
    return refresh(t)


def ai_generate(p):
    start, end = parse_date(p["start_date"]), parse_date(p["end_date"])
    ndays = (end - start).days + 1
    payload = {"task": "Create a trip", "preferences": p,
               "budget_per_person": round(budget_per_person(p)), "num_days": ndays}
    return _finish_ai_trip(_call_ai(payload), p, ndays)


def ai_modify(trip, request):
    base = {k: trip[k] for k in ("summary", "hotel", "transport_cost_pp",
                                 "local_cost_pp_day", "days", "notes")}
    payload = {"task": "Modify the trip. Change ONLY what the request needs and keep "
                       "everything else identical. Also add a top-level key "
                       "'changes': [short strings describing what you changed].",
               "request": request, "original_preferences": trip["prefs"],
               "current_trip": base}
    raw = _call_ai(payload)
    changes = [str(c) for c in raw.pop("changes", [])] or ["Trip updated"]
    new = _finish_ai_trip(raw, trip["prefs"], trip["summary"]["days"])
    return {"trip": new, "changes": changes, "before": trip["total_pp"],
            "after": new["total_pp"]}


# ---------------------------------------------------------- public API
def generate_trip(prefs, force_demo=False):
    errs = validate_prefs(prefs)
    if errs:
        raise PlannerError(errs[0][1])
    trip = ai_generate(prefs) if (not force_demo and ai_available()) else demo_generate(prefs)
    return osm_places.enrich_with_osm(trip)


def modify_trip(trip, request, force_demo=False):
    if not request.strip():
        raise PlannerError("Tell us what you'd like to change.")
    result = (ai_modify(trip, request) if (not force_demo and ai_available())
              else demo_modify(trip, request))
    result["trip"] = osm_places.enrich_with_osm(result["trip"])
    return result


# ======================================================================
# 3. STORAGE (local SQLite)
# ======================================================================
DB_PATH = Path(__file__).parent / "db" / "trippilot.db"


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS trips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        share_id TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        destination TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    return c


def save_trip(trip, name=None, trip_id=None):
    """Insert a new trip, or update trip_id if given. Returns the trip id."""
    data = json.dumps(trip)
    dest = trip["summary"]["destination"]
    with closing(_conn()) as c, c:
        if trip_id:
            c.execute("UPDATE trips SET data=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (data, trip_id))
            return trip_id
        title = name or f"{dest} · {trip['summary']['start_date']}"
        cur = c.execute("INSERT INTO trips (share_id, name, destination, data) VALUES (?,?,?,?)",
                        (secrets.token_urlsafe(8), title, dest, data))
        return cur.lastrowid


def list_trips():
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute(
            "SELECT id, share_id, name, destination, data, updated_at FROM trips "
            "ORDER BY updated_at DESC, id DESC")]


def get_trip(trip_id):
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    return _row(r)


def get_by_share(share_id):
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM trips WHERE share_id=?", (share_id,)).fetchone()
    return _row(r)


def _row(r):
    if not r:
        return None
    d = dict(r)
    d["trip"] = json.loads(d.pop("data"))
    return d


def rename_trip(trip_id, name):
    with closing(_conn()) as c, c:
        c.execute("UPDATE trips SET name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                  (name.strip() or "Untitled trip", trip_id))


def delete_trip(trip_id):
    with closing(_conn()) as c, c:
        c.execute("DELETE FROM trips WHERE id=?", (trip_id,))


def duplicate_trip(trip_id):
    row = get_trip(trip_id)
    if not row:
        return None
    return save_trip(row["trip"], name=f"{row['name']} (copy)")


# ======================================================================
# 4. USER INTERFACE
# ======================================================================
STEPS = ["Destination", "Dates", "Travelers", "Budget", "Style", "Interests", "Preferences"]
STEP_FIELDS = {0: ["destination"], 1: ["dates"], 2: ["travelers"], 3: ["budget"]}
SUGGESTIONS = ["Make it cheaper", "Make it more relaxed", "Add nightlife",
               "Remove expensive activities", "Add more food experiences",
               "Give me a free afternoon", "Change my hotel", "Make Day 2 adventurous"]


# ---------------------------------------------------------------- helpers
def money(amount, cur):
    return f"{CURRENCY_SYMBOLS.get(cur, '')}{amount:,.0f}"


def default_prefs(destination="Goa"):
    start = date.today() + timedelta(days=30)
    return {"destination": destination, "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=3)).isoformat(),
            "travel_type": "Friends", "adults": 3, "children": 0,
            "budget_amount": 25000.0, "budget_basis": "per person", "currency": "INR",
            "flexibility": "Somewhat flexible", "styles": ["Relaxed"],
            "interests": ["Beaches", "Food", "Nightlife"], "pace": "Relaxed",
            "wake": "Normal", "accommodation": "No preference", "transport": "Mixed",
            "food": "Mixed", "notes": ""}


def init_state():
    ss = st.session_state
    ss.setdefault("page", "home")
    ss.setdefault("step", 0)
    ss.setdefault("prefs", default_prefs())
    ss.setdefault("trip", None)
    ss.setdefault("trip_id", None)
    ss.setdefault("last_change", None)
    ss.setdefault("build_error", None)
    ss.setdefault("change_error", None)


def go(page):
    st.session_state.page = page


def start_plan(destination=None):
    ss = st.session_state
    ss.prefs = default_prefs(destination or "Goa")
    ss.step, ss.page, ss.build_error = 0, "plan", None


def edit_prefs():
    ss = st.session_state
    ss.prefs = copy.deepcopy(ss.trip["prefs"])
    ss.step, ss.page = 0, "plan"


def open_saved(trip_id):
    row = get_trip(trip_id)
    if row:
        ss = st.session_state
        ss.trip, ss.trip_id, ss.last_change, ss.page = row["trip"], trip_id, None, "trip"


def trip_markdown(trip):
    s, cur = trip["summary"], trip["currency"]
    lines = [f"# {s['destination']}: {s['start_date']} to {s['end_date']}",
             f"{s['days']} days · {s['travelers']} travelers · "
             f"about {money(trip['total_pp'], cur)} per person (estimate)", ""]
    lines += [f"_{n}_" for n in trip["notes"]] + [""]
    for day in trip["days"]:
        lines.append(f"## Day {day['day']}: {day['title']} ({day['date']})")
        for it in day["items"]:
            lines.append(f"- {it['time']} {it['name']} ({money(it['cost'], cur)} pp)")
        lines.append("")
    lines.append("## Budget (per person)")
    lines += [f"- {k}: {money(v, cur)}" for k, v in trip["budget"].items()]
    return "\n".join(lines)


# ----------------------------------------------------------------- pages
def page_home():
    st.markdown("""<div class="hero"><h1>Plan a trip that actually feels like you.</h1>
    <p>Tell TripPilot where you want to go, how you like to travel, and what you want to
    spend. AI builds a personalized trip around you.</p></div>""", unsafe_allow_html=True)
    c1, c2, _ = st.columns([1, 1, 3])
    c1.button("Plan my trip", type="primary", on_click=start_plan, use_container_width=True)
    c2.button("How it works", on_click=go, args=("how",), use_container_width=True)
    st.caption("Your trip. Your way.")
    st.subheader("Why TripPilot")
    cols = st.columns(3)
    feats = [("Personalized itineraries", "Built around your pace, wake-up time and interests."),
             ("Budget-aware planning", "See where every rupee goes, per person."),
             ("Change anything in plain English", "“Make it cheaper”, “remove the fort”, “free afternoon”.")]
    for col, (t, d) in zip(cols, feats):
        col.markdown(f"**{t}**")
        col.write(d)


def page_how():
    st.header("How it works")
    for i, (t, d) in enumerate([
            ("Tell us what you want", "A short 7-step flow: destination, dates, group, budget, style, interests, preferences."),
            ("AI builds your trip", "You get a day-by-day plan with a budget breakdown."),
            ("Customize anything", "Type a change in plain English and only the relevant parts update."),
            ("Travel with confidence", "Save it, download it, or share a read-only link.")], 1):
        st.markdown(f"**{i}. {t}**  \n{d}")
    st.button("Plan my trip", type="primary", on_click=start_plan)


def page_explore():
    st.header("Explore destinations")
    st.caption("Demo destinations with illustrative data.")
    cols = st.columns(3)
    for i, (name, d) in enumerate(DESTINATIONS.items()):
        with cols[i % 3].container(border=True):
            st.subheader(name)
            st.write(f"{d['country']} · {d['vibe']}")
            st.button(f"Plan {name}", key=f"exp_{name}", on_click=start_plan, args=(name,))


def page_profile():
    st.header("Profile")
    st.info("Accounts aren't part of this local MVP yet. Trips are stored in a local "
            "SQLite file on this computer.")
    st.write("AI planning:", "✅ OpenAI key found" if ai_available()
             else "Demo planner (no OPENAI_API_KEY set)")


def page_my_trips():
    st.header("My trips")
    rows = list_trips()
    if not rows:
        st.info("No saved trips yet. Plan one and press Save.")
        st.button("+ Plan a new trip", type="primary", on_click=start_plan)
        return
    for r in rows:
        t = json.loads(r["data"])
        s = t["summary"]
        with st.container(border=True):
            c1, c2 = st.columns([3, 2])
            c1.subheader(r["name"])
            c1.caption(f"{s['destination']} · {s['start_date']} to {s['end_date']} · "
                       f"{s['days']} days · ~{money(t['total_pp'], t['currency'])}/person")
            new_name = c1.text_input("Rename", value=r["name"], key=f"rn_{r['id']}",
                                     label_visibility="collapsed")
            if new_name != r["name"]:
                rename_trip(r["id"], new_name)
                st.rerun()
            c2.button("Open", key=f"op_{r['id']}", on_click=open_saved, args=(r["id"],))
            if c2.button("Duplicate", key=f"du_{r['id']}"):
                duplicate_trip(r["id"])
                st.rerun()
            if c2.button("Delete", key=f"de_{r['id']}"):
                delete_trip(r["id"])
                st.rerun()


# ---------------------------------------------------------------- wizard
def run_build(force_demo=False):
    ss, p = st.session_state, st.session_state.prefs
    with st.status("Planning your trip…", expanded=True) as status:
        try:
            st.write("Checking your preferences…")
            errs = validate_prefs(p)
            if errs:
                raise PlannerError(errs[0][1])
            mode = "AI" if (ai_available() and not force_demo) else "demo planner"
            st.write(f"Building and validating your itinerary ({mode})…")
            ss.trip = generate_trip(p, force_demo=force_demo)
            ss.trip_id, ss.last_change, ss.build_error = None, None, None
            status.update(label="Your trip is ready", state="complete")
        except PlannerError as e:
            ss.build_error = str(e)
            status.update(label="Couldn't build the trip", state="error")
    if ss.trip and not ss.build_error:
        ss.page = "trip"
        st.rerun()


def page_plan():
    ss = st.session_state
    p, step = ss.prefs, ss.step
    st.header("Plan a trip")
    st.progress((step + 1) / len(STEPS),
                text=" → ".join(f"**{n}**" if i == step else n for i, n in enumerate(STEPS)))
    st.subheader({0: "Where do you want to go?", 1: "When are you traveling?",
                  2: "Who are you traveling with?", 3: "What's your budget?",
                  4: "What's your travel style?", 5: "What are you into?",
                  6: "Anything else we should know?"}[step])

    if step == 0:
        names = list(DESTINATIONS)
        p["destination"] = st.selectbox("Search destination", names,
                                        index=names.index(p["destination"]))
        st.caption(DESTINATIONS[p["destination"]]["vibe"] + " (demo destination)")
    elif step == 1:
        c1, c2 = st.columns(2)
        start = c1.date_input("Start date", value=parse_date(p["start_date"]),
                              min_value=date.today())
        end = c2.date_input("End date", value=max(parse_date(p["end_date"]), start),
                            min_value=start)
        p["start_date"], p["end_date"] = start.isoformat(), end.isoformat()
        nights = (end - start).days
        st.info(f"{nights} nights · {nights + 1} days")
    elif step == 2:
        p["travel_type"] = st.radio("Trip type", TRAVEL_TYPES, horizontal=True,
                                    index=TRAVEL_TYPES.index(p["travel_type"]))
        c1, c2 = st.columns(2)
        p["adults"] = int(c1.number_input("Adults", 1, 20, p["adults"]))
        p["children"] = int(c2.number_input("Children", 0, 20, p["children"]))
    elif step == 3:
        c1, c2 = st.columns(2)
        p["budget_basis"] = c1.radio("Budget is…", ["per person", "total"], horizontal=True,
                                     index=["per person", "total"].index(p["budget_basis"]))
        cur_list = list(CURRENCY_RATES)
        p["currency"] = c2.selectbox("Currency", cur_list, index=cur_list.index(p["currency"]))
        p["budget_amount"] = st.number_input(f"Amount ({CURRENCY_SYMBOLS[p['currency']]})",
                                             min_value=0.0, step=1000.0,
                                             value=float(p["budget_amount"]))
        p["flexibility"] = st.select_slider("How flexible is your budget?", FLEXIBILITY,
                                            value=p["flexibility"])
    elif step == 4:
        p["styles"] = st.multiselect("Pick one or more", STYLES, default=p["styles"])
    elif step == 5:
        p["interests"] = st.multiselect("Pick your interests", INTERESTS, default=p["interests"])
    else:
        p["notes"] = st.text_area(
            "In your own words", value=p["notes"], height=120,
            placeholder="Don't wake me up before 9 AM. I prefer local restaurants, hate long "
                        "drives, and want some free time every afternoon.")
        c1, c2, c3 = st.columns(3)
        p["pace"] = c1.radio("Pace", PACES, index=PACES.index(p["pace"]))
        p["wake"] = c2.radio("Wake-up", WAKE, index=WAKE.index(p["wake"]))
        acc = ["No preference"] + TIERS
        p["accommodation"] = c3.selectbox("Accommodation", acc, index=acc.index(p["accommodation"]))
        c4, c5 = st.columns(2)
        p["transport"] = c4.selectbox("Transport", TRANSPORT, index=TRANSPORT.index(p["transport"]))
        p["food"] = c5.selectbox("Food", FOOD, index=FOOD.index(p["food"]))

    if ss.build_error:
        st.error(ss.build_error)
        if ai_available() and st.button("Use the demo planner instead"):
            run_build(force_demo=True)

    back, nxt = st.columns(2)
    if step > 0 and back.button("← Back"):
        ss.step -= 1
        st.rerun()
    if step < len(STEPS) - 1:
        if nxt.button("Next →", type="primary"):
            errs = [m for f, m in validate_prefs(p) if f in STEP_FIELDS.get(step, [])]
            if errs:
                st.error(errs[0])
            else:
                ss.step += 1
                st.rerun()
    elif nxt.button("Build my trip", type="primary"):
        run_build()


# ------------------------------------------------------------- trip view
def run_change(request):
    ss = st.session_state
    try:
        result = modify_trip(ss.trip, request)
        ss.trip, ss.last_change, ss.change_error = result["trip"], result, None
        if ss.trip_id:
            save_trip(ss.trip, trip_id=ss.trip_id)
    except PlannerError as e:
        ss.change_error = str(e)


def select_hotel(tier):
    ss = st.session_state
    hotel = next(h for h in inventory(ss.trip["summary"]["destination"],
                                              ss.trip["currency"])["hotels"] if h["tier"] == tier)
    ss.trip["hotel"].update(name=hotel["name"], tier=tier, price_per_night=hotel["price_per_night"],
                            location_note="Demo placeholder, not a real property")
    refresh(ss.trip)
    if ss.trip_id:
        save_trip(ss.trip, trip_id=ss.trip_id)


def select_real_hotel(place):
    ss = st.session_state
    trip = ss.trip
    tier = osm_places.tier_for(place) or trip["hotel"]["tier"]
    dest = DESTINATIONS[trip["summary"]["destination"]]
    where = place.get("address") or "Real place from OpenStreetMap"
    trip["hotel"].update(
        name=place["name"], tier=tier,
        price_per_night=round(dest["base3"] * TIER_MULT.get(tier, 1.0) * trip["rate"]),
        location_note=f"{where} · price is an estimate for a {tier} stay",
        osm_url=place["url"], osm_name=place["name"], lat=place["lat"], lon=place["lon"])
    refresh(trip)
    if ss.trip_id:
        save_trip(trip, trip_id=ss.trip_id)


def page_trip(public=False, row=None):
    ss = st.session_state
    trip = row["trip"] if public else ss.trip
    if not trip:
        st.info("No trip yet.")
        st.button("Plan a trip", type="primary", on_click=start_plan)
        return
    s, cur = trip["summary"], trip["currency"]
    st.markdown(f"<span class='pill'>{'AI-generated' if trip['source'] == 'ai' else 'Demo data'}"
                "</span><span class='pill'>Estimates, nothing is booked</span>",
                unsafe_allow_html=True)
    st.title(s["destination"].upper())
    hero = osm_places.destination_image(s["destination"])
    if hero:
        st.image(hero, caption="Photo: Wikipedia / Wikimedia Commons")
    st.caption(f"{s['start_date']} → {s['end_date']} · {s['days']} days · {s['travelers']} travelers")
    m1, m2, m3 = st.columns(3)
    m1.metric("Estimated per person", money(trip["total_pp"], cur))
    m2.metric("Estimated group total", money(trip["total_pp"] * s["travelers"], cur))
    if not public:
        target = budget_per_person(trip["prefs"])
        m3.metric("Your budget (per person)", money(target, cur),
                  delta=money(target - trip["total_pp"], cur) + " left"
                  if target >= trip["total_pp"] else "-" + money(trip["total_pp"] - target, cur) + " over",
                  delta_color="normal")
    for n in trip["notes"]:
        st.caption("ℹ️ " + n)

    if not public:
        b1, b2, b3, b4, b5 = st.columns(5)
        if b1.button("Save", type="primary"):
            ss.trip_id = save_trip(trip, trip_id=ss.trip_id)
            st.toast("Trip saved")
        b2.button("Edit trip", on_click=edit_prefs)
        b3.download_button("Download", trip_markdown(trip), file_name=f"{s['destination']}-trip.md")
        if b4.button("Share"):
            ss.trip_id = save_trip(trip, trip_id=ss.trip_id)
            ss.show_share = True
        if b5.button("Book"):
            st.info("This booking integration isn't available yet.")
        if ss.get("show_share") and ss.trip_id:
            share_id = get_trip(ss.trip_id)["share_id"]
            st.success("Anyone with your app's address plus this suffix can view a read-only copy:")
            st.code(f"?share={share_id}")

        st.subheader("What would you like to change?")
        cols = st.columns(4)
        for i, sug in enumerate(SUGGESTIONS):
            if cols[i % 4].button(sug, key=f"sg{i}", use_container_width=True):
                run_change(sug)
        c1, c2 = st.columns([4, 1])
        req = c1.text_input("Describe a change", label_visibility="collapsed",
                            placeholder="e.g. Keep the total under 20000, or make Day 3 adventurous")
        if c2.button("Update", type="primary", use_container_width=True) and req.strip():
            run_change(req)
        if ss.change_error:
            st.warning(ss.change_error)
        lc = ss.last_change
        if lc:
            with st.container(border=True):
                st.markdown("**Trip updated**")
                for c in lc["changes"]:
                    st.write("• " + c)
                st.write(f"Budget: {money(lc['before'], cur)} → {money(lc['after'], cur)} per person")
        trip = ss.trip  # may have just changed

    st.subheader("Budget breakdown (per person)")
    df = pd.DataFrame({"Per person": {k: v for k, v in trip["budget"].items() if v > 0}})
    c1, c2 = st.columns([2, 3])
    c1.dataframe(df.assign(**{"Per person": df["Per person"].map(lambda v: money(v, cur))}))
    c2.bar_chart(df)

    st.subheader("Map")
    osm_places.render_map(trip)

    st.subheader("Day by day")
    for i, day in enumerate(trip["days"]):
        with st.expander(f"Day {day['day']} · {day['title']} · {day['date']}", expanded=i == 0):
            for it in day["items"]:
                cost = f" · {money(it['cost'], cur)} pp" if it["cost"] else ""
                st.markdown(f"**{it['time']}** {it['name']}{cost}")
                if it.get("description"):
                    st.caption(it["description"])

    inv = inventory(s["destination"], cur)
    st.subheader("Hotel")
    h = trip["hotel"]
    st.write(f"**{h['name']}** ({h['tier']}) · {money(h['price_per_night'], cur)} per room/night "
             f"· {h.get('location_note', '')}")
    if h.get("osm_url") and h.get("osm_name") == h["name"]:
        st.markdown(f"[View this hotel on OpenStreetMap]({h['osm_url']})")
    if not public:
        st.caption("Price tiers (estimates; OpenStreetMap has no live prices):")
        cols = st.columns(4)
        for i, opt in enumerate(inv["hotels"]):
            with cols[i % 4].container(border=True):
                st.write(f"**{opt['tier']}**")
                st.caption(f"{money(opt['price_per_night'], cur)}/night")
                st.button("Select", key=f"hs_{opt['tier']}", on_click=select_hotel, args=(opt["tier"],))

    # Real hotels, restaurants and sights from OpenStreetMap; demo lists are the fallback.
    if not osm_places.render_places(trip, cur, None if public else select_real_hotel):
        st.subheader("Activities and restaurants (demo suggestions)")
        a_col, r_col = st.columns(2)
        for a in inv["activities"]:
            a_col.markdown(f"**{a['name']}** · {a['category']} · {a['hours']}h · {money(a['price'], cur)}")
        for r in inv["restaurants"]:
            r_col.markdown(f"**{r['name']}** · {r['cuisine']} · ~{money(r['price'], cur)} pp")


# ------------------------------------------------------------------ main
def main():
    init_state()
    share_id = st.query_params.get("share")
    if share_id:
        row = get_by_share(share_id)
        if row:
            page_trip(public=True, row=row)
        else:
            st.error("This shared trip couldn't be found.")
        return

    with st.sidebar:
        st.markdown("## 🧭 TripPilot")
        st.caption("Your trip. Your way.")
        for label, key in [("Home", "home"), ("Plan a trip", "plan"), ("Current trip", "trip"),
                           ("My trips", "my_trips"), ("Explore", "explore"), ("Profile", "profile")]:
            if key == "plan":
                st.button(label, key="nav_plan", on_click=start_plan, use_container_width=True)
            else:
                st.button(label, key=f"nav_{key}", on_click=go, args=(key,), use_container_width=True)

    {"home": page_home, "how": page_how, "plan": page_plan, "trip": page_trip,
     "my_trips": page_my_trips, "explore": page_explore, "profile": page_profile
     }[st.session_state.page]()


if __name__ == "__main__":
    main()
