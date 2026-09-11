"""Deterministic schedule and budget arithmetic around optional LLM discovery."""
import asyncio
import calendar
import json
import math
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from .agents import agent_report, flight_search, rank_flights, surface_options
from .models import Brief, PlanRequest
from .providers import Providers, safely

CATALOG = json.loads((Path(__file__).resolve().parents[1] / "dist/catalog.json").read_text())
IATA = {"kolkata": "CCU", "delhi": "DEL", "new delhi": "DEL", "mumbai": "BOM", "bengaluru": "BLR",
        "bangalore": "BLR", "chennai": "MAA", "hyderabad": "HYD", "london": "LON", "new york": "NYC",
        **{p["name"].lower(): p["iata"] for p in CATALOG}}
CATEGORIES = ["beaches", "nature", "culture", "adventure", "food", "nightlife", "shopping"]


def round_inr(value: float) -> int:
    """Whole-rupee rounding, matching JavaScript Math.round for nonnegative costs."""
    return math.floor(value + 0.5)


def map_link(query: str) -> str:
    return "https://www.google.com/maps/search/?" + urlencode({"api": 1, "query": query})


def parse_prompt(prompt: str) -> dict:
    text = prompt.lower()
    if re.search(r"[$€£]|\b(?:usd|eur|gbp|dollars|euros|pounds)\b", text):
        raise ValueError("This version budgets in INR. Please enter your total budget in ₹.")
    result = {}
    # Match only after travel/destination cues so the departure city cannot win.
    dest = re.search(r"(?:trip|vacation|holiday|travel)\s+(?:to|in)\s+([a-z][a-z .'-]+?)(?=\s+(?:for|from|in|with|on|during|budget)\b|[.,;!]|$)", text)
    if not dest:
        dest = re.search(r"(?:\d+[- ]days?\s+)([a-z][a-z .'-]+?)(?:\s+trip|\s+vacation)", text)
    if dest:
        result["destination"] = dest.group(1).strip().title()
    if "destination" not in result:
        # Known destination, excluding the explicit origin phrase.
        destination_text = re.sub(r"\bfrom\s+[a-z .'-]+?(?=\s+(?:in|for|to|on|with|during)\b|[.,;!]|$)", "", text)
        found = next((p for p in CATALOG if re.search(r"\b" + re.escape(p["name"].lower()) + r"\b", destination_text)), None)
        if found:
            result["destination"] = found["name"]
    origin = re.search(r"\bfrom\s+([a-z][a-z .'-]+?)(?=\s+(?:in|for|to|on|with|during|budget)\b|[.,;!]|$)", text)
    if origin:
        result["origin"] = origin.group(1).strip().title()
    for pattern, field in [(r"(\d+)\s*[- ]?\s*days?", "days"), (r"(\d+)\s*(?:people|persons|adults|travellers|travelers)", "travelers")]:
        found = re.search(pattern, text)
        if found:
            result[field] = int(found.group(1))
    amount = re.search(r"(?:₹|inr\s*|budget\s*(?:of|is|:)?\s*)([\d,]+(?:\.\d+)?)\s*(lakh|lac|lakhs|lacs|k|crore)?", text)
    if amount:
        factor = {"lakh": 100000, "lac": 100000, "lakhs": 100000, "lacs": 100000, "k": 1000, "crore": 10000000}.get(amount.group(2), 1)
        result["budget"] = round_inr(float(amount.group(1).replace(",", "")) * factor)
    exact = re.search(r"\b(20\d\d-\d\d-\d\d)\b", text)
    if exact:
        result["start_date"] = date.fromisoformat(exact.group(1)).isoformat()
    else:
        for month in range(1, 13):
            match = re.search(r"\b" + calendar.month_name[month].lower() + r"(?:\s+(20\d\d))?\b", text)
            if match:
                year = int(match.group(1)) if match.group(1) else date.today().year
                if not match.group(1) and date(year, month, 1) < date.today():
                    year += 1
                result["start_date"] = date(year, month, 1).isoformat()
                break
    interests = [c for c in CATEGORIES if c in text or (c == "beaches" and "beach" in text)]
    if interests:
        result["interests"] = interests
    if any(x in text for x in ["relax", "slow", "not exhausting", "don't make", "easy pace"]):
        result["pace"] = "relaxed"
    elif any(x in text for x in ["packed", "fast-paced", "busy schedule"]):
        result["pace"] = "packed"
    elif "balanced" in text:
        result["pace"] = "balanced"
    if "luxury" in text or "5-star" in text:
        result["comfort"] = "luxury"
    elif "hostel" in text or "budget hotel" in text:
        result["comfort"] = "value"
    return result


