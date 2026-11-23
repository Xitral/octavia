"""Garmin Connect: signing in once, keeping the sign-in safe, and reading the user's watch data.

Garmin's official Connect Developer Program (the Health and Activity APIs) only takes approved business
applications, so a personal assistant can't get keys for it. What's left is what the Garmin Connect app itself does,
the approach of the open-source ``garth`` and ``garminconnect`` libraries, written here with httpx so nothing new is
installed: Garmin's single sign-on page takes the email and password (and a code, for accounts with two-step
verification) and hands back a ticket; the ticket buys an OAuth 1 token (valid about a year) signed with the Connect
app's own consumer key, and that buys short-lived OAuth 2 tokens for ``connectapi.garmin.com``, renewed from the
OAuth 1 token whenever they run out.

The password is typed into a small window (``octavia.platform.sign_in_window``), sent to Garmin once and never kept.
Only the tokens are, in ``data/garmin/tokens.json``, sealed with Windows' DPAPI so only this Windows account can read
them. Nothing here puts a token, the password or a code in a result, an error or a log line.

Not loaded as a tool module (the name starts with ``_``); ``garmin`` and the scheduler's Garmin conditions use it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

from octavia.config import find_project_root
from octavia.core.jsonfile import read_json, write_json
from octavia.core.text import clock
from octavia.net import tls_context

log = logging.getLogger(__name__)

TOKEN_FILE = find_project_root() / "data" / "garmin" / "tokens.json"
SSO = "https://sso.garmin.com/sso"
SSO_EMBED = f"{SSO}/embed"
API = "https://connectapi.garmin.com"
# The Garmin Connect app's own OAuth consumer, as garth uses it: Garmin hands tokens only to its apps.
CONSUMER_KEY = "fc3e99d2-118c-44b8-8ae3-03370dde24c0"
CONSUMER_SECRET = "E08WAR897WEy2knn7aFBrvegVAf0AFdWBBF"
SSO_AGENT = "GCM-iOS-5.7.2.1"
APP_AGENT = "com.garmin.android.apps.connectmobile"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
CODE_WAIT_S = 600.0  # how long a sign-in waits for the verification code Garmin emailed
EMBED_PARAMS = {"id": "gauth-widget", "embedWidget": "true", "gauthHost": SSO}
SIGNIN_PARAMS = {
    **EMBED_PARAMS,
    "gauthHost": SSO_EMBED,
    "service": SSO_EMBED,
    "source": SSO_EMBED,
    "redirectAfterAccountLoginUrl": SSO_EMBED,
    "redirectAfterAccountCreationUrl": SSO_EMBED,
}
_CSRF = re.compile(r'name="_csrf"\s+value="(.+?)"')
_TITLE = re.compile(r"<title>(.+?)</title>", re.IGNORECASE | re.DOTALL)
_TICKET = re.compile(r'embed\?ticket=([^"]+)"')
NOT_CONNECTED = (
    "Garmin isn't connected yet. Ask the user whether to connect it: connect_garmin opens a small window on their "
    "screen where they type their Garmin Connect email and password (never say them aloud)."
)


class GarminError(RuntimeError):
    """Garmin couldn't be reached, refused, or answered oddly; the message is for the user and holds no secret."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class NotConnected(GarminError):
    def __init__(self) -> None:
        super().__init__(NOT_CONNECTED)


# --------------------------------------------------------------------------------------------------- keeping tokens


