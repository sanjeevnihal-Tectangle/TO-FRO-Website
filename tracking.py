"""Look up a parcel with DHL, FedEx, UPS, Blue Dart, Delhivery, or Courier Wala.

Credentials are read from the environment (or a local .env file). The page
never sees those keys. AWB patterns overlap, so a number is matched to the
most likely carrier first and the others are tried only when that lookup
does not find a shipment.
"""

import base64
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

    Courier Wala is always last. It tracks shipments booked with them,
    including parcels they hand to another carrier.
    """
    if re.fullmatch(r"1Z[0-9A-Z]{16}", awb):
        order = ["ups"]
    elif re.fullmatch(r"(JD|JJD|JVGL|GM|LX|RX)[0-9A-Z]{8,}", awb) or re.fullmatch(r"DT\d{10,}", awb):
        order = ["dhl"] if not awb.startswith("DT") else ["fedex", "dhl"]
    else:
        digits = awb.isdigit()
        length = len(awb)
        if digits and 10 <= length <= 18:
            others = []
            if length in {12, 15}:
                others.append("fedex")
            if length in {10, 11, 12, 15}:
                others.append("bluedart")
            if length in {10, 11, 12}:
                others.append("dhl")
            order = ["delhivery", *others]
        elif digits and length in {20, 22, 34}:
            order = ["fedex", "delhivery"]
        elif digits and length == 9:
            order = ["ups", "bluedart", "dhl"]
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
        "events": cleaned[:12],
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
    status = _pretty_status(details.get("unmapped_shipment_status") or details.get("shipment_status"))
    location = _scan_place(details.get("last_update")) or details.get("destination") or ""
    return _shipment(
        "delhivery",
        details.get("awb_number") or awb,
        status,
        location,
        _epoch_text(details.get("estimated_delivery_date") or details.get("promised_delivery_date")),
        events,
    )


def _track_delhivery(awb):
    if _d1_config():
        return _track_delhivery_one(awb)
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
            "Type": "A",
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
    vendor = _field(row, "VendorName", "Vendor", "CourierName", "Carrier", "Forwarder", "ServiceName")
    if vendor and not vendor.replace(" ", "").isdigit():
        shipment["carrierName"] = vendor
    return shipment


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
    if not 8 <= len(awb) <= 40:
        return {
            "ok": False,
            "code": "invalid",
            "message": "Enter a valid AWB or tracking number.",
        }
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
    if failures and len(failures) == len(configured):
        return {
            "ok": False,
            "code": "unavailable",
            "message": "The courier tracking service could not be reached. Please try again in a moment.",
        }
    return {
        "ok": False,
        "code": "not_found",
        "checked": [CARRIER_NAMES[carrier] for carrier in configured],
        "message": "No shipment was found for this tracking number.",
    }
