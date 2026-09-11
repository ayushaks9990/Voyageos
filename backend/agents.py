"""Specialist travel agents that rank options without inventing live inventory."""
import re
from urllib.parse import urlencode

from .models import Brief

INDIAN_CITIES = {
    "kolkata", "delhi", "new delhi", "mumbai", "bengaluru", "bangalore", "chennai", "hyderabad",
    "goa", "manali", "jaipur", "pune", "ahmedabad", "lucknow", "varanasi", "kochi", "cochin"
}


def duration_minutes(value: str) -> int:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", value or "")
    if not match:
        return 10**6
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def flight_search(brief: Brief) -> str:
    date_text = brief.start_date.isoformat() if brief.start_date else "flexible dates"
    return "https://www.google.com/travel/flights?" + urlencode({
        "q": f"return flights from {brief.origin} to {brief.destination} {date_text} for {brief.travelers} travelers"
    })


def rank_flights(brief: Brief, live_offers: list[dict], allowance: int) -> list[dict]:
    url = flight_search(brief)
    if live_offers:
        enriched = []
        priced = [float(x.get("price", 0)) for x in live_offers if float(x.get("price", 0)) > 0]
        if not priced:
            return rank_flights(brief, [], allowance)
        cheapest = min(priced)
        for index, offer in enumerate(live_offers):
            routes = offer.get("itineraries", [])
            minutes = sum(duration_minutes(route.get("duration", "")) for route in routes)
            stops = sum(max(0, len(route.get("segments", [])) - 1) for route in routes)
            price = float(offer.get("price", 0))
            price_score = cheapest / price if price else 0
            time_score = 1 / (1 + max(0, minutes - 360) / 900)
            score = round(100 * (0.68 * price_score + 0.22 * time_score + 0.10 / (1 + stops)))
            enriched.append({**offer, "id": f"provider-{index+1}", "estimate": round(price), "score": score,
                             "stops": stops, "duration_minutes": minutes, "url": url, "source_mode": "provider",
                             "label": "Provider option"})
        enriched.sort(key=lambda x: (-x["score"], x["estimate"]))
        if enriched:
            enriched[0]["label"] = "Best overall"
        return enriched
    base = max(0, allowance)
    if base == 0:
        return []
    choices = [
        ("Best overall", 1.0, "Balanced target for fare, stops and timing", 91),
        ("Lowest-price target", 0.82, "Try nearby dates and longer connections", 86),
        ("Fewer-stops target", 1.14, "Prioritise a simpler route and shorter journey", 82),
    ]
    return [{"id": f"estimate-{index+1}", "label": label, "estimate": round(base * factor), "currency": "INR",
             "score": score, "stops": None, "duration_minutes": None, "url": url, "source": "VoyageOS planning estimate",
             "source_mode": "estimate", "why": why, "travelers": brief.travelers, "live": False, "itineraries": []}
            for index, (label, factor, why, score) in enumerate(choices)]


def surface_options(brief: Brief, destination: dict, transport_allowance: int) -> dict:
    origin = brief.origin.casefold()
    domestic_india = destination.get("country", "").casefold() == "india" and origin in INDIAN_CITIES
    query = f"{brief.origin} to {brief.destination}"
    rail = {
        "available": domestic_india,
        "name": "Indian Railways",
        "label": "Best for value" if domestic_india else "Not suitable for this route",
        "description": "Compare classes and verified train availability on IRCTC." if domestic_india else
                       "This planner found no sensible direct rail handoff for the selected route.",
        "estimate": round(max(600, transport_allowance * 0.55)) if domestic_india else None,
        "url": "https://www.irctc.co.in/nget/train-search",
        "search_url": "https://www.google.com/search?" + urlencode({"q": query + " trains IRCTC"}),
        "source_mode": "estimate" if domestic_india else "not_applicable",
    }
    road = {
        "available": domestic_india,
        "name": "Intercity road",
        "label": "Flexible alternative" if domestic_india else "Local transfers only",
        "description": "Compare bus or intercity cab time against the train before choosing." if domestic_india else
                       "Use road travel for airport and local transfers after arrival.",
        "estimate": round(max(900, transport_allowance * 0.85)) if domestic_india else None,
        "url": "https://www.google.com/maps/dir/?" + urlencode({"api": 1, "origin": brief.origin, "destination": brief.destination}),
        "source_mode": "estimate" if domestic_india else "route_guidance",
    }
    cab_query = f"airport transfer in {brief.destination}"
    cabs = [
        {"name": "Airport pickup", "label": "Arrival-safe", "description": "Compare a pre-booked pickup with the airport taxi desk.",
         "estimate": round(max(500, transport_allowance * 0.18)),
         "url": "https://www.google.com/maps/search/?" + urlencode({"api": 1, "query": cab_query})},
        {"name": "Ride-hailing", "label": "On demand", "description": "Check availability, surge pricing and pickup zones after landing.",
         "estimate": round(max(350, transport_allowance * 0.12)),
         "url": "https://m.uber.com/ul/?" + urlencode({"action": "setPickup", "pickup": "my_location", "dropoff[formatted_address]": brief.destination})},
        {"name": "Day cab", "label": "Best for clusters", "description": "Useful when several stops are outside the public-transport core.",
         "estimate": round(max(1200, transport_allowance * 0.55)),
         "url": "https://www.google.com/search?" + urlencode({"q": f"day cab in {brief.destination}"})},
    ]
    return {"rail": rail, "road": road, "cabs": cabs}