def _dpapi_seal(raw: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptProtectData(raw, "Octavia Garmin sign-in", None, None, None, 0)


def _dpapi_open(sealed: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptUnprotectData(sealed, None, None, None, 0)[1]


Seal = Callable[[bytes], bytes]


class TokenStore:
    """The sign-in on disk: sealed with DPAPI on Windows (readable only by this Windows account), so a copy of the
    file elsewhere is useless. ``seal``/``open`` are None where there's no DPAPI (tests, other systems)."""

    def __init__(self, path: Path = TOKEN_FILE, seal: Seal | None = None, open_: Seal | None = None) -> None:
        self.path = path
        windows = sys.platform == "win32"
        self._seal = seal if seal is not None else (_dpapi_seal if windows else None)
        self._open = open_ if open_ is not None else (_dpapi_open if windows else None)

    def load(self) -> dict[str, Any] | None:
        data = read_json(self.path, "the Garmin sign-in")
        if not isinstance(data, dict):
            return None
        try:
            if "sealed" in data and self._open is not None:
                tokens = json.loads(self._open(base64.b64decode(data["sealed"])))
            else:
                tokens = data.get("plain")
        except Exception:
            log.warning("Couldn't open the Garmin sign-in in %s (made under another Windows account?)", self.path)
            return None
        return tokens if isinstance(tokens, dict) else None

    def save(self, tokens: dict[str, Any]) -> None:
        raw = json.dumps(tokens).encode()
        if self._seal is not None:
            write_json(self.path, {"sealed": base64.b64encode(self._seal(raw)).decode()})
        else:
            write_json(self.path, {"plain": tokens})

    def clear(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


# --------------------------------------------------------------------------------------------------- OAuth 1


def _pct(text: str) -> str:
    return quote(str(text), safe="~-._")


def oauth1_header(
    method: str,
    url: str,
    token: tuple[str, str] | None = None,
    form: dict[str, str] | None = None,
    *,
    consumer: tuple[str, str] = (CONSUMER_KEY, CONSUMER_SECRET),
    nonce: str | None = None,
    timestamp: int | None = None,
) -> str:
    """The ``Authorization`` header of an OAuth 1.0a request signed with HMAC-SHA1 (RFC 5849)."""
    parts = urlsplit(url)
    oauth = {
        "oauth_consumer_key": consumer[0],
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        "oauth_version": "1.0",
    }
    if token is not None:
        oauth["oauth_token"] = token[0]
    pairs = [*parse_qsl(parts.query, keep_blank_values=True), *(form or {}).items(), *oauth.items()]
    normalized = "&".join(f"{k}={v}" for k, v in sorted((_pct(k), _pct(v)) for k, v in pairs))
    base = "&".join((method.upper(), _pct(f"{parts.scheme}://{parts.netloc}{parts.path}"), _pct(normalized)))
    key = f"{_pct(consumer[1])}&{_pct(token[1] if token else '')}"
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in oauth.items())


# --------------------------------------------------------------------------------------------------- signing in


@dataclass
class _Pending:
    """A sign-in waiting for the verification code Garmin emailed: its session (cookies) and the page it's on."""

    client: httpx.AsyncClient
    page: str
    referer: str
    started: float = field(default_factory=time.monotonic)


def _title(page: str) -> str:
    found = _TITLE.search(page)
    return " ".join(found.group(1).split()) if found else ""


def _csrf(page: str) -> str:
    found = _CSRF.search(page)
    if found is None:
        raise GarminError("Garmin's sign-in page has changed, so I couldn't sign in.")
    return found.group(1)


class GarminAuth:
    """Signs in (``start``, then ``finish`` with a code when Garmin asks for one) and keeps the tokens fresh."""

    def __init__(
        self,
        store: TokenStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock_s: Callable[[], float] = time.time,
    ) -> None:
        self.store = store or TokenStore()
        self.transport = transport
        self._now = clock_s
        self._pending: _Pending | None = None

    def _client(self, agent: str = SSO_AGENT) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport,
            timeout=TIMEOUT,
            verify=tls_context(),
            follow_redirects=True,
            headers={"User-Agent": agent},
        )

    @property
    def connected(self) -> bool:
        tokens = self.store.load()
        return bool(tokens and tokens.get("oauth1_token"))

    @property
    def waiting_for_code(self) -> bool:
        if self._pending is not None and time.monotonic() - self._pending.started > CODE_WAIT_S:
            self._drop_pending()
        return self._pending is not None

    def _drop_pending(self) -> None:
        if self._pending is not None:
            client, self._pending = self._pending.client, None
            _close_later(client)

    async def start(self, email: str, password: str) -> bool:
        """Sign in with the email and password. True when it's done; False when Garmin emailed a code to ``finish``
        with. Raises GarminError (wrong password, Garmin refused, the page changed)."""
        self._drop_pending()
        client = self._client()
        try:
            await self._send(client, "GET", f"{SSO}/embed", params=EMBED_PARAMS)
            page = await self._send(client, "GET", f"{SSO}/signin", params=SIGNIN_PARAMS)
            form = {"username": email, "password": password, "embed": "true", "_csrf": _csrf(page.text)}
            page = await self._send(
                client, "POST", f"{SSO}/signin", params=SIGNIN_PARAMS, data=form, referer=str(page.url)
            )
            title = _title(page.text)
            if "MFA" in title:
                self._pending = _Pending(client, page.text, str(page.url))
                return False
            await self._finish_page(client, page.text, title)
        finally:
            if self._pending is None or self._pending.client is not client:
                await client.aclose()
        return True

    async def finish(self, code: str) -> None:
        """Finish the sign-in ``start`` began with the code Garmin emailed or texted."""
        if not self.waiting_for_code or self._pending is None:
            raise GarminError("There's no Garmin sign-in waiting for a code; connect Garmin again.")
        pending = self._pending
        form = {"mfa-code": code, "embed": "true", "_csrf": _csrf(pending.page), "fromPage": "setupEnterMfaCode"}
        page = await self._send(
            pending.client,
            "POST",
            f"{SSO}/verifyMFA/loginEnterMfaCode",
            params=SIGNIN_PARAMS,
            data=form,
            referer=pending.referer,
        )
        title = _title(page.text)
        if "MFA" in title:
            pending.page, pending.referer = page.text, str(page.url)
            raise GarminError("Garmin didn't accept that code. Check the newest code and try again.")
        try:
            await self._finish_page(pending.client, page.text, title)
        finally:
            self._drop_pending()

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        referer: str | None = None,
    ) -> httpx.Response:
        headers = {"Referer": referer} if referer else {}
        try:
            response = await client.request(method, url, params=params, data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise GarminError(f"I couldn't reach Garmin ({type(exc).__name__}).") from exc
        if response.status_code == 429:
            raise GarminError("Garmin says there have been too many sign-in attempts; try again in an hour.", 429)
        if response.status_code >= 400:
            raise GarminError(f"Garmin's sign-in answered with an error (HTTP {response.status_code}).")
        return response

    async def _finish_page(self, client: httpx.AsyncClient, page: str, title: str) -> None:
        if title != "Success":
            if "locked" in page.lower():
                raise GarminError("Garmin says the account is locked; the user can unlock it on garmin.com.")
            raise GarminError("Garmin didn't accept that email and password.")
        found = _TICKET.search(page)
        if found is None:
            raise GarminError("Garmin's sign-in page has changed, so I couldn't sign in.")
        await self._tokens_from_ticket(found.group(1))

    async def _tokens_from_ticket(self, ticket: str) -> None:
        url = f"{API}/oauth-service/oauth/preauthorized?ticket={ticket}&login-url={SSO_EMBED}&accepts-mfa-tokens=true"
        async with self._client(APP_AGENT) as client:
            try:
                response = await client.get(url, headers={"Authorization": oauth1_header("GET", url)})
            except httpx.HTTPError as exc:
                raise GarminError(f"I couldn't reach Garmin ({type(exc).__name__}).") from exc
        if response.status_code != 200:
            raise GarminError(f"Garmin wouldn't finish the sign-in (HTTP {response.status_code}).")
        given = dict(parse_qsl(response.text))
        if not given.get("oauth_token") or not given.get("oauth_token_secret"):
            raise GarminError("Garmin's answer to the sign-in was missing its token.")
        tokens = {"oauth1_token": given["oauth_token"], "oauth1_secret": given["oauth_token_secret"]}
        if given.get("mfa_token"):
            tokens["mfa_token"] = given["mfa_token"]
        tokens.update(await self._exchange(tokens))
        self.store.save(tokens)

    async def _exchange(self, tokens: dict[str, Any]) -> dict[str, Any]:
        """New OAuth 2 tokens from the OAuth 1 token."""
        url = f"{API}/oauth-service/oauth/exchange/user/2.0"
        form = {"mfa_token": tokens["mfa_token"]} if tokens.get("mfa_token") else {}
        header = oauth1_header("POST", url, (tokens["oauth1_token"], tokens["oauth1_secret"]), form)
        async with self._client(APP_AGENT) as client:
            try:
                response = await client.post(
                    url,
                    data=form,
                    headers={"Authorization": header, "Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                raise GarminError(f"I couldn't reach Garmin ({type(exc).__name__}).") from exc
        if response.status_code in (401, 403):
            raise GarminError("Garmin no longer accepts the saved sign-in; connect Garmin again.", response.status_code)
        if response.status_code != 200:
            raise GarminError(f"Garmin couldn't renew the sign-in (HTTP {response.status_code}).")
        try:
            given = response.json()
            access = str(given["access_token"])
            expires_in = float(given.get("expires_in") or 3600)
        except (ValueError, KeyError, TypeError) as exc:
            raise GarminError("Garmin's answer to renewing the sign-in was odd.") from exc
        return {"access_token": access, "expires_at": self._now() + expires_in}

    async def access_token(self, renew: bool = False) -> str:
        """A usable OAuth 2 access token, renewed first when it has (nearly) run out or ``renew`` says so."""
        tokens = self.store.load()
        if not tokens or not tokens.get("oauth1_token"):
            raise NotConnected()
        if renew or not tokens.get("access_token") or float(tokens.get("expires_at") or 0) < self._now() + 60:
            tokens.update(await self._exchange(tokens))
            self.store.save(tokens)
        return str(tokens["access_token"])

    def remember(self, **values: Any) -> None:
        tokens = self.store.load()
        if tokens:
            tokens.update(values)
            self.store.save(tokens)

    def recall(self, name: str) -> Any:
        return (self.store.load() or {}).get(name)

    def disconnect(self) -> bool:
        self._drop_pending()
        return self.store.clear()


def _close_later(client: httpx.AsyncClient) -> None:
    import asyncio

    try:
        asyncio.get_running_loop().create_task(client.aclose())
    except RuntimeError:
        pass


# --------------------------------------------------------------------------------------------------- reading data


class GarminClient:
    """GET requests to Garmin Connect with the user's sign-in; nothing here changes anything on Garmin."""

    def __init__(self, auth: GarminAuth | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.auth = auth or GarminAuth(transport=transport)
        self._transport = transport if transport is not None else self.auth.transport

    @property
    def connected(self) -> bool:
        return self.auth.connected

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """The JSON at ``path``; None when Garmin has nothing for it (HTTP 204 or 404)."""
        for attempt in range(2):
            token = await self.auth.access_token(renew=attempt > 0)
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT, verify=tls_context(), base_url=API
            ) as client:
                try:
                    response = await client.get(
                        path,
                        params=params,
                        headers={"Authorization": f"Bearer {token}", "User-Agent": APP_AGENT},
                    )
                except httpx.HTTPError as exc:
                    raise GarminError(f"I couldn't reach Garmin Connect ({type(exc).__name__}).") from exc
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code in (204, 404):
                return None
            if response.status_code == 429:
                raise GarminError("Garmin Connect says it's had too many requests; try again in a few minutes.", 429)
            if response.status_code >= 400:
                raise GarminError(f"Garmin Connect answered with an error (HTTP {response.status_code}).")
            try:
                return response.json()
            except ValueError as exc:
                raise GarminError("Garmin Connect's answer wasn't readable.") from exc
        raise GarminError("Garmin no longer accepts the saved sign-in; connect Garmin again.", 401)

    async def display_name(self) -> str:
        """The account's display name, which some of Garmin's addresses need; asked once, then remembered."""
        known = self.auth.recall("display_name")
        if known:
            return str(known)
        profile = await self.get("/userprofile-service/socialProfile")
        name = profile.get("displayName") if isinstance(profile, dict) else None
        if not name:
            raise GarminError("Garmin Connect didn't say which account this is.")
        self.auth.remember(display_name=str(name))
        return str(name)

    async def summary(self, day: date) -> dict[str, Any]:
        name = await self.display_name()
        data = await self.get(f"/usersummary-service/usersummary/daily/{name}", {"calendarDate": day.isoformat()})
        return data if isinstance(data, dict) else {}

    async def heart_rate(self, day: date) -> dict[str, Any]:
        name = await self.display_name()
        data = await self.get(f"/wellness-service/wellness/dailyHeartRate/{name}", {"date": day.isoformat()})
        return data if isinstance(data, dict) else {}

    async def hrv(self, day: date) -> dict[str, Any]:
        data = await self.get(f"/hrv-service/hrv/{day.isoformat()}")
        return data if isinstance(data, dict) else {}

    async def stress(self, day: date) -> dict[str, Any]:
        data = await self.get(f"/wellness-service/wellness/dailyStress/{day.isoformat()}")
        return data if isinstance(data, dict) else {}

    async def sleep(self, day: date) -> dict[str, Any]:
        name = await self.display_name()
        data = await self.get(
            f"/wellness-service/wellness/dailySleepData/{name}", {"date": day.isoformat(), "nonSleepBufferMinutes": 60}
        )
        return data if isinstance(data, dict) else {}

    async def spo2(self, day: date) -> dict[str, Any]:
        data = await self.get(f"/wellness-service/wellness/daily/spo2/{day.isoformat()}")
        return data if isinstance(data, dict) else {}

    async def activities(self, limit: int, day: date | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"start": 0, "limit": limit}
        if day is not None:
            params.update(startDate=day.isoformat(), endDate=day.isoformat())
        data = await self.get("/activitylist-service/activities/search/activities", params)
        return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


_SHARED: GarminClient | None = None


def shared_client() -> GarminClient:
    """One client for the tools and the scheduler's conditions, so a sign-in waiting for its code is shared."""
    global _SHARED
    if _SHARED is None:
        _SHARED = GarminClient()
    return _SHARED


# --------------------------------------------------------------------------------------------------- saying it


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _int(value: Any) -> str:
    number = _num(value)
    return "" if number is None else f"{round(number):,}"


def duration(seconds: Any) -> str:
    """ "7 hours 12 minutes", "45 minutes"."""
    total = _num(seconds)
    if total is None:
        return ""
    hours, minutes = divmod(round(total / 60), 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes or not hours:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts)


def miles(meters: Any) -> str:
    number = _num(meters)
    return "" if number is None else f"{number / 1609.344:.1f} miles"


def stress_word(level: float) -> str:
    """Garmin's own bands for a stress level."""
    if level <= 25:
        return "resting"
    if level <= 50:
        return "low"
    if level <= 75:
        return "medium"
    return "high"


def _when_ms(value: Any) -> datetime | None:
    number = _num(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number / 1000).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def latest_heart_rate(data: dict[str, Any]) -> tuple[int, datetime] | None:
    """The newest reading the watch has synced that day: (beats per minute, when)."""
    values = data.get("heartRateValues")
    if not isinstance(values, list):
        return None
    for point in reversed(values):
        if isinstance(point, list | tuple) and len(point) >= 2 and _num(point[1]) is not None:
            moment = _when_ms(point[0])
            if moment is not None:
                return round(point[1]), moment
    return None


def say_latest_heart_rate(data: dict[str, Any]) -> str:
    """The newest reading, said as what it is: their heart rate as of the watch's last sync."""
    latest = latest_heart_rate(data)
    if latest is None:
        return ""
    return f"Latest heart rate reading {latest[0]} at {clock(latest[1])} (their heart rate as of the last sync)."


def say_heart_rate(data: dict[str, Any]) -> str:
    # The latest reading first, and the resting rate named as the day's baseline: asked for "my heart rate right
    # now", she once read out the resting rate instead.
    parts = [say_latest_heart_rate(data)]
    if resting := _int(data.get("restingHeartRate")):
        week = _int(data.get("lastSevenDaysAvgRestingHeartRate"))
        parts.append(
            f"Resting heart rate {resting}" + (f" (7-day average {week})" if week else "") + ", a daily baseline, "
            "not a current reading."
        )
    low, high = _int(data.get("minHeartRate")), _int(data.get("maxHeartRate"))
    if low and high:
        parts.append(f"It ranged from {low} to {high} beats per minute.")
    return " ".join(p for p in parts if p)


def say_hrv(data: dict[str, Any]) -> str:
    summary = data.get("hrvSummary") if isinstance(data.get("hrvSummary"), dict) else {}
    night = _int(summary.get("lastNightAvg"))
    if not night:
        return ""
    text = f"Overnight HRV averaged {night} milliseconds"
    if week := _int(summary.get("weeklyAvg")):
        text += f", weekly average {week}"
    status = str(summary.get("status") or "").replace("_", " ").lower()
    return text + (f", status {status}." if status and status != "none" else ".")


def say_stress(data: dict[str, Any]) -> str:
    average = _num(data.get("avgStressLevel"))
    if average is None or average < 0:
        return ""
    text = f"Average stress {round(average)}, {stress_word(average)}"
    peak = _num(data.get("maxStressLevel"))
    return text + (f"; peak {round(peak)}." if peak is not None and peak >= 0 else ".")


def say_body_battery(summary: dict[str, Any]) -> str:
    now = _int(summary.get("bodyBatteryMostRecentValue"))
    if not now:
        return ""
    text = f"Body Battery is at {now}"
    high, low = _int(summary.get("bodyBatteryHighestValue")), _int(summary.get("bodyBatteryLowestValue"))
    if high and low:
        text += f"; it peaked at {high} and dipped to {low}"
    charged, drained = _int(summary.get("bodyBatteryChargedValue")), _int(summary.get("bodyBatteryDrainedValue"))
    if charged and drained:
        text += f", charged {charged} and drained {drained}"
    return text + "."


def say_sleep(data: dict[str, Any]) -> str:
    dto = data.get("dailySleepDTO") if isinstance(data.get("dailySleepDTO"), dict) else {}
    total = _num(dto.get("sleepTimeSeconds"))
    if not total:
        return ""
    text = f"Slept {duration(total)}"
    stages = [
        f"{label} {duration(dto.get(key))}"
        for label, key in (("deep", "deepSleepSeconds"), ("light", "lightSleepSeconds"), ("REM", "remSleepSeconds"))
        if _num(dto.get(key))
    ]
    if stages:
        text += f" ({', '.join(stages)})"
    scores = dto.get("sleepScores") if isinstance(dto.get("sleepScores"), dict) else {}
    overall = scores.get("overall") if isinstance(scores.get("overall"), dict) else {}
    if score := _int(overall.get("value")):
        quality = str(overall.get("qualifierKey") or "").replace("_", " ").lower()
        text += f"; sleep score {score}" + (f", {quality}" if quality else "")
    return text + "."


def say_steps(summary: dict[str, Any]) -> str:
    steps = _int(summary.get("totalSteps"))
    if not steps:
        return ""
    text = f"{steps} steps"
    if goal := _int(summary.get("dailyStepGoal")):
        text += f" of a {goal} goal"
    if distance := miles(summary.get("totalDistanceMeters")):
        text += f", {distance}"
    return text + "."


def say_spo2(data: dict[str, Any]) -> str:
    average = _int(data.get("averageSpO2"))
    if not average:
        return ""
    text = f"Blood oxygen averaged {average} percent"
    if lowest := _int(data.get("lowestSpO2")):
        text += f", lowest {lowest}"
    if latest := _int(data.get("latestSpO2")):
        text += f", latest {latest}"
    return text + "."


def say_activity(activity: dict[str, Any]) -> str:
    kind = activity.get("activityType") if isinstance(activity.get("activityType"), dict) else {}
    name = str(activity.get("activityName") or str(kind.get("typeKey") or "activity").replace("_", " "))
    text = name
    started = str(activity.get("startTimeLocal") or "")
    try:
        moment = datetime.fromisoformat(started)
        text += f", {moment:%A} at {clock(moment)}"
    except ValueError:
        pass
    details = [miles(activity.get("distance"))] if _num(activity.get("distance")) else []
    if length := duration(activity.get("duration")):
        details.append(length)
    if heart := _int(activity.get("averageHR")):
        details.append(f"average heart rate {heart}")
    if calories := _int(activity.get("calories")):
        details.append(f"{calories} calories")
    return text + (f": {', '.join(details)}" if details else "") + "."


def parse_day(text: str | None, today: date) -> date:
    """'today', 'yesterday' or 'YYYY-MM-DD' (empty = today). Raises ValueError for the user."""
    value = (text or "").strip().lower()
    if value in ("", "today", "now"):
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    try:
        wanted = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Give the date as YYYY-MM-DD, today or yesterday.") from exc
    if wanted > today:
        raise ValueError("That date hasn't happened yet.")
    return wanted
