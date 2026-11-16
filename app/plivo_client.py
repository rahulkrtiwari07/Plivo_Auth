"""Thin Plivo REST wrapper for the Numbers + PhoneNumber Compliance APIs.

Endpoints (all relative to https://api.plivo.com/v1/Account/{auth_id}):
  GET    /PhoneNumber/Compliance/Requirements      ?country_iso&number_type&user_type
  POST   /PhoneNumber/Compliance/                  multipart: data=<json>, documents[i].file
  GET    /PhoneNumber/Compliance/{id}/             ?expand=end_user,documents,linked_numbers
  PATCH  /PhoneNumber/Compliance/{id}/             multipart (only when status=rejected; auto-resubmits)
  GET    /PhoneNumber/                             search numbers to rent
  POST   /PhoneNumber/{number}/                    rent  {app_id, compliance_application_id}
  POST   /Number/{number}/                         update {alias, app_id}
  DELETE /Number/{number}/                         unrent
"""
from __future__ import annotations

import json
import random
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import Settings, get_settings


class PlivoError(Exception):
    def __init__(self, message: str, status_code: int = 502, payload: Any = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload


@dataclass
class UploadFile:
    filename: str
    content: bytes
    content_type: str


class PlivoAPI(Protocol):
    def get_requirements(self, country_iso: str, number_type: str, user_type: str) -> dict: ...
    def create_compliance(self, data: dict, files: list[tuple[int, UploadFile]]) -> dict: ...
    def update_compliance(self, compliance_id: str, data: dict, files: list[tuple[int, UploadFile]]) -> dict: ...
    def get_compliance(self, compliance_id: str, expand: str | None = None, subaccount: str | None = None) -> dict: ...
    def create_subaccount(self, name: str) -> dict: ...
    def search_numbers(self, **params: Any) -> dict: ...
    def buy_number(self, number: str, app_id: str | None, compliance_application_id: str,
                   subaccount: str | None = None) -> dict: ...
    def update_number(self, number: str, alias: str | None = None, app_id: str | None = None) -> dict: ...
    def delete_number(self, number: str) -> None: ...


# --------------------------------------------------------------------------------------------
# Real client
# --------------------------------------------------------------------------------------------
class PlivoClient:
    def __init__(self, settings: Settings):
        if not settings.plivo_auth_id or not settings.plivo_auth_token:
            raise RuntimeError("PLIVO_AUTH_ID / PLIVO_AUTH_TOKEN are required when PLIVO_MOCK=false")
        self._base = f"{settings.plivo_api_base.rstrip('/')}/{settings.plivo_auth_id}"
        self._http = httpx.Client(
            auth=(settings.plivo_auth_id, settings.plivo_auth_token),
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": "plivo-numbers-service/1.0"},
        )

    def _url(self, *parts: str) -> str:
        return "/".join([self._base, *[str(p).strip("/") for p in parts]]) + "/"

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise PlivoError(f"Could not reach Plivo: {exc}", 502) from exc
        if resp.status_code == 204 or not resp.content:
            if resp.is_success:
                return None
        try:
            body = resp.json()
        except ValueError:
            body = {"error": resp.text[:500]}
        if not resp.is_success:
            msg = body.get("error") or body.get("message") or f"Plivo returned HTTP {resp.status_code}"
            if isinstance(msg, dict):
                msg = json.dumps(msg)
            # 4xx from Plivo are the caller's problem (bad KYC, number taken...), surface them as 400.
            status = 400 if 400 <= resp.status_code < 500 and resp.status_code != 401 else 502
            raise PlivoError(str(msg), status, body)
        return body

    @staticmethod
    def _multipart(data: dict, files: list[tuple[int, UploadFile]]):
        """`files` holds (i, file) pairs; file i is sent as documents[i].file to match data.documents[i]."""
        form = {"data": json.dumps(data)}
        parts = [(f"documents[{i}].file", (f.filename, f.content, f.content_type)) for i, f in files]
        return form, parts

    def get_requirements(self, country_iso, number_type, user_type):
        # Docs show this path without a trailing slash.
        url = self._url("PhoneNumber", "Compliance", "Requirements").rstrip("/")
        return self._request(
            "GET", url, params={"country_iso": country_iso, "number_type": number_type, "user_type": user_type}
        )

    def create_compliance(self, data, files):
        form, parts = self._multipart(data, files)
        return self._request("POST", self._url("PhoneNumber", "Compliance"), data=form, files=parts or None)

    def update_compliance(self, compliance_id, data, files):
        form, parts = self._multipart(data, files)
        # Docs show Get/Update without a trailing slash (Create and List have one).
        url = self._url("PhoneNumber", "Compliance", compliance_id).rstrip("/")
        return self._request("PATCH", url, data=form, files=parts or None)

    def get_compliance(self, compliance_id, expand=None, subaccount=None):
        params = {k: v for k, v in {"expand": expand, "subaccount": subaccount}.items() if v}
        url = self._url("PhoneNumber", "Compliance", compliance_id).rstrip("/")
        return self._request("GET", url, params=params or None)

    def create_subaccount(self, name):
        # enabled=True so the subaccount's numbers can be used for calls straight away.
        return self._request("POST", self._url("Subaccount"), json={"name": name[:50], "enabled": True})

    def search_numbers(self, **params):
        params = {k: v for k, v in params.items() if v not in (None, "")}
        return self._request("GET", self._url("PhoneNumber"), params=params)

    def buy_number(self, number, app_id, compliance_application_id, subaccount=None):
        body = {"compliance_application_id": compliance_application_id}
        if app_id:
            body["app_id"] = app_id
        if subaccount:
            body["subaccount"] = subaccount
        return self._request("POST", self._url("PhoneNumber", number), json=body)

    def update_number(self, number, alias=None, app_id=None):
        body = {k: v for k, v in {"alias": alias, "app_id": app_id}.items() if v is not None}
        return self._request("POST", self._url("Number", number), json=body)

    def delete_number(self, number):
        self._request("DELETE", self._url("Number", number))


# --------------------------------------------------------------------------------------------
# Mock client: same contract, no network, no money spent.
#   * KYC auto-accepts MOCK_ACCEPT_AFTER seconds after submission.
#   * An end_user name containing "reject" gets rejected (to demo the resubmit flow).
# --------------------------------------------------------------------------------------------
MOCK_ACCEPT_AFTER = 5.0

def _mock_field(name, label, help_text=""):
    return {"field_name": name, "friendly_name": label, "help_text": help_text, "field_type": "string",
            "required": True, "format": "", "enums": "", "min_length": 1, "max_length": 255}


# Mirrors the Compliance API spec for India (business): a Registration Certificate (COI or Udyam,
# with business_name) AND a GST Certificate. In live mode the form is built from Plivo's real response.
_MOCK_REQUIREMENTS = {
    "business": [
        {"document_type_id": "mock-doc-registration", "name": "Registration Certificate",
         "description": "Certificate of Incorporation (MCA) or Udyam Registration Certificate (MSME)",
         "proof_required": True, "required_fields": [_mock_field("business_name", "Business Name",
                                                                  "Legal name as on the certificate")]},
        {"document_type_id": "mock-doc-gst", "name": "GST Certificate",
         "description": "GST Registration Certificate (Form GST REG-06)",
         "proof_required": True, "required_fields": []},
    ],
}

# Matches live Plivo India stock (Nov 2026): Bangalore (+91 80) and Mumbai (+91 22) only.
_MOCK_CITIES = [("Bangalore", "Bangalore", "80"), ("Mumbai", "Maharashtra", "22")]


class MockPlivoClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._apps: dict[str, dict] = {}
        self._rented: dict[str, dict] = {}
        self._subaccounts: dict[str, dict] = {}

    def create_subaccount(self, name):
        auth_id = "SA" + uuid.uuid4().hex[:18].upper()
        self._subaccounts[auth_id] = {"name": name, "enabled": True}
        return {"api_id": str(uuid.uuid4()), "auth_id": auth_id, "auth_token": "mock-token", "message": "created"}

    def get_requirements(self, country_iso, number_type, user_type):
        if user_type not in _MOCK_REQUIREMENTS:
            raise PlivoError("user_type must be individual or business", 400)
        return {"api_id": str(uuid.uuid4()), "requirement_id": f"mock-req-{number_type}-{user_type}",
                "country_iso": country_iso, "number_type": number_type, "user_type": user_type,
                "document_types": _MOCK_REQUIREMENTS[user_type]}

    def create_compliance(self, data, files):
        cid = str(uuid.uuid4())
        with self._lock:
            if any(a["alias"] == data["alias"] for a in self._apps.values()):
                raise PlivoError("alias already exists for this end user", 400)
            if data.get("subaccount") and data["subaccount"] not in self._subaccounts:
                raise PlivoError("invalid subaccount", 400)
            if data.get("subaccount", "").startswith("MA"):
                raise PlivoError(f"There is no subaccount with auth_id {data['subaccount']}", 400)
            self._apps[cid] = {"compliance_id": cid, "alias": data["alias"], "status": "submitted",
                               "subaccount": data.get("subaccount"),
                               "country_iso": data["country_iso"], "number_type": data["number_type"],
                               "user_type": data["end_user"]["type"], "end_user": data["end_user"],
                               "documents": data["documents"], "rejection_reason": None,
                               "submitted_at": time.time()}
        return {"api_id": str(uuid.uuid4()), "compliance_id": cid,
                "message": "Compliance application created and submitted for review."}

    def update_compliance(self, compliance_id, data, files):
        with self._lock:
            app = self._apps.get(compliance_id)
            if not app:
                raise PlivoError(f"Compliance application '{compliance_id}' not found.", 404)
            if app["status"] != "rejected":
                raise PlivoError(f"Compliance application must be in 'rejected' status. "
                                 f"Current status: '{app['status']}'.", 400)
            end_user = {**app["end_user"], **data.get("end_user", {})}
            app.update(end_user=end_user, documents=data.get("documents", app["documents"]),
                       status="submitted", rejection_reason=None, submitted_at=time.time())
        return {"api_id": str(uuid.uuid4()), "compliance_id": compliance_id, "status": "submitted"}

    def get_compliance(self, compliance_id, expand=None, subaccount=None):
        with self._lock:
            app = self._apps.get(compliance_id)
            if not app:
                raise PlivoError(f"Compliance application '{compliance_id}' not found.", 404)
            if app["status"] == "submitted" and time.time() - app["submitted_at"] >= MOCK_ACCEPT_AFTER:
                if "reject" in app["end_user"].get("name", "").lower():
                    app["status"] = "rejected"
                    app["rejection_reason"] = "Document is not sealed and signed by the authorised signatory."
                else:
                    app["status"] = "accepted"
            return {"api_id": str(uuid.uuid4()),
                    "compliance": {k: v for k, v in app.items() if k != "submitted_at"}}

    def search_numbers(self, country_iso, type=None, pattern=None, city=None, region=None,
                       services=None, limit=20, offset=0, **_):
        rnd = random.Random(f"{type}-{pattern}-{city}-{offset}")
        objects = []
        for _i in range(min(int(limit or 20), 20)):
            c, state, std = rnd.choice(_MOCK_CITIES)
            if city and city.lower() != c.lower():
                continue
            if type == "tollfree":
                num = "91" + "1800" + "".join(rnd.choice("0123456789") for _ in range(7))
            else:
                num = "91" + std + "".join(rnd.choice("0123456789") for _ in range(8))
            if pattern:  # Plivo semantics: prefix right after the country code
                num = "91" + pattern + num[2 + len(pattern):] if len(pattern) < 10 else "91" + pattern
            if num in self._rented:
                continue
            objects.append({"number": num, "prefix": std, "city": c if type != "tollfree" else None,
                            "country": "INDIA", "region": state, "type": type or "local",
                            "sub_type": type or "local", "setup_rate": "0.00",
                            "monthly_rental_rate": "4.00" if type == "tollfree" else "2.50",
                            "voice_enabled": True, "sms_enabled": False, "mms_enabled": False,
                            "voice_rate": "0.0150", "restriction": None, "restriction_text": None})
        return {"api_id": str(uuid.uuid4()),
                "meta": {"limit": limit, "offset": offset, "total_count": len(objects)},
                "objects": objects}

    def buy_number(self, number, app_id, compliance_application_id, subaccount=None):
        with self._lock:
            app = self._apps.get(compliance_application_id)
            if not app:
                raise PlivoError("compliance_application_id is required to rent this number.", 400)
            if app.get("subaccount") != subaccount:
                raise PlivoError("Compliance application belongs to a different (sub)account", 400)
            if app["status"] != "accepted":
                raise PlivoError(
                    f"Compliance application must be in 'accepted' status. Current status: '{app['status']}'", 400)
            if number in self._rented:
                raise PlivoError("Number is no longer available", 400)
            self._rented[number] = {"app_id": app_id, "alias": None}
        return {"api_id": str(uuid.uuid4()), "message": "created",
                "numbers": [{"number": number, "status": "Success"}], "status": "fulfilled"}

    def update_number(self, number, alias=None, app_id=None):
        if number not in self._rented:
            raise PlivoError("Number not found on account", 404)
        if alias is not None:
            self._rented[number]["alias"] = alias
        if app_id is not None:
            self._rented[number]["app_id"] = app_id
        return {"message": "changed", "api_id": str(uuid.uuid4())}

    def delete_number(self, number):
        if self._rented.pop(number, None) is None:
            raise PlivoError("Number not found on account", 404)


_client: PlivoAPI | None = None
_client_lock = threading.Lock()


def get_plivo() -> PlivoAPI:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                s = get_settings()
                _client = MockPlivoClient() if s.plivo_mock else PlivoClient(s)
    return _client


def set_plivo(client: PlivoAPI | None) -> None:
    """Testing hook."""
    global _client
    _client = client