def generic_destination(name: str) -> dict:
    return dict(id="custom", name=name, country="To confirm", iata=None, lat=None, lon=None,
                tagline="Your trip, at your pace", description="A flexible starting point for your adventure",
                room=6500, food=2200, transport=1400, activity=1600, flight=45000,
                image="/assets/terraces.jpg", image_subject="Travel inspiration", areas=["Central area"], places=[])


def estimate_budget(brief: Brief, destination: dict) -> dict:
    scale = {"value": 0.65, "comfort": 1, "luxury": 2}[brief.comfort]
    rooms = math.ceil(brief.travelers / 2)
    nights = max(0, brief.days - 1)
    same_city = brief.origin.casefold() == brief.destination.casefold()
    costs = [
        ("flights", "Return flights", 0 if same_city else destination["flight"] * brief.travelers, f"{brief.travelers} people · route estimate"),
        ("stays", "Accommodation", round_inr(destination["room"] * scale) * rooms * nights, f"{rooms} room(s) × {nights} nights"),
        ("food", "Food & cafés", round_inr(destination["food"] * scale) * brief.travelers * brief.days, f"{brief.travelers} people × {brief.days} days"),
        ("transport", "Local transport", round_inr(destination["transport"] * math.ceil(brief.travelers / 4)) * brief.days, "Shared rides / local transport allowance"),
        ("activities", "Experiences", round_inr(destination["activity"] * scale) * brief.travelers * max(0, brief.days-2), "Activity allowance for full days")]
    subtotal = sum(c[2] for c in costs)
    costs.append(("buffer", "Contingency", round_inr(subtotal * 0.15), "15% for changes and incidental costs"))
    total = sum(c[2] for c in costs)
    return dict(currency="INR", total=total, target=brief.budget, remaining=brief.budget-total,
                per_person=round_inr(total/brief.travelers), rooms=rooms, nights=nights,
                nightly_rate=round_inr(destination["room"] * scale),
                items=[dict(id=key, label=label, amount=amount, detail=detail) for key, label, amount, detail in costs],
                status="within" if total <= brief.budget else "over",
                basis="Illustrative planning estimates, not quotes. Flights use a broad India-origin baseline; no route or fare has been priced. "
                      "Rooms assume two guests each. Visa fees, insurance and shopping are excluded. Confirm all costs before booking.")


