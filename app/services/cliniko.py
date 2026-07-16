"""Async Cliniko API client.

Cliniko API facts this client encodes (verified against docs.api.cliniko.com):
  - Auth: HTTP Basic, API key as username with BLANK password. Encoded manually
    as base64(api_key + ":") because some encoders malform the blank password.
  - The API key's suffix is the shard: https://api.{shard}.cliniko.com/v1
  - A descriptive User-Agent "NAME (EMAIL)" is MANDATORY (requests without it
    are blocked).
  - Rate limit: 200 requests/minute per key -> 429 with X-RateLimit-Reset.
  - available_times: from/to window must be <= 7 days, from must not precede
    'today' in the account's local timezone.
  - POST /individual_appointments: OMIT ends_at so the appointment-type
    duration (which includes buffer) is preserved.
  - Cancel is PATCH /individual_appointments/{id}/cancel with an integer
    cancellation_reason (50 = "Other").
  - Cliniko does NOT reject double bookings and has NO idempotency support —
    our Postgres layer owns both guarantees.
"""
import asyncio
import base64
import logging
from datetime import date
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger("cliniko")
settings = get_settings()

CANCELLATION_REASON_OTHER = 50


class ClinikoError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Cliniko {status_code}: {message}")


class ClinikoClient:
    def __init__(self) -> None:
        token = base64.b64encode(f"{settings.cliniko_api_key}:".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": f"{settings.cliniko_vendor_name} ({settings.cliniko_vendor_email})",
        }
        self._client = httpx.AsyncClient(
            base_url=settings.cliniko_base_url,
            headers=self._headers,
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        for attempt in range(3):
            response = await self._client.request(method, path, **kwargs)
            if response.status_code == 429:
                # X-RateLimit-Reset is a UNIX timestamp; wait briefly and retry.
                wait = min(5.0, 2.0 * (attempt + 1))
                log.warning("Cliniko 429; retrying in %.1fs", wait)
                await asyncio.sleep(wait)
                continue
            if response.status_code >= 400:
                raise ClinikoError(response.status_code, response.text[:500])
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise ClinikoError(429, "rate limited after retries")

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── Reads ──────────────────────────────────────────────────────────────

    async def list_businesses(self) -> list[dict]:
        data = await self._request("GET", "/businesses")
        return data.get("businesses", [])

    async def list_practitioners(self) -> list[dict]:
        data = await self._request("GET", "/practitioners")
        return data.get("practitioners", [])

    async def list_appointment_types(self) -> list[dict]:
        data = await self._request("GET", "/appointment_types")
        return data.get("appointment_types", [])

    async def available_times(
        self,
        business_id: str,
        practitioner_id: str,
        appointment_type_id: str,
        date_from: date,
        date_to: date,
    ) -> list[dict]:
        """Open slots for one practitioner+branch+type. Window must be <=7 days.
        Returns [{"appointment_start": "2026-07-17T04:00:00Z"}, ...]."""
        if (date_to - date_from).days > 6:
            raise ValueError("available_times window must be <= 7 days")
        path = (
            f"/businesses/{business_id}/practitioners/{practitioner_id}"
            f"/appointment_types/{appointment_type_id}/available_times"
        )
        data = await self._request(
            "GET", path, params={"from": date_from.isoformat(), "to": date_to.isoformat()}
        )
        return data.get("available_times", [])

    async def next_available_time(
        self, business_id: str, practitioner_id: str, appointment_type_id: str
    ) -> dict | None:
        path = (
            f"/businesses/{business_id}/practitioners/{practitioner_id}"
            f"/appointment_types/{appointment_type_id}/next_available_time"
        )
        data = await self._request("GET", path)
        times = data.get("next_available_time") or data.get("available_times")
        if isinstance(times, list):
            return times[0] if times else None
        return times

    # ── Setup writes (used by the seeder) ─────────────────────────────────

    async def create_business(self, business_name: str, address: str = "") -> dict:
        payload: dict[str, Any] = {"business_name": business_name}
        if address:
            payload["address_1"] = address[:255]
        return await self._request("POST", "/businesses", json=payload)

    async def create_appointment_type(
        self, name: str, duration_in_minutes: int, color: str = "#B8D9FF"
    ) -> dict:
        """Cliniko duration includes the buffer (consult + turnover).
        color must be from Cliniko's palette — callers should copy one from an
        existing appointment type (the trial's defaults are valid)."""
        payload = {
            "name": name,
            "duration_in_minutes": duration_in_minutes,
            "max_attendees": 1,
            "show_in_online_bookings": True,
            "color": color,
        }
        return await self._request("POST", "/appointment_types", json=payload)

    # ── Patients ───────────────────────────────────────────────────────────

    async def create_patient(self, first_name: str, last_name: str, phone: str | None = None) -> dict:
        payload: dict[str, Any] = {"first_name": first_name, "last_name": last_name}
        if phone:
            payload["patient_phone_numbers"] = [{"number": phone, "phone_type": "Mobile"}]
        return await self._request("POST", "/patients", json=payload)

    # ── Appointment writes ────────────────────────────────────────────────

    async def create_appointment(
        self,
        appointment_type_id: str,
        business_id: str,
        patient_id: str,
        practitioner_id: str,
        starts_at_utc_iso: str,
    ) -> dict:
        """NOTE: ends_at deliberately omitted — Cliniko derives it from the
        appointment type's duration, preserving the configured buffer."""
        payload = {
            "appointment_type_id": appointment_type_id,
            "business_id": business_id,
            "patient_id": patient_id,
            "practitioner_id": practitioner_id,
            "starts_at": starts_at_utc_iso,
        }
        return await self._request("POST", "/individual_appointments", json=payload)

    async def update_appointment(self, appointment_id: str, starts_at_utc_iso: str) -> dict:
        return await self._request(
            "PATCH",
            f"/individual_appointments/{appointment_id}",
            json={"starts_at": starts_at_utc_iso},
        )

    async def cancel_appointment(
        self, appointment_id: str, reason: int = CANCELLATION_REASON_OTHER
    ) -> dict:
        return await self._request(
            "PATCH",
            f"/individual_appointments/{appointment_id}/cancel",
            json={"cancellation_reason": reason},
        )


_client: ClinikoClient | None = None


def get_cliniko() -> ClinikoClient:
    global _client
    if _client is None:
        _client = ClinikoClient()
    return _client
