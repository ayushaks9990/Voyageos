from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    prompt: str = Field(min_length=8, max_length=3000)
    destination: str | None = Field(default=None, min_length=2, max_length=100)
    origin: str | None = Field(default=None, min_length=2, max_length=100)
    days: int | None = Field(default=None, ge=1, le=30)
    travelers: int | None = Field(default=None, ge=1, le=20)
    budget: int | None = Field(default=None, ge=1000, le=10000000)
    start_date: date | None = None
    pace: Literal["relaxed", "balanced", "packed"] | None = None
    comfort: Literal["value", "comfort", "luxury"] | None = None
    interests: list[str] | None = Field(default=None, max_length=12)


class Brief(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    destination: str = Field(min_length=2, max_length=100)
    origin: str = Field(default="Your city", max_length=100)
    days: int = Field(default=6, ge=1, le=30)
    travelers: int = Field(default=2, ge=1, le=20)
    budget: int = Field(default=140000, ge=1000, le=10000000)
    start_date: date | None = None
    pace: Literal["relaxed", "balanced", "packed"] = "relaxed"
    comfort: Literal["value", "comfort", "luxury"] = "comfort"
    interests: list[str] = Field(default_factory=lambda: ["nature", "food"], max_length=12)


class SuggestedPlace(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    area: str = Field(max_length=100)
    category: Literal["beaches", "nature", "culture", "adventure", "food", "nightlife", "shopping"]
    description: str = Field(max_length=400)


class Discovery(BaseModel):
    country: str = Field(max_length=100)
    places: list[SuggestedPlace] = Field(min_length=3, max_length=16)
    areas: list[str] = Field(min_length=1, max_length=3)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1500)
    brief: Brief
    history: list[dict[str, str]] = Field(default_factory=list, max_length=8)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=60)
    email: str = Field(min_length=5, max_length=200, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=5, max_length=200)
    password: str = Field(min_length=8, max_length=128)


class SaveTripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trip: dict