def build_schedule(brief: Brief, destination: dict) -> list[dict]:
    # Stable area clustering reduces cross-city zigzags. Interest score chooses areas first.
    places = destination["places"]
    grouped = {}
    for place in places:
        grouped.setdefault(place["area"], []).append(place)
    groups = sorted(grouped.values(), key=lambda g: -sum(p["category"] in brief.interests for p in g))
    ordered = [p for group in groups for p in sorted(group, key=lambda p: p["category"] not in brief.interests)]
    per_day = {"relaxed": 2, "balanced": 3, "packed": 4}[brief.pace]
    cursor = 0
    days = []
    for index in range(brief.days):
        is_first, is_last = index == 0, index == brief.days-1
        count = 1 if is_first or is_last else per_day
        selected = ordered[cursor:cursor+count]
        cursor += len(selected)
        items = []
        if is_first:
            items.append(dict(time="Flexible", title="Arrive, settle in & take a breath", category="travel",
                              area=destination["areas"][0], description="Keep this block flexible around actual arrival and check-in times.", duration="Allow 2–3 hours", optional=False))
        for j, place in enumerate(selected):
            time = ("16:00" if is_first else "09:00" if is_last else ["10:00", "15:00", "17:30", "19:30"][j])
            if place["category"] == "nightlife":
                time = "19:30"
            items.append(dict(time=time, title=place["name"], area=place["area"], category=place["category"],
                description=place["description"], duration="Allow 1–2 hours", optional=is_first or is_last or place["category"] == "nightlife",
                map_url=map_link(place["name"] + ", " + brief.destination)))
        if not is_first and not is_last:
            items.append(dict(time="13:00", title="Lunch & proper downtime", category="rest", area=selected[0]["area"] if selected else destination["areas"][0],
                              description="Keep this window free for lunch, a café or a hotel break.", duration="2 hours", optional=False))
            if not selected:
                items.append(dict(time="10:00", title="An open day to make your own", category="free", area=destination["areas"][0],
                    description="Explore a neighbourhood, revisit a favourite spot, or ask the copilot for ideas. No new sight has been verified for this day.", duration="Your choice", optional=True))
        if is_last:
            items.append(dict(time="Flexible", title="Check out & head home", category="travel", area="Departure",
                description="Work backwards from your confirmed departure time and the operator’s check-in guidance.", duration="Allow transfer time", optional=False))
        # Flexible arrival comes first; flexible departure comes last.
        fixed = sorted([i for i in items if i["time"] != "Flexible"], key=lambda i: i["time"])
        start = [i for i in items if i["time"] == "Flexible" and i["title"].startswith("Arrive")]
        end = [i for i in items if i["time"] == "Flexible" and not i["title"].startswith("Arrive")]
        title = "Touch down & slow down" if is_first else "One last little adventure" if is_last else (
            selected[0]["area"] + ", at your pace" if selected else "Leave a little room for serendipity")
        days.append(dict(day=index+1, date=(brief.start_date+timedelta(days=index)).isoformat() if brief.start_date else None,
                         title=title, area=selected[0]["area"] if selected else destination["areas"][0],
                         items=start+fixed+end, note="Suggested times. Confirm access, opening hours and travel times before heading out."))
    return days


def stay_options(brief: Brief, destination: dict, budget: dict) -> list[dict]:
    return [dict(name=f"{style} in {area}", area=area, source="Stay inspiration", real_property=False,
                 image="/assets/hotel.jpg" if index % 2 == 0 else "/assets/villa.jpg", image_subject="Illustrative hotel photo",
                 nightly_estimate=round_inr(budget["nightly_rate"] * [1, 0.8, 1.25][index % 3]),
                 description=description, url="https://www.booking.com/searchresults.html?" + urlencode({
                     "ss": area + ", " + brief.destination, "group_adults": brief.travelers,
                     "no_rooms": budget["rooms"], **({"checkin": brief.start_date.isoformat(),
                     "checkout": (brief.start_date+timedelta(days=brief.days-1)).isoformat()} if brief.start_date and brief.days > 1 else {})}))
            for index, (area, style, description) in enumerate(zip(destination["areas"],
                ["Boutique stay", "A comfortable base", "A little extra comfort"],
                ["Look for a well-located stay with easy access to your planned stops.", "Prioritise a comfortable room and practical transport links.", "Compare amenities, recent reviews and cancellation terms."]))]


