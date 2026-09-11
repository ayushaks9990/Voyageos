import asyncio
import json
from functools import partial
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.main import app, requests_by_ip
from backend.models import Brief, PlanRequest
from backend.planner import CATALOG, build_schedule, estimate_budget, parse_prompt, plan_trip
from backend.providers import Providers


SAMPLE = "Plan a 6-day Bali trip for 2 people from Kolkata in December. Budget ₹1.4 lakh. We like beaches, nightlife, adventure and good hotels. Don't make the schedule exhausting."


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in ("GROQ_API_KEY", "GOOGLE_PLACES_API_KEY", "AMADEUS_CLIENT_ID", "AMADEUS_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENABLE_PUBLIC_ENRICHMENT", "false")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "100")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_SECRET", "test-secret-for-voyageos")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    def block_network(request):
        raise AssertionError("An offline test attempted an unmocked outbound request")
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(block_network)))
    requests_by_ip.clear()


@pytest.fixture
def client():
    with TestClient(app) as test:
        yield test


def test_exact_user_brief():
    data = parse_prompt(SAMPLE)
    assert data["destination"] == "Bali"
    assert data["origin"] == "Kolkata"
    assert data["days"] == 6 and data["travelers"] == 2
    assert data["budget"] == 140000 and data["pace"] == "relaxed"
    assert "adventure" in data["interests"]


def test_known_origin_is_not_mistaken_for_destination():
    data = parse_prompt("Plan a trip to Kyoto for 5 days from Tokyo. Budget ₹2 lakh.")
    assert data["destination"] == "Kyoto" and data["origin"] == "Tokyo"


def test_budget_cannot_be_artificially_capped():
    b = Brief(destination="Paris", origin="Kolkata", budget=1000, travelers=3, days=7)
    budget = estimate_budget(b, CATALOG[3])
    assert budget["status"] == "over" and budget["remaining"] < 0
    assert budget["rooms"] == 2 and budget["nights"] == 6
    assert budget["total"] == sum(x["amount"] for x in budget["items"])
    assert budget["items"][-1]["amount"] == round(sum(x["amount"] for x in budget["items"][:-1]) * .15)


@pytest.mark.parametrize("pace,max_stops", [("relaxed", 2), ("balanced", 3), ("packed", 4)])
def test_schedule_pace_and_no_duplicate_sights(pace, max_stops):
    b = Brief(destination="Bali", days=12, pace=pace)
    days = build_schedule(b, CATALOG[0])
    sight_names = []
    for day in days:
        sights = [i for i in day["items"] if i["category"] not in {"travel", "rest", "free"}]
        assert len(sights) <= (1 if day["day"] in {1, 12} else max_stops)
        sight_names.extend(i["title"] for i in sights)
        if 1 < day["day"] < 12:
            assert any(i["category"] == "rest" for i in day["items"])
    assert len(sight_names) == len(set(sight_names))


def test_one_day_has_no_nights_and_both_transfers():
    b = Brief(destination="Bali", days=1)
    budget = estimate_budget(b, CATALOG[0])
    assert budget["nights"] == 0 and budget["items"][1]["amount"] == 0
    day = build_schedule(b, CATALOG[0])[0]
    assert day["items"][0]["title"].startswith("Arrive")
    assert day["items"][-1]["title"].startswith("Check out")


def test_half_rupee_rounding_matches_browser():
    b = Brief(destination="Bali", travelers=1, days=3, comfort="value")
    assert estimate_budget(b, CATALOG[0])["total"] == 45115