def agent_report(brief: Brief, destination: dict, budget: dict, hotels: list[dict], flights: list[dict],
                 transport: dict, mode: str) -> tuple[list[dict], dict]:
    provider_flights = sum(x.get("source_mode") == "provider" for x in flights)
    real_hotels = sum(bool(x.get("real_property")) for x in hotels)
    over = budget["remaining"] < 0
    confidence = 88 if destination.get("id") != "custom" else 72
    agents = [
        {"id": "brief", "name": "Intent Agent", "role": "Understands your constraints", "status": "complete",
         "mode": "AI + rules" if mode == "ai" else "rules", "summary": f"Locked {brief.days} days, {brief.travelers} travelers and a ₹{brief.budget:,} ceiling."},
        {"id": "route", "name": "Route Agent", "role": "Ranks ways to reach the destination", "status": "complete",
         "mode": "provider" if provider_flights else "estimate", "summary": f"Ranked {len(flights)} flight choices and checked rail/road fit."},
        {"id": "stay", "name": "Stay Agent", "role": "Compares areas and properties", "status": "complete",
         "mode": "provider" if real_hotels else "estimate", "summary": f"Shortlisted {len(hotels)} stays across practical neighbourhoods."},
        {"id": "mobility", "name": "Mobility Agent", "role": "Plans airport and local movement", "status": "complete",
         "mode": "route guidance", "summary": f"Prepared {len(transport['cabs'])} local mobility choices and transfer handoffs."},
        {"id": "itinerary", "name": "Itinerary Agent", "role": "Groups nearby experiences", "status": "complete",
         "mode": "constraint engine", "summary": f"Built {brief.days} paced days with protected rest and light travel days."},
        {"id": "budget", "name": "Budget Agent", "role": "Builds the full-trip cost model", "status": "complete",
         "mode": "deterministic", "summary": f"Costed the whole group with a 15% contingency: ₹{budget['total']:,}."},
        {"id": "critic", "name": "Critic Agent", "role": "Challenges weak assumptions", "status": "complete",
         "mode": "validation", "summary": "Flagged the budget gap and cheaper levers." if over else "Checked the plan against budget, pace and source quality."},
    ]
    score = max(54, min(96, confidence + (5 if not over else -18) + (3 if provider_flights or real_hotels else 0)))
    transport_name = "flight" if flights else "local transport"
    if transport["rail"]["available"] and flights and transport["rail"].get("estimate", 10**9) < flights[0].get("estimate", 0) * 0.45:
        transport_name = "train"
    recommendation = {
        "score": score,
        "headline": f"Best fit: {transport_name.title()} + {brief.comfort} stay + {brief.pace} days",
        "reason": "This combination protects your pace while keeping the largest costs visible." if not over else
                  "This is the strongest route match, but the budget needs one deliberate trade-off before booking.",
        "budget_status": "within" if not over else "over",
        "savings": [
            "Compare departures one or two days either side of your date before paying.",
            f"Start with {destination.get('areas', ['a central area'])[0]} to reduce repeated local transfers.",
            "Choose refundable options until the route and leave dates are final.",
        ],
        "confidence": "high" if score >= 85 else "medium",
    }
    return agents, recommendation
