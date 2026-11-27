"""Look up a parcel with DHL, FedEx, UPS, Blue Dart, Delhivery, or Courier Wala.

Credentials are read from the environment (or a local .env file). The page
never sees those keys. AWB patterns overlap, so a number is matched to the
most likely carrier first and the others are tried only when that lookup
does not find a shipment.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.abspath(__file__))

CARRIERS = (
    {"id": "shipway", "name": "Shipway"},
    {"id": "dhl", "name": "DHL"},
    {"id": "fedex", "name": "FedEx"},
    {"id": "ups", "name": "UPS"},
    {"id": "bluedart", "name": "Blue Dart"},
    {"id": "delhivery", "name": "Delhivery"},
    {"id": "courierwala", "name": "Courier Wala"},
)
CARRIER_NAMES = {item["id"]: item["name"] for item in CARRIERS}

_TOKEN_LOCK = threading.Lock()
_TOKENS = {}
_STAFF = threading.local()


def _staff_note(kind, data):
    """Keep the raw carrier record for the staff search running on this thread."""
    if not getattr(_STAFF, "active", False) or not isinstance(data, dict):
        return
    bag = getattr(_STAFF, "bag", None)
    if isinstance(bag, dict):
        bag[kind] = data


def load_env(path=None):
    path = path or os.path.join(ROOT, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _env(name):
    return os.environ.get(name, "").strip()


def _flag(name):
    return _env(name).lower() in {"1", "true", "yes", "on"}


def normalize_awb(value):
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def candidates(awb):
    """Return carrier ids from most likely to least likely.

    An 8 to 11 digit number is treated as Courier Wala. A 12 to 18 digit
    number is treated as Delhivery. The other of those two is still tried
    when the first one has no shipment.
    """
    if re.fullmatch(r"1Z[0-9A-Z]{16}", awb):
        order = ["ups"]
    elif re.fullmatch(r"(JD|JJD|JVGL|GM|LX|RX)[0-9A-Z]{8,}", awb) or re.fullmatch(r"DT\d{10,}", awb):
        order = ["dhl"] if not awb.startswith("DT") else ["fedex", "dhl"]
    else:
        digits = awb.isdigit()
        length = len(awb)
        if digits and 8 <= length <= 11:
            order = ["courierwala", "delhivery"]
        elif digits and 12 <= length <= 18:
            others = []
            if length in {12, 15}:
                others.append("fedex")
            if length in {12}:
                others.append("bluedart")
                others.append("dhl")
            order = ["delhivery", *others]
        elif digits and length in {20, 22, 34}:
            order = ["fedex", "delhivery"]
        else:
            order = ["dhl", "fedex", "ups", "bluedart", "delhivery"]
    if "courierwala" not in order:
        order.append("courierwala")
    return order


def _configured(carrier):
    if carrier == "dhl":
        return bool(_env("DHL_API_KEY"))
    if carrier == "fedex":
        return bool(_env("FEDEX_CLIENT_ID") and _env("FEDEX_CLIENT_SECRET"))
    if carrier == "ups":
        return bool(_env("UPS_CLIENT_ID") and _env("UPS_CLIENT_SECRET"))
    if carrier == "bluedart":
        return bool(
            _env("BLUEDART_CLIENT_ID")
            and _env("BLUEDART_CLIENT_SECRET")
            and _env("BLUEDART_LOGIN_ID")
            and _env("BLUEDART_LICENSE_KEY")
        )
    if carrier == "delhivery":
        return bool(_d1_config()) or bool(_env("DELHIVERY_API_TOKEN"))
    if carrier == "courierwala":
        user, password = _courierwala_auth()
        return bool(user and password)
    return False


def _request(method, url, headers=None, body=None, timeout=18):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type", "")
            return response.status, payload, content_type
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("Content-Type", "")
    except urllib.error.URLError as error:
        raise LookupError(str(error.reason or error)) from error


def _json_body(payload):
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _token(name, fetcher):
    now = time.monotonic()
    with _TOKEN_LOCK:
        cached = _TOKENS.get(name)
        if cached and cached[1] > now:
            return cached[0]
    token, lifetime = fetcher()
    expires = now + max(60, lifetime - 60)
    with _TOKEN_LOCK:
        _TOKENS[name] = (token, expires)
    return token


def _place(address):
    if not isinstance(address, dict):
        return ""
    parts = [
        address.get("addressLocality") or address.get("city"),
        address.get("addressRegion") or address.get("stateOrProvinceCode") or address.get("stateProvince"),
        address.get("postalCode"),
        address.get("countryCode") or address.get("country"),
    ]
    seen = []
    for part in parts:
        text = str(part or "").strip()
        if text and text not in seen:
            seen.append(text)
    return ", ".join(seen)


def _tone(status):
    text = (status or "").lower()
    if any(word in text for word in ("deliver", "signed")):
        return "ok"
    if any(word in text for word in ("fail", "exception", "undeliver", "return", "cancel", "refused")):
        return "bad"
    if any(word in text for word in ("transit", "out for", "pickup", "picked", "depart", "arriv", "customs", "manifest")):
        return "move"
    return "wait"


def _format_time(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{8}", text):
        return f"{text[6:8]}-{text[4:6]}-{text[0:4]}"
    if re.fullmatch(r"\d{14}", text):
        return f"{text[6:8]}-{text[4:6]}-{text[0:4]} {text[8:10]}:{text[10:12]}"
    try:
        parsed = parsedate_to_datetime(text)
        return parsed.strftime("%d %b %Y, %H:%M")
    except (TypeError, ValueError, IndexError):
        pass
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", text)
    if match:
        year, month, day, hour, minute = match.groups()
        return f"{day}-{month}-{year} {hour}:{minute}"
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        year, month, day = match.groups()
        return f"{day}-{month}-{year}"
    match = re.match(r"(\d{2})[/-](\d{2})[/-](\d{4})(?:[ T](\d{2}):(\d{2}))?", text)
    if match:
        day, month, year, hour, minute = match.groups()
        if hour:
            return f"{day}-{month}-{year} {hour}:{minute}"
        return f"{day}-{month}-{year}"
    return text


def _shipment(carrier, awb, status, location="", eta="", events=None):
    cleaned = []
    for event in events or []:
        description = str(event.get("description") or "").strip()
        if not description:
            continue
        cleaned.append({
            "time": _format_time(event.get("time")),
            "description": description,
            "location": str(event.get("location") or "").strip(),
        })
    return {
        "carrier": carrier,
        "carrierName": CARRIER_NAMES[carrier],
        "awb": awb,
        "status": status or "Status update",
        "tone": _tone(status),
        "location": location or "",
        "eta": _format_time(eta) if eta else "",
        "events": cleaned[:40],
    }


class NotFound(Exception):
    pass


class Unavailable(Exception):
    pass


def _track_dhl(awb):
    status, payload, _ = _request(
        "GET",
        "https://api-eu.dhl.com/track/shipments?" + urllib.parse.urlencode({
            "trackingNumber": awb,
            "language": "en",
        }),
        {"Accept": "application/json", "DHL-API-Key": _env("DHL_API_KEY")},
    )
    data = _json_body(payload)
    if status == 404 or not data.get("shipments"):
        raise NotFound()
    if status >= 400:
        raise Unavailable()
    shipment = data["shipments"][0]
    current = shipment.get("status") or {}
    location = _place((current.get("location") or {}).get("address") or {})
    events = []
    for event in shipment.get("events") or []:
        events.append({
            "time": event.get("timestamp"),
            "description": event.get("description") or event.get("status"),
            "location": _place((event.get("location") or {}).get("address") or {}),
        })
    return _shipment(
        "dhl",
        shipment.get("id") or awb,
        current.get("description") or current.get("status") or current.get("statusCode"),
        location,
        shipment.get("estimatedTimeOfDelivery") or "",
        events,
    )


def _fedex_token():
    host = "apis-sandbox.fedex.com" if _flag("FEDEX_SANDBOX") else "apis.fedex.com"
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": _env("FEDEX_CLIENT_ID"),
        "client_secret": _env("FEDEX_CLIENT_SECRET"),
    }).encode()
    status, payload, _ = _request(
        "POST",
        f"https://{host}/oauth/token",
        {"Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    data = _json_body(payload)
    if status >= 400 or not data.get("access_token"):
        raise Unavailable()
    return data["access_token"], int(data.get("expires_in") or 3600)


def _track_fedex(awb):
    host = "apis-sandbox.fedex.com" if _flag("FEDEX_SANDBOX") else "apis.fedex.com"
    token = _token("fedex", _fedex_token)
    body = json.dumps({
        "includeDetailedScans": True,
        "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": awb}}],
    }).encode()
    status, payload, _ = _request(
        "POST",
        f"https://{host}/track/v1/trackingnumbers",
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-locale": "en_US",
        },
        body,
    )
    data = _json_body(payload)
    if status >= 400:
        raise Unavailable()
    results = (((data.get("output") or {}).get("completeTrackResults") or [{}])[0].get("trackResults") or [])
    if not results or results[0].get("error"):
        raise NotFound()
    result = results[0]
    latest = result.get("latestStatusDetail") or {}
    events = []
    for scan in result.get("scanEvents") or []:
        events.append({
            "time": scan.get("date"),
            "description": scan.get("eventDescription") or scan.get("derivedStatus"),
            "location": _place(scan.get("scanLocation") or {}),
        })
    window = ((result.get("estimatedDeliveryTimeWindow") or {}).get("window") or {})
    eta = window.get("ends") or (result.get("standardTransitTimeWindow") or {}).get("window", {}).get("ends") or ""
    return _shipment(
        "fedex",
        awb,
        latest.get("description") or latest.get("statusByLocale"),
        _place(latest.get("scanLocation") or {}),
        eta,
        events,
    )


def _ups_token():
    host = "wwwcie.ups.com" if _flag("UPS_SANDBOX") else "onlinetools.ups.com"
    pair = f"{_env('UPS_CLIENT_ID')}:{_env('UPS_CLIENT_SECRET')}".encode()
    basic = base64.b64encode(pair).decode()
    status, payload, _ = _request(
        "POST",
        f"https://{host}/security/v1/oauth/token",
        {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        b"grant_type=client_credentials",
    )
    data = _json_body(payload)
    if status >= 400 or not data.get("access_token"):
        raise Unavailable()
    return data["access_token"], int(data.get("expires_in") or 3600)


def _track_ups(awb):
    host = "wwwcie.ups.com" if _flag("UPS_SANDBOX") else "onlinetools.ups.com"
    token = _token("ups", _ups_token)
    status, payload, _ = _request(
        "GET",
        f"https://{host}/api/track/v1/details/{urllib.parse.quote(awb)}?locale=en_US",
        {
            "Authorization": f"Bearer {token}",
            "transId": f"tofro-{int(time.time())}",
            "transactionSrc": "tofro",
        },
    )
    data = _json_body(payload)
    if status == 404:
        raise NotFound()
    if status >= 400:
        raise Unavailable()
    shipment = ((data.get("trackResponse") or {}).get("shipment") or [None])[0]
    package = ((shipment or {}).get("package") or [None])[0]
    if not package:
        raise NotFound()
    current = package.get("currentStatus") or {}
    events = []
    for activity in package.get("activity") or []:
        address = ((activity.get("location") or {}).get("address") or {})
        events.append({
            "time": f"{activity.get('date', '')}{activity.get('time', '')}",
            "description": (activity.get("status") or {}).get("description"),
            "location": _place(address),
        })
    delivery = ""
    dates = package.get("deliveryDate") or []
    if dates:
        delivery = dates[0].get("date") or ""
    return _shipment(
        "ups",
        package.get("trackingNumber") or awb,
        current.get("simplifiedTextDescription") or current.get("description"),
        events[0]["location"] if events else "",
        delivery,
        events,
    )


def _bluedart_token():
    host = "apigateway-sandbox.bluedart.com" if _flag("BLUEDART_SANDBOX") else "apigateway.bluedart.com"
    status, payload, _ = _request(
        "GET",
        f"https://{host}/in/transportation/token/v1/login",
        {"ClientID": _env("BLUEDART_CLIENT_ID"), "clientSecret": _env("BLUEDART_CLIENT_SECRET")},
    )
    data = _json_body(payload)
    token = data.get("JWTToken")
    if status >= 400 or not token:
        raise Unavailable()
    return token, 24 * 60 * 60


def _xml_text(node, name):
    for child in list(node):
        if child.tag.rsplit("}", 1)[-1] == name:
            return (child.text or "").strip()
    return ""


def _bluedart_from_xml(payload, awb):
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise Unavailable() from error
    shipments = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "Shipment"]
    if not shipments:
        raise NotFound()
    shipment = shipments[0]
    status_text = _xml_text(shipment, "Status")
    if not status_text or "incorrect" in status_text.lower() or "not found" in status_text.lower():
        raise NotFound()
    events = []
    for scan in shipment.iter():
        if scan.tag.rsplit("}", 1)[-1] != "ScanDetail":
            continue
        events.append({
            "time": " ".join(part for part in (_xml_text(scan, "ScanDate"), _xml_text(scan, "ScanTime")) if part),
            "description": _xml_text(scan, "Scan"),
            "location": _xml_text(scan, "ScannedLocation"),
        })
    return _shipment(
        "bluedart",
        shipment.attrib.get("WaybillNo") or awb,
        status_text,
        events[0]["location"] if events else "",
        _xml_text(shipment, "ExpectedDeliveryDate"),
        events,
    )


def _bluedart_from_json(data, awb):
    shipment_data = data.get("ShipmentData") or {}
    shipments = shipment_data.get("Shipment") or []
    if isinstance(shipments, dict):
        shipments = [shipments]
    if not shipments:
        raise NotFound()
    shipment = shipments[0]
    status_text = str(shipment.get("Status") or "")
    if not status_text or "incorrect" in status_text.lower():
        raise NotFound()
    events = []
    scans = shipment.get("Scans") or []
    if isinstance(scans, dict):
        scans = [scans]
    for scan in scans:
        detail = scan.get("ScanDetail") if isinstance(scan, dict) else None
        detail = detail or scan
        if not isinstance(detail, dict):
            continue
        events.append({
            "time": " ".join(part for part in (detail.get("ScanDate"), detail.get("ScanTime")) if part),
            "description": detail.get("Scan"),
            "location": detail.get("ScannedLocation"),
        })
    return _shipment(
        "bluedart",
        shipment.get("WaybillNo") or awb,
        status_text,
        events[0]["location"] if events else "",
        shipment.get("ExpectedDeliveryDate") or "",
        events,
    )


def _track_bluedart(awb):
    host = "apigateway-sandbox.bluedart.com" if _flag("BLUEDART_SANDBOX") else "apigateway.bluedart.com"
    token = _token("bluedart", _bluedart_token)
    query = urllib.parse.urlencode({
        "handler": "tnt",
        "action": "custawbquery",
        "loginid": _env("BLUEDART_LOGIN_ID"),
        "awb": "awb",
        "numbers": awb,
        "format": "xml",
        "lickey": _env("BLUEDART_LICENSE_KEY"),
        "verno": "1",
        "scan": "1",
    })
    status, payload, content_type = _request(
        "GET",
        f"https://{host}/in/transportation/tracking/v1?{query}",
        {"JWTToken": token, "Accept": "application/xml, application/json"},
    )
    if status == 404:
        raise NotFound()
    if status >= 400:
        raise Unavailable()
    text = payload.lstrip() if payload else b""
    if b"json" in content_type.encode() or text.startswith(b"{") or text.startswith(b"["):
        return _bluedart_from_json(_json_body(payload), awb)
    return _bluedart_from_xml(payload, awb)


def _d1_config():
    names = ("D1_CLIENT_ID", "D1_CLIENT_SECRET", "D1_AUTH_URL", "D1_REALM", "D1_CLIENT_CMS", "D1_MCP_URL")
    from_env = {name: _env(name) for name in names}
    if all(from_env.values()):
        return from_env
    path = os.path.join(ROOT, "mcp.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        values = saved["mcpServers"]["delhivery-one"]["env"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    config = {name: str(values.get(name) or "").strip() for name in names}
    return config if all(config.values()) else None


def _d1_token():
    config = _d1_config()
    url = config["D1_AUTH_URL"].rstrip("/") + "/realms/" + config["D1_REALM"] + "/protocol/openid-connect/token"
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": config["D1_CLIENT_ID"],
        "client_secret": config["D1_CLIENT_SECRET"],
    }).encode()
    status, payload, _ = _request(
        "POST",
        url,
        {"Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    data = _json_body(payload)
    if status >= 400 or not data.get("access_token"):
        raise Unavailable()
    return data["access_token"], int(data.get("expires_in") or 300)


def _mcp_text(payload):
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload or "")
    for line in text.splitlines():
        if line.startswith("data: "):
            message = json.loads(line[6:])
            content = ((message.get("result") or {}).get("content") or [{}])[0]
            return content.get("text") or ""
    return text


def _d1_tool(name, arguments):
    config = _d1_config()
    token = _token("d1", _d1_token)
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode()
    status, payload, _ = _request(
        "POST",
        config["D1_MCP_URL"],
        {
            "Authorization": f"Bearer {token}",
            "x-hq-client-id": config["D1_CLIENT_CMS"],
            "X-UCP-User-Email": "--",
            "X-UCP-Realm": config["D1_REALM"],
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        body,
        timeout=25,
    )
    if status >= 400:
        raise Unavailable()
    try:
        data = json.loads(_mcp_text(payload) or "{}")
    except json.JSONDecodeError as error:
        raise Unavailable() from error
    if isinstance(data, dict) and data.get("error_code") == "NOT_FOUND":
        raise NotFound()
    if isinstance(data, dict) and data.get("error_code"):
        raise Unavailable()
    return data


def _epoch_text(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    seconds = number / 1000 if number > 10_000_000_000 else number
    moment = datetime.fromtimestamp(seconds, timezone(timedelta(hours=5, minutes=30)))
    return moment.strftime("%d-%m-%Y")


def _scan_place(text):
    raw = str(text or "")
    marker = " at "
    if marker in raw:
        return raw.split(marker, 1)[1].strip()
    return ""


def _pretty_status(value):
    text = str(value or "").strip()
    if text.isupper():
        return text.replace("_", " ").title()
    return text


def _track_delhivery_one(awb):
    details = _d1_tool("get-shipment-details-by-awb", {"awb_number": awb})
    if not isinstance(details, dict) or not details.get("awb_number"):
        raise NotFound()
    try:
        history = _d1_tool("get-awb-transit-history", {"awb_number": awb})
    except NotFound:
        history = {}
    events = []
    for item in (history or {}).get("transit_history") or []:
        label = item.get("status") or item.get("remarks")
        events.append({
            "time": item.get("transit_at_string") or item.get("transit_at"),
            "description": label,
            "location": _scan_place(label),
        })
    events.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
    _staff_note("delhivery", details)
    _staff_note("history", history if isinstance(history, dict) else {})
    status = _pretty_status(details.get("unmapped_shipment_status") or details.get("shipment_status"))
    location = _scan_place(details.get("last_update")) or details.get("destination") or ""
    shipment = _shipment(
        "delhivery",
        details.get("awb_number") or awb,
        status,
        location,
        _epoch_text(details.get("estimated_delivery_date") or details.get("promised_delivery_date")),
        events,
    )
    order_number = str(details.get("order_number") or "").strip()
    if order_number and order_number.upper() != str(shipment["awb"]).upper():
        shipment["orderId"] = order_number
    return shipment


def _track_delhivery_public(awb):
    """Public tracking page API. It only answers when the caller is a Delhivery site."""
    status, payload, _ = _request(
        "GET",
        "https://dlv-api.delhivery.com/v3/unified-tracking-new?wbn=" + urllib.parse.quote(awb),
        {
            "Accept": "application/json",
            "Origin": "https://www.delhivery.com",
            "Referer": "https://www.delhivery.com/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    if status >= 400:
        raise Unavailable()
    data = _json_body(payload)
    rows = data.get("data") if isinstance(data, dict) else None
    if not rows:
        raise NotFound()
    item = rows[0] if isinstance(rows[0], dict) else {}
    current = item.get("status") if isinstance(item.get("status"), dict) else {}
    headline = _pretty_status(current.get("status") or current.get("instructions"))
    events = []
    for state in item.get("trackingStates") or []:
        if not isinstance(state, dict):
            continue
        for scan in state.get("scans") or []:
            if not isinstance(scan, dict):
                continue
            description = scan.get("scanNslRemark") or scan.get("scan") or current.get("instructions")
            if not description:
                continue
            events.append({
                "time": scan.get("scanDateTime") or scan.get("scanDate") or current.get("statusDateTime"),
                "description": description,
                "location": scan.get("scannedLocation") or scan.get("cityLocation") or "",
            })
    if not events and headline:
        events.append({
            "time": current.get("statusDateTime"),
            "description": current.get("instructions") or headline,
            "location": "",
        })
    location = ""
    if events:
        location = events[0]["location"]
    eta = ""
    if _tone(headline) != "ok":
        eta = item.get("promiseDeliveryDate") or item.get("deliveryDateText") or ""
    _staff_note("delhivery_public", item)
    return _shipment("delhivery", item.get("awb") or awb, headline, location, eta, events)


def _track_delhivery(awb):
    if _d1_config():
        try:
            return _track_delhivery_one(awb)
        except NotFound:
            pass
        # Delhivery One order IDs are often all digits (e.g. 88806958), so an
        # unmatched number is always searched as an order ID too.
        try:
            return _track_delhivery_order(awb)
        except NotFound:
            pass
    try:
        return _track_delhivery_public(awb)
    except NotFound:
        if not _env("DELHIVERY_API_TOKEN"):
            raise
    host = "staging-express.delhivery.com" if _flag("DELHIVERY_SANDBOX") else "track.delhivery.com"
    status, payload, _ = _request(
        "GET",
        f"https://{host}/api/v1/packages/json/?waybill={urllib.parse.quote(awb)}",
        {"Authorization": f"Token {_env('DELHIVERY_API_TOKEN')}", "Accept": "application/json"},
    )
    data = _json_body(payload)
    if status == 404:
        raise NotFound()
    if status >= 400:
        raise Unavailable()
    rows = data.get("ShipmentData") or []
    if not rows:
        raise NotFound()
    shipment = (rows[0] or {}).get("Shipment") or {}
    if not shipment or not shipment.get("AWB"):
        raise NotFound()
    _staff_note("delhivery_classic", shipment)
    current = shipment.get("Status") or {}
    events = []
    for scan in shipment.get("Scans") or []:
        detail = scan.get("ScanDetail") or scan
        events.append({
            "time": detail.get("ScanDateTime"),
            "description": detail.get("Scan") or detail.get("Instructions"),
            "location": detail.get("ScannedLocation"),
        })
    return _shipment(
        "delhivery",
        shipment.get("AWB") or awb,
        current.get("Status") or current.get("Instructions"),
        current.get("StatusLocation") or (events[0]["location"] if events else ""),
        shipment.get("ExpectedDeliveryDate") or "",
        events,
    )


def _d1_search(term, page_size=5, search_on=None):
    stamp = "2020-01-01T00:00:00+05:30"
    return _d1_tool("shipment-search", {
        "search_on": search_on or ["oid"],
        "search_term": term,
        "search_type": "CONTAINS",
        "page": 1,
        "page_size": page_size,
        "field": "manifested_at",
        "direction": "DESC",
        "op": "GTE",
        "value": stamp,
        "range_updated_at_op": "GTE",
        "range_updated_at_value": stamp,
        "range_manifested_at_op": "GTE",
        "range_manifested_at_value": stamp,
        "range_estimated_delivery_date_op": "GTE",
        "range_estimated_delivery_date_value": stamp,
        "range_shipped_at_op": "GTE",
        "range_shipped_at_value": stamp,
        "range_dispatch_count_op": "GTE",
        "range_dispatch_count_value": 0,
    })


def _search_rows(term, page_size=5):
    data = _d1_search(term, page_size)
    rows = data.get("results") if isinstance(data, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


# Order number -> AWB pairs seen while scanning, so repeat lookups skip the scan.
# Delhivery's firewall blocks bursts of requests (403), so scans are small,
# run one at a time, and never re-read an AWB whose order is already known.
_ORDER_AWBS = {}
_KNOWN_SERIALS = set()
_MISSED_ORDERS = {}
_ORDER_LOCK = threading.Lock()
_SCAN_LOCK = threading.Lock()
_SCAN_WORKERS = 4      # parallel requests during a scan
_SCAN_BACK = 30        # serials checked below the newest shipped AWB
_SCAN_AHEAD_MAX = 80   # hard cap on serials checked above it
_SCAN_GAP = 6          # stop looking ahead after this many unused AWBs in a row
_MISS_TTL = 600        # seconds before an order ID that was not found is scanned again


def _awb_from_serial(serial):
    """Delhivery AWBs end in a check digit: (serial - 1) mod 7."""
    return f"{serial}{(serial - 1) % 7}"


def _order_of_awb(awb):
    try:
        details = _d1_tool("get-shipment-details-by-awb", {"awb_number": awb})
    except NotFound:
        return None
    if not isinstance(details, dict) or not details.get("awb_number"):
        return None
    order_number = str(details.get("order_number") or "").strip().upper()
    if order_number:
        with _ORDER_LOCK:
            _ORDER_AWBS[order_number] = str(details["awb_number"])
            _KNOWN_SERIALS.add(int(awb) // 10)
    return order_number


def _scan_serials(pool, serials, term):
    """Read the AWBs for these serials; return (match found, list of hit/miss flags)."""
    todo = [serial for serial in serials if serial not in _KNOWN_SERIALS]
    results = dict.fromkeys(serials, True)
    for serial, found in zip(todo, pool.map(_order_of_awb, [_awb_from_serial(s) for s in todo])):
        results[serial] = bool(found)
        if found == term:
            return True, []
    return False, [results[serial] for serial in serials]


def _find_unshipped_order(order_id):
    """Find an order that Delhivery's search does not list yet (not picked up).

    The search only returns shipped parcels, but this account's AWBs are
    issued in sequence. Starting from the newest shipped AWB, nearby AWBs are
    read directly and matched on their order number.
    """
    term = order_id.upper()
    with _SCAN_LOCK:
        with _ORDER_LOCK:
            known = _ORDER_AWBS.get(term)
            missed = _MISSED_ORDERS.get(term, 0)
        if known:
            return known
        if time.monotonic() - missed < _MISS_TTL:
            raise NotFound()
        latest = [row.get("awb_number") for row in _search_rows("", 20)]
        serials = [int(awb) // 10 for awb in latest if str(awb or "").isdigit() and len(str(awb)) >= 10]
        if not serials:
            raise NotFound()
        anchor = max(serials)
        from concurrent.futures import ThreadPoolExecutor

        _token("d1", _d1_token)
        with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as pool:
            # Newer AWBs first: unshipped orders are usually the most recent ones.
            ahead, misses = 1, 0
            while ahead <= _SCAN_AHEAD_MAX and misses < _SCAN_GAP:
                batch = list(range(anchor + ahead, anchor + ahead + _SCAN_WORKERS))
                matched, flags = _scan_serials(pool, batch, term)
                if matched:
                    return _ORDER_AWBS[term]
                for hit in flags:
                    misses = 0 if hit else misses + 1
                ahead += _SCAN_WORKERS
            for start in range(0, _SCAN_BACK, _SCAN_WORKERS):
                batch = list(range(anchor - start, anchor - start - _SCAN_WORKERS, -1))
                matched, _ = _scan_serials(pool, batch, term)
                if matched:
                    return _ORDER_AWBS[term]
        with _ORDER_LOCK:
            _MISSED_ORDERS[term] = time.monotonic()
    raise NotFound()


def _track_delhivery_order(order_id):
    """Find a Delhivery shipment by the order number shown in Delhivery One."""
    term = order_id.upper()
    match = next((row for row in _search_rows(order_id) if str(row.get("order_number") or "").upper() == term), None)
    if match and match.get("awb_number"):
        awb = str(match["awb_number"])
    else:
        awb = _find_unshipped_order(order_id)
    shipment = _track_delhivery_one(awb)
    shipment["orderId"] = shipment.get("orderId") or order_id
    return shipment


def _courierwala_auth():
    """Customer tracking login. Env overrides the public Courier Wala track page."""
    user = _env("COURIERWALA_USER_ID") or "CUSTRC"
    password = _env("COURIERWALA_PASSWORD") or "83D54B1C37"
    return user, password


def _field(row, *names):
    if not isinstance(row, dict):
        return ""
    folded = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        text = str(folded.get(name.lower()) or "").strip()
        if text and text.lower() not in {"null", "none", "n/a", "-", "na"}:
            return text
    return ""


def _clock(value):
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{2})(\d{2})(\d{2})?", text)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return text


def _event_stamp(value):
    text = str(value or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _track_courierwala(awb):
    try:
        return _track_courierwala_query(awb, "A")
    except NotFound:
        return _track_courierwala_query(awb, "R")


def _track_courierwala_query(awb, query_type):
    user, password = _courierwala_auth()
    url = _env("COURIERWALA_API_URL") or "https://xpresion.courierwalaexpress.in/api/v1/Tracking/Tracking"
    status, payload, _ = _request(
        "POST",
        url,
        {"Content-Type": "application/json", "Accept": "application/json"},
        json.dumps({
            "UserID": user,
            "Password": password,
            "AWBNo": awb,
            "Type": query_type,
            "RequiredUrl": "yes",
            "ShowAllFields": "yes",
        }).encode(),
        timeout=25,
    )
    if status >= 400:
        raise Unavailable()
    data = _json_body(payload)
    response = data.get("Response") if isinstance(data, dict) else None
    if not isinstance(response, dict):
        raise Unavailable()
    disc = str(response.get("ErrorDisc") or "")
    lowered = disc.lower()
    if "invalid user" in lowered or "password" in lowered and "invalid" in lowered:
        raise Unavailable()
    tracking = response.get("Tracking")
    if isinstance(tracking, dict):
        tracking = [tracking]
    if not isinstance(tracking, list):
        tracking = []
    if "success" not in lowered or not tracking:
        if any(phrase in lowered for phrase in ("not exist", "not found", "no record", "no data")) or not tracking:
            raise NotFound()
        raise Unavailable()
    row = tracking[0] if isinstance(tracking[0], dict) else {}
    events = []
    raw_events = response.get("Events") or []
    if isinstance(raw_events, dict):
        raw_events = [raw_events]
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        when = " ".join(part for part in (_field(item, "EventDate"), _clock(_field(item, "EventTime"))) if part)
        label = _pretty_status(_field(item, "Status", "Remark", "Activity"))
        if not label:
            continue
        events.append({
            "time": when,
            "description": label,
            "location": _field(item, "Location", "City", "Branch"),
        })
    stamps = [_event_stamp(item["time"]) for item in events]
    known = [stamp for stamp in stamps if stamp]
    if len(known) >= 2 and known[0] < known[-1]:
        events.reverse()
    current = _pretty_status(_field(row, "Status")) or (events[0]["description"] if events else "Status update")
    location = _field(row, "Destination", "CurrentLocation", "Location")
    if not location and events:
        location = events[0]["location"]
    eta = ""
    if _tone(current) != "ok":
        eta = _field(row, "ExpectedDeliveryDate", "EDD", "EstimatedDeliveryDate")
    shipment = _shipment("courierwala", _field(row, "AWBNo") or awb, current, location, eta, events)
    reference = _field(row, "RefNo", "OrderNo", "OrderID")
    if reference and reference.upper() != str(shipment["awb"]).upper():
        shipment["orderId"] = reference
    vendor = _field(row, "VendorName", "Vendor", "CourierName", "Carrier", "Forwarder", "ServiceName")
    if vendor and not vendor.replace(" ", "").isdigit():
        shipment["carrierName"] = vendor
    _staff_note("courierwala", row)
    return shipment


SHIPWAY_URL = "https://shipway.in/api/getOrderShipmentDetails"

# Shipway status codes (see Shipway Experience > Settings > Webhooks).
SHIPWAY_TONES = {
    "DEL": "ok",
    "UND": "bad", "RTO": "bad", "RTD": "bad", "CAN": "bad", "LOST": "bad", "DMG": "bad",
    "INT": "move", "OOD": "move", "SHP": "move", "PKP": "move", "OFP": "move", "RAD": "move",
}


def _shipway_auth():
    return _env("SHIPWAY_USERNAME"), _env("SHIPWAY_LICENCE_KEY")


def _shipway_configured():
    user, key = _shipway_auth()
    return bool(user and key)


def _shipway_events(response):
    """Scan history, if Shipway includes it. The documented sample has none, so key names vary."""
    for name in ("scan", "scans", "scan_details", "shipment_track_activities", "tracking_history", "history"):
        rows = response.get(name)
        if isinstance(rows, dict):
            rows = list(rows.values())
        if isinstance(rows, list) and rows:
            break
    else:
        return []
    events = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        events.append({
            "time": _field(row, "time", "datetime", "date", "scan_time", "updated_at"),
            "description": _pretty_status(_field(row, "status_detail", "status", "activity", "remark", "remarks", "scan", "message")),
            "location": _field(row, "location", "city", "scan_location", "place"),
        })
    return events


def _track_shipway(value, field="order_id"):
    """Look up a shipment in Shipway Experience by order ID (field "order_id") or AWB ("awbno")."""
    user, key = _shipway_auth()
    status, payload, _ = _request(
        "POST",
        SHIPWAY_URL,
        {"Content-Type": "application/json", "Accept": "application/json"},
        json.dumps({"username": user, "password": key, field: value}).encode(),
    )
    if status >= 500:
        raise Unavailable()
    data = _json_body(payload)
    if not isinstance(data, dict):
        raise Unavailable()
    if str(data.get("status") or "").lower() != "success":
        message = str(data.get("msg") or data.get("message") or "").lower()
        if "password" in message or "email" in message or "licen" in message or status == 401:
            raise Unavailable()
        raise NotFound()
    response = data.get("response")
    if isinstance(response, list):
        response = response[0] if response else None
    if not isinstance(response, dict):
        raise NotFound()
    awb = _field(response, "awbno", "awb", "tracking_number")
    code = _field(response, "current_status_code").upper()
    current = _field(response, "current_status") or "Status update"
    if not awb and code in {"", "NFI"} and current.lower() in {"status update", "no information yet"}:
        raise NotFound()
    events = _shipway_events(response)
    stamp = _field(response, "time", "pickup_date")
    if not events and current and stamp:
        events = [{"time": stamp, "description": current, "location": _field(response, "to", "from")}]
    events.sort(key=lambda item: _event_stamp(_format_time(item["time"])) or datetime.min, reverse=True)
    extra = response.get("extra_fields") if isinstance(response.get("extra_fields"), dict) else {}
    order_id = _field(response, "order_id") or (value if field == "order_id" else "")
    tone = SHIPWAY_TONES.get(code) or _tone(current)
    shipment = _shipment(
        "shipway",
        awb or value,
        current,
        _field(response, "current_location", "location") or (events[0]["location"] if events else ""),
        "" if tone == "ok" else _field(extra, "expected_delivery_date") or _field(response, "expected_delivery_date", "edd", "etd"),
        events,
    )
    shipment["tone"] = tone
    carrier = _field(response, "carrier", "courier_name", "courier")
    if carrier:
        shipment["carrierName"] = "FedEx" if carrier.lower() == "fedex" else carrier
    if order_id and order_id.upper() != str(shipment["awb"]).upper():
        shipment["orderId"] = order_id
    _staff_note("shipway", response)
    return shipment


def _shipway_lookup(raw):
    """Try the input as an order ID, then as an AWB, both as typed and cleaned.

    Shipway order IDs may contain '-' or '_', so the typed form is tried first.
    """
    typed = str(raw or "").strip().lstrip("#").strip()
    values = []
    for value in (typed, normalize_awb(raw)):
        if value and value not in values and len(value) <= 60:
            values.append(value)
    for field in ("order_id", "awbno"):
        for value in values:
            try:
                return _track_shipway(value, field)
            except NotFound:
                continue
    raise NotFound()


TRACKERS = {
    "dhl": _track_dhl,
    "fedex": _track_fedex,
    "ups": _track_ups,
    "bluedart": _track_bluedart,
    "delhivery": _track_delhivery,
    "courierwala": _track_courierwala,
}


def track_awb(raw):
    awb = normalize_awb(raw)
    if not 4 <= len(awb) <= 40:
        return {
            "ok": False,
            "code": "invalid",
            "message": "Enter a valid AWB or order ID.",
        }
    shipway_failed = False
    if _shipway_configured():
        # Shipway is one quick call against the account's own orders, so it is asked first.
        try:
            shipment = _shipway_lookup(raw)
            shipment["checked"] = ["Shipway"]
            return {"ok": True, "shipment": shipment}
        except NotFound:
            pass
        except (Unavailable, LookupError, TimeoutError):
            shipway_failed = True
    order = candidates(awb)
    configured = [carrier for carrier in order if _configured(carrier)]
    if not configured:
        best = order[0]
        return {
            "ok": False,
            "code": "not_configured",
            "carrier": best,
            "carrierName": CARRIER_NAMES[best],
            "checked": [CARRIER_NAMES[carrier] for carrier in order],
            "message": f"This looks like a {CARRIER_NAMES[best]} tracking number. Live tracking for that carrier is not connected yet.",
        }
    failures = []
    for carrier in configured:
        try:
            shipment = TRACKERS[carrier](awb)
            shipment["checked"] = [CARRIER_NAMES[item] for item in configured]
            return {"ok": True, "shipment": shipment}
        except NotFound:
            continue
        except (Unavailable, LookupError, TimeoutError):
            failures.append(CARRIER_NAMES[carrier])
    if failures and len(failures) == len(configured) and (shipway_failed or not _shipway_configured()):
        return {
            "ok": False,
            "code": "unavailable",
            "message": "The courier tracking service could not be reached. Please try again in a moment.",
        }
    return {
        "ok": False,
        "code": "not_found",
        "checked": [CARRIER_NAMES[carrier] for carrier in configured],
        "message": "No shipment was found for this AWB or order ID.",
    }


def _staff_account():
    email = (_env("STAFF_EMAIL") or "tonfrologistics@gmail.com").strip().lower()
    password = _env("STAFF_PASSWORD") or "TonFro@2026"
    return email, password


def _staff_same(left, right):
    return hmac.compare_digest(
        hashlib.sha256(str(left).encode()).digest(),
        hashlib.sha256(str(right).encode()).digest(),
    )


def _staff_sign(payload, password):
    return hmac.new(str(password).encode(), str(payload).encode(), hashlib.sha256).hexdigest()


def staff_login(email, password):
    expected_email, expected_password = _staff_account()
    if not _staff_same(str(email or "").strip().lower(), expected_email) or not _staff_same(password or "", expected_password):
        return {"ok": False, "code": "denied", "message": "That email or password is not correct."}
    expires = int(time.time()) + 12 * 60 * 60
    payload = f"{expected_email}|{expires}"
    return {"ok": True, "token": f"{payload}|{_staff_sign(payload, expected_password)}", "email": expected_email}


def staff_authorized(token):
    expected_email, expected_password = _staff_account()
    parts = str(token or "").split("|")
    if len(parts) != 3:
        return False
    email, expires, signature = parts
    if email != expected_email:
        return False
    try:
        if int(expires) < int(time.time()):
            return False
    except ValueError:
        return False
    return hmac.compare_digest(_staff_sign(f"{email}|{expires}", expected_password), signature)


def staff_track(raw):
    _STAFF.active = True
    _STAFF.bag = {}
    try:
        result = track_awb(raw)
        bag = dict(_STAFF.bag)
    finally:
        _STAFF.active = False
        _STAFF.bag = {}
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    shipment = result.get("shipment")
    if isinstance(shipment, dict):
        try:
            shipment["facts"] = _staff_facts(shipment, bag)
        except (Unavailable, NotFound, LookupError, TimeoutError, ValueError, TypeError):
            shipment["facts"] = []
    return result


def _staff_facts(shipment, bag):
    facts = []
    seen = set()

    def add(label, value):
        text = _staff_text(value)
        if not text or label in seen:
            return
        seen.add(label)
        facts.append({"label": label, "value": text})

    add("Carrier", shipment.get("carrierName"))
    rich = _staff_delhivery_row(shipment)
    if isinstance(rich, dict):
        _delhivery_facts(add, rich)
    history = bag.get("history") if isinstance(bag.get("history"), dict) else None
    if history is None and _d1_config() and shipment.get("awb"):
        try:
            history = _d1_tool("get-awb-transit-history", {"awb_number": str(shipment.get("awb"))})
        except (NotFound, Unavailable, LookupError, TimeoutError):
            history = None
    if isinstance(history, dict):
        add("Consignee", history.get("consignee_name"))
        add("Consignee phone", history.get("consignee_phone"))
        attempts = history.get("delivery_attempts")
        if attempts is not None and str(attempts).strip() != "":
            add("Delivery attempts", attempts)
        add("Delivered on", _epoch_text(history.get("delivered_at")) or history.get("delivered_at_string"))
    classic = bag.get("delhivery_classic")
    if isinstance(classic, dict):
        _classic_facts(add, classic)
    public = bag.get("delhivery_public")
    if isinstance(public, dict):
        _public_facts(add, public)
    courier = bag.get("courierwala")
    if isinstance(courier, dict):
        _courier_facts(add, courier)
    shipway = bag.get("shipway")
    if isinstance(shipway, dict):
        _shipway_facts(add, shipway)
    if not any(item["label"] == "Destination" for item in facts):
        add("Current location", shipment.get("location"))
    if shipment.get("eta"):
        add("Estimated delivery", shipment.get("eta"))
    return facts[:40]


def _staff_delhivery_row(shipment):
    if not _d1_config():
        return None
    awb = str(shipment.get("awb") or "").strip()
    order_id = str(shipment.get("orderId") or "").strip()
    queries = []
    if awb:
        queries.append((["wbn"], awb))
    if order_id:
        queries.append((["oid"], order_id))
    if awb and awb.upper() != order_id.upper():
        queries.append((["oid"], awb))
    for search_on, term in queries:
        try:
            data = _d1_search(term, 5, search_on)
        except (NotFound, Unavailable, LookupError, TimeoutError):
            continue
        rows = data.get("results") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            continue
        rows = [row for row in rows if isinstance(row, dict)]
        for row in rows:
            row_awb = str(row.get("awb_number") or "").upper()
            row_order = str(row.get("order_number") or "").upper()
            if awb and row_awb == awb.upper():
                return row
            if order_id and row_order == order_id.upper():
                return row
        if len(rows) == 1:
            return rows[0]
    return None


def _staff_text(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 0:
            return ""
        if value > 10000000000:
            return _epoch_text(value)
        if float(value).is_integer():
            return str(int(value))
        return str(value)
    text = str(value or "").strip()
    if text.lower() in {"", "null", "none", "n/a", "na", "-", "0"}:
        return ""
    return text


def _staff_address(block):
    if not isinstance(block, dict):
        return ""
    name = " ".join(part for part in (_staff_text(block.get("first_name")), _staff_text(block.get("last_name"))) if part)
    city_line = ", ".join(part for part in (
        _staff_text(block.get("city")),
        _staff_text(block.get("state")),
        _staff_text(block.get("pin_code") or block.get("pincode")),
    ) if part)
    country = _staff_text(block.get("country"))
    lines = [
        name,
        _staff_text(block.get("address_line1") or block.get("address_line") or block.get("address")),
        _staff_text(block.get("address_line2")),
        city_line,
        "" if country.upper() == "IN" else country,
        _staff_text(block.get("phone") or block.get("phone_number")),
        _staff_text(block.get("email")),
    ]
    return "\n".join(line for line in lines if line)


def _delhivery_facts(add, row):
    customer = " ".join(part for part in (_staff_text(row.get("customer_first_name")), _staff_text(row.get("customer_last_name"))) if part)
    add("Customer", customer)
    add("Customer phone", row.get("customer_phone"))
    add("Consignee", _staff_address(row.get("shipping_address")))
    add("Billing address", _staff_address(row.get("billing_address")))
    add("Origin", _staff_address(row.get("origin_address")))
    add("Destination", row.get("destination"))
    add("Pickup location", row.get("pickup_location_name"))
    add("Seller", row.get("seller_name") or _staff_address(row.get("seller_address")))
    add("Return address", _staff_address(row.get("return_address")))
    add("Contents", row.get("product_description"))
    add("Payment", _pretty_status(row.get("payment_mode")))
    currency = _staff_text(row.get("currency")) or "INR"
    amount = _staff_text(row.get("grand_total_value"))
    if amount:
        add("Shipment value", f"{currency} {amount}")
    cod = _staff_text(row.get("cod_amount"))
    if cod:
        add("COD amount", f"{currency} {cod}")
    add("Shipment type", _pretty_status(row.get("shipment_type")))
    add("Package status", _pretty_status(row.get("package_status") or row.get("package_status_type")))
    fragile = str(row.get("contains_fragile_items") or "").strip().upper()
    if fragile in {"ON", "OFF"}:
        add("Fragile", "Yes" if fragile == "ON" else "No")
    weight = _staff_text(row.get("client_total_weight"))
    if weight:
        add("Weight", f"{weight} g")
    add("Quantity", row.get("item_quantity") or row.get("box_count"))
    boxes = row.get("box_list") if isinstance(row.get("box_list"), list) else []
    box_text = "; ".join(part for part in (_box_text(box) for box in boxes) if part)
    add("Package size", box_text)
    add("Zone", row.get("zone"))
    add("Delivery attempts", row.get("delivery_attempt_count"))
    add("NDR status", _pretty_status(row.get("ndr_status")))
    score = _staff_text(row.get("rto_score"))
    if score and score.upper() != "NONE":
        add("RTO score", _pretty_status(score))
    add("Address check", _pretty_status(row.get("shipping_address_validity")))
    add("Invoice number", row.get("invoice_number"))
    add("Invoice date", _epoch_text(row.get("invoice_date")))
    add("E-way bill", row.get("ewaybill_number"))
    add("Promised delivery", _epoch_text(row.get("promised_delivery_date")))
    add("Estimated delivery", _epoch_text(row.get("estimated_delivery_date")))
    add("Picked up", _epoch_text(row.get("pickup_date")))
    add("Delivered on", _epoch_text(row.get("delivered_date")))
    add("Latest update", row.get("last_update") or row.get("remarks"))
    add("Client", row.get("client_name"))


def _box_text(box):
    if not isinstance(box, dict):
        return ""
    weight = _staff_text(box.get("weight"))
    unit = _staff_text(box.get("weight_unit")) or "GM"
    length = _staff_text(box.get("length"))
    breadth = _staff_text(box.get("breadth"))
    height = _staff_text(box.get("height"))
    dim_unit = _staff_text(box.get("length_unit")) or "CM"
    parts = []
    if weight:
        parts.append(f"{weight} {unit.lower()}")
    if length and breadth and height:
        parts.append(f"{length} × {breadth} × {height} {dim_unit.lower()}")
    mode = _pretty_status(box.get("shipping_mode"))
    if mode:
        parts.append(mode)
    return ", ".join(parts)


def _classic_facts(add, shipment):
    consignee = shipment.get("Consignee") if isinstance(shipment.get("Consignee"), dict) else {}
    name = _staff_text(consignee.get("Name"))
    address = ", ".join(part for part in (
        _staff_text(consignee.get("Address1") or consignee.get("Address")),
        _staff_text(consignee.get("Address2")),
        _staff_text(consignee.get("City")),
        _staff_text(consignee.get("State")),
        _staff_text(consignee.get("PinCode") or consignee.get("Pincode")),
    ) if part)
    add("Consignee", "\n".join(part for part in (name, address, _staff_text(consignee.get("Telephone"))) if part))
    add("Origin", shipment.get("Origin"))
    add("Destination", shipment.get("Destination"))
    add("Payment", shipment.get("OrderType"))
    add("COD amount", shipment.get("CODAmount"))
    add("Shipment value", shipment.get("InvoiceAmount"))
    add("Contents", shipment.get("ProductDesc") or shipment.get("ProductsDesc"))
    sender = shipment.get("SenderName") or shipment.get("Shipper")
    add("Shipper", sender if isinstance(sender, str) else "")


def _public_facts(add, item):
    for label, key in (
        ("Consignee", "consigneeName"),
        ("Consignee phone", "consigneePhone"),
        ("Destination", "destination"),
        ("Origin", "origin"),
        ("Payment", "paymentMode"),
        ("Contents", "productType"),
    ):
        add(label, item.get(key))


_COURIER_LABELS = {
    "origin": "Origin",
    "destination": "Destination",
    "consignee": "Consignee",
    "receivername": "Receiver",
    "receiver": "Receiver",
    "receivertelephone": "Receiver phone",
    "receivermobile": "Receiver phone",
    "receiveraddress": "Receiver address",
    "receiveraddress1": "Receiver address",
    "shipper_name": "Shipper",
    "shippername": "Shipper",
    "shipperaddress": "Shipper address",
    "shipperaddress1": "Shipper address",
    "vendorname": "Handed to",
    "vendor": "Handed to",
    "vendorawbno1": "Vendor AWB",
    "vendorawbno2": "Vendor AWB 2",
    "weight": "Weight",
    "actualweight": "Weight",
    "chargedweight": "Charged weight",
    "pcs": "Pieces",
    "pieces": "Pieces",
    "nop": "Pieces",
    "service": "Service",
    "servicename": "Service",
    "goodsdesc": "Contents",
    "description": "Contents",
    "product": "Contents",
    "content": "Contents",
    "invoicevalue": "Invoice value",
    "invoiceno": "Invoice number",
    "bookingdate": "Booked on",
}


def _courier_facts(add, row):
    folded = {str(key).lower(): value for key, value in row.items()}
    for key, label in _COURIER_LABELS.items():
        if key in folded:
            add(label, folded[key])


def _shipway_facts(add, response):
    extra = response.get("extra_fields") if isinstance(response.get("extra_fields"), dict) else {}
    name = _field(response, "customer_name", "name", "consignee") or " ".join(
        part for part in (_field(response, "first_name"), _field(response, "last_name")) if part
    )
    add("Customer", name)
    add("Customer phone", _field(response, "phone", "customer_phone", "mobile", "contact"))
    add("Customer email", _field(response, "email", "customer_email"))
    add("Origin", _field(response, "from", "pickup_city", "origin"))
    add("Destination", _field(response, "to", "shipping_city", "destination"))
    add("Contents", _field(response, "product", "products_name", "item_name") or _shipway_products(response))
    add("Payment", _field(response, "payment_type", "payment_mode"))
    add("Shipment value", _field(response, "order_total", "invoice_amount", "amount"))
    add("Picked up", _field(response, "pickup_date"))
    add("Estimated delivery", _field(extra, "expected_delivery_date"))


def _shipway_products(response):
    products = response.get("products") or response.get("items") or []
    if isinstance(products, dict):
        products = list(products.values())
    if not isinstance(products, list):
        return ""
    names = []
    for item in products:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            label = _field(item, "product", "name", "product_name", "item_name")
            qty = _field(item, "qty", "quantity")
            if label:
                names.append(f"{label} × {qty}" if qty else label)
    return ", ".join(names)