def test_full_api_and_static_assets(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/api/config").json()["ai"] is False
    assert "VoyageOS" in client.get("/").text
    assert client.get("/assets/bali.jpg").headers["content-type"] == "image/jpeg"
    result = client.post("/api/plan", json={"prompt": SAMPLE})
    assert result.status_code == 200, result.text
    p = result.json()
    assert p["brief"]["budget"] == 140000 and len(p["days"]) == 6
    assert len(p["flights"]) == 3 and all(f["source_mode"] == "estimate" for f in p["flights"])
    assert all(not h["real_property"] for h in p["hotels"])
    assert len(p["agents"]) == 7 and p["recommendation"]["score"] >= 50
    assert len(p["transport"]["cabs"]) == 3 and not p["transport"]["rail"]["available"]
    assert p["budget"]["total"] == 125810
    assert p["budget"]["remaining"] == 14190
    assert "no-store" in result.headers["cache-control"]


def test_form_overrides_win(client):
    r = client.post("/api/plan", json={"prompt": SAMPLE, "destination": "Goa", "days": 3, "travelers": 1,
                                        "budget": 50000, "start_date": (date.today()+timedelta(days=40)).isoformat()})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["brief"]["destination"] == "Goa" and len(p["days"]) == 3
    assert p["brief"]["budget"] == 50000 and p["brief"]["travelers"] == 1


def test_stream_has_real_progress_and_terminal_result(client):
    r = client.post("/api/plan/stream", json={"prompt": SAMPLE})
    assert r.status_code == 200
    frames = [f for f in r.text.split("\n\n") if f.startswith("event:")]
    assert sum(f.startswith("event: progress") for f in frames) == 7
    result = json.loads(next(f.split("data: ", 1)[1] for f in frames if f.startswith("event: result")))
    assert len(result["days"]) == 6


@pytest.mark.parametrize("override", [{"days": 0}, {"days": 31}, {"travelers": -1}, {"budget": 999}, {"unexpected": "field"}])
def test_schema_rejects_invalid_requests(client, override):
    assert client.post("/api/plan", json={"prompt": SAMPLE, **override}).status_code == 422


def test_missing_destination_and_past_date_are_actionable(client):
    r = client.post("/api/plan", json={"prompt": "I want a holiday"})
    assert r.status_code == 422 and "destination" in r.json()["detail"]
    assert client.post("/api/plan", json={"prompt": SAMPLE, "start_date": "2020-01-01"}).status_code == 422


def test_unknown_destination_honest_flexible_outline(client):
    r = client.post("/api/plan", json={"prompt": "Plan a trip to Kyoto for 5 days. Budget ₹2 lakh."})
    assert r.status_code == 200
    p = r.json()
    assert p["mode"] == "flexible" and p["destination"]["places"] == []
    assert any("no built-in sight list" in w for w in p["warnings"])


def test_foreign_currency_not_silently_treated_as_inr(client):
    r = client.post("/api/plan", json={"prompt": "Plan a trip to Bali with a budget of $2000."})
    assert r.status_code == 422 and "INR" in r.json()["detail"]


def test_chat_fallback_and_rate_limit(client, monkeypatch):
    response = client.post("/api/chat", json={"message": "How can I reduce the budget?", "brief": {"destination": "Bali"}})
    assert response.status_code == 200 and "allowances" in response.json()["answer"]
    requests_by_ip.clear()
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    assert client.post("/api/plan", json={"prompt": SAMPLE}).status_code == 200
    assert client.post("/api/plan", json={"prompt": SAMPLE}).status_code == 429


def test_large_body_is_rejected(client):
    assert client.post("/api/plan", content=b"x"*33000).status_code == 413


def test_account_login_and_owned_trip_storage(client):
    assert client.get("/api/auth/me").json()["user"] is None
    assert client.get("/api/trips").status_code == 401
    registered = client.post("/api/auth/register", json={
        "name": "Ayush Shaw", "email": "ayush@example.com", "password": "strong-pass-123"
    })
    assert registered.status_code == 201
    assert registered.json()["user"]["email"] == "ayush@example.com"
    assert client.get("/api/auth/me").json()["user"]["name"] == "Ayush Shaw"
    plan = client.post("/api/plan", json={"prompt": SAMPLE}).json()
    saved = client.post("/api/trips", json={"trip": plan})
    assert saved.status_code == 200 and saved.json()["trip"]["brief"]["destination"] == "Bali"
    trips = client.get("/api/trips").json()["trips"]
    assert len(trips) == 1 and trips[0]["trip"]["id"] == plan["id"]
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/trips").status_code == 401


def test_ai_extraction_failure_recovers_to_catalog(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as http:
            return await plan_trip(PlanRequest(prompt=SAMPLE), Providers(http))
    result = asyncio.run(run())
    assert result["mode"] == "catalog" and len(result["days"]) == 6
    assert any("AI provider is unavailable" in w for w in result["warnings"])


def test_custom_destination_uses_validated_ai_discovery(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    replies = iter([json.dumps({"destination": "Kyoto", "days": 4}), json.dumps({"country": "Japan", "areas": ["Higashiyama"],
        "places": [{"name": name, "area": "Higashiyama", "category": "culture", "description": "Suggested visit; verify access."}
                   for name in ["Kiyomizu-dera", "Yasaka Shrine", "Gion"]]})])
    def handler(request):
        assert request.url.host == "api.groq.com"
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await plan_trip(PlanRequest(prompt="Plan a trip to Kyoto for 4 days"), Providers(http))
    result = asyncio.run(run())
    assert result["destination"]["country"] == "Japan" and len(result["destination"]["places"]) == 3
    assert any("need verification" in w for w in result["warnings"])


def test_google_property_photo_and_attribution(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key-not-real")
    def handler(request):
        assert request.headers["x-goog-api-key"] == "test-key-not-real"
        assert "key=" not in str(request.url)
        if request.url.path.endswith("searchText"):
            return httpx.Response(200, json={"places": [{"displayName": {"text": "Test Hotel"}, "googleMapsUri": "https://maps.google.com/",
                "photos": [{"name": "places/example/photos/photo1", "authorAttributions": [{"displayName": "Photographer", "uri": "https://maps.google.com/"}]}]}]})
        return httpx.Response(200, json={"photoUri": "https://example.com/hotel.jpg"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await Providers(http).hotels(Brief(destination="Bali"))
    result = asyncio.run(run())
    assert result[0]["real_property"] and result[0]["photo_authors"][0]["displayName"] == "Photographer"


def test_amadeus_test_results_are_never_live(monkeypatch):
    monkeypatch.setenv("AMADEUS_CLIENT_ID", "test-id")
    monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("AMADEUS_ENV", "test")
    def handler(request):
        assert request.url.host == "test.api.amadeus.com"
        if request.url.path.endswith("token"):
            return httpx.Response(200, json={"access_token": "test-token"})
        return httpx.Response(200, json={"data": [{"price": {"grandTotal": "52000", "currency": "INR"}, "travelerPricings": [{}, {}],
            "itineraries": [{"duration": "PT8H", "segments": [{"departure": {"iataCode": "CCU", "at": "2030-12-01T01:00:00"},
                              "arrival": {"iataCode": "DPS"}, "carrierCode": "XX", "number": "123"}]}]}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await Providers(http).flights(Brief(destination="Bali", start_date=date(2030, 12, 1)), "CCU", "DPS")
    result = asyncio.run(run())
    assert result[0]["live"] is False and result[0]["source"] == "Amadeus TEST data"


def test_weather_uses_catalog_coordinates_and_respects_horizon(monkeypatch):
    monkeypatch.setenv("ENABLE_PUBLIC_ENRICHMENT", "true")
    calls = []
    def handler(request):
        calls.append(request)
        assert "geocoding" not in request.url.host
        if request.url.host == "api.open-meteo.com":
            assert float(request.url.params["latitude"]) == CATALOG[0]["lat"]
            return httpx.Response(200, json={"daily": {"time": [date.today().isoformat()]}})
        return httpx.Response(200, json={"query": {"pages": {"-1": {}}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = Providers(http)
            near = await provider.public_context("Bali", date.today(), 6, CATALOG[0])
            far = await provider.public_context("Bali", date.today()+timedelta(days=50), 6, CATALOG[0])
            return near, far
    near, far = asyncio.run(run())
    assert near["weather"]["time"] and "weather" not in far
    assert sum(r.url.host == "api.open-meteo.com" for r in calls) == 1