async def plan_trip(request: PlanRequest, providers: Providers, progress=None) -> dict:
    async def stage(name, detail):
        if progress:
            await progress({"stage": name, "detail": detail})
    await stage("Intent", "Intent Agent is locking your dates, budget and travel style")
    parsed = parse_prompt(request.prompt)
    mode, warnings = "catalog", []
    if os.getenv("GROQ_API_KEY"):
        extracted = await safely(providers.extract(request.prompt), {})
        if extracted:
            parsed.update({k: v for k, v in extracted.items() if v is not None and k in Brief.model_fields})
            mode = "ai"
        else:
            warnings.append("The AI provider is unavailable. The built-in planner handled this request.")
    parsed.update(request.model_dump(exclude_none=True, exclude={"prompt"}))
    if not parsed.get("destination"):
        raise ValueError("Add a destination in Trip details, or write ‘Plan a trip to Bali for 6 days’.")
    brief = Brief.model_validate(parsed)
    if brief.start_date and brief.start_date < date.today():
        raise ValueError("The departure date is in the past. Choose a future date in Trip details.")
    await stage("Route", f"Route Agent is checking the best way from {brief.origin} to {brief.destination}")
    destination = next((dict(p) for p in CATALOG if p["name"].casefold() == brief.destination.casefold()), None)
    if not destination:
        destination = generic_destination(brief.destination)
        if os.getenv("GROQ_API_KEY"):
            discovered = await safely(providers.discover(brief), {})
            if discovered:
                destination.update(discovered)
        if not destination["places"]:
            mode = "flexible"
            warnings.append("This destination has no built-in sight list. This is a flexible outline; add Groq for destination-specific suggestions.")
        else:
            warnings.append("AI-suggested places need verification. Map searches help you confirm the correct location and access.")
    context, hotels, provider_flights = await asyncio.gather(
        safely(providers.public_context(brief.destination, brief.start_date, brief.days,
                                       destination if destination.get("lat") is not None else None), {}),
        safely(providers.hotels(brief), []),
        safely(providers.flights(brief, IATA.get(brief.origin.lower()), destination.get("iata")), []))
    await stage("Stay", "Stay Agent is comparing neighbourhoods, value and comfort")
    if destination["id"] == "custom" and context.get("geo"):
        destination.update(lat=context["geo"]["lat"], lon=context["geo"]["lon"], country=context["geo"]["country"])
    if context.get("image"):
        destination.update(image=context["image"], image_subject=brief.destination, image_source=context.get("image_source"))
    await stage("Mobility", "Mobility Agent is checking trains, cabs and local transfers")
    await stage("Itinerary", "Itinerary Agent is grouping nearby sights and protecting downtime")
    days = build_schedule(brief, destination)
    await stage("Budget", "Budget Agent is calculating every category and a 15% contingency")
    budget = estimate_budget(brief, destination)
    flight_allowance = next(x["amount"] for x in budget["items"] if x["id"] == "flights")
    local_allowance = next(x["amount"] for x in budget["items"] if x["id"] == "transport")
    flight_options = rank_flights(brief, provider_flights, flight_allowance)
    transport = surface_options(brief, destination, local_allowance)
    final_hotels = hotels or stay_options(brief, destination, budget)
    await stage("Critic", "Critic Agent is challenging the plan before it reaches you")
    agents, recommendation = agent_report(brief, destination, budget, final_hotels, flight_options, transport, mode)
    if budget["status"] == "over":
        warnings.append(f"This estimate exceeds your budget by ₹{abs(budget['remaining']):,}. Try value stays, fewer nights or another destination.")
    if brief.start_date and not request.start_date and re.search(r"\b(?:" + "|".join(calendar.month_name[1:]) + r")\b", request.prompt, re.I):
        warnings.append("A month-only request uses its first day. Confirm your exact dates in Trip details.")
    warnings.append("Entry requirements, activity suitability, transport schedules and opening hours are not verified. Review official sources before booking.")
    return dict(id=str(uuid.uuid4()), created_at=datetime.now(timezone.utc).isoformat(), mode=mode,
                brief=brief.model_dump(mode="json"), destination=destination, days=days, budget=budget,
                hotels=final_hotels, flights=flight_options, transport=transport, agents=agents,
                recommendation=recommendation, flight_search_url=flight_search(brief),
                transfer_url=map_link("airport transfer " + brief.destination), weather=context.get("weather"),
                weather_note="Forecasts are available only within the next 16 days; later dates are not a weather forecast.",
                warnings=warnings, packing=["Travel documents & copies", "Cards and some local cash", "Charger & adapter", "Comfortable walking shoes", "Weather-appropriate layers", "Personal essentials"],
                sources=[{"name": "Google Maps · verify places", "url": map_link(brief.destination)},
                         {"name": "Open-Meteo / GeoNames", "url": "https://open-meteo.com/"},
                         {"name": "Wikipedia · destination context", "url": "https://en.wikipedia.org/wiki/" + quote(brief.destination)}])
