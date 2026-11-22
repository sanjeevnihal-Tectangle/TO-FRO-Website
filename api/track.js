export default async function handler(request, response) {
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("Access-Control-Allow-Origin", "*");
  response.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  response.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (request.method === "OPTIONS") {
    response.status(204).end();
    return;
  }
  if (request.method !== "POST" && request.method !== "GET") {
    response.status(405).json({ ok: false, code: "invalid", message: "Enter a valid AWB or tracking number." });
    return;
  }

  const awb = readAwb(request);
  if (awb.length < 4 || awb.length > 40) {
    response.status(200).json({ ok: false, code: "invalid", message: "Enter a valid AWB or order ID." });
    return;
  }

  // Shipway is one quick call against the account's own orders, so it is asked first.
  const shipway = await trackShipway(readTyped(request), awb);
  if (shipway && shipway.ok) {
    response.status(200).json(shipway);
    return;
  }

  const delhivery = await trackDelhivery(awb);
  if (delhivery.ok) {
    response.status(200).json(delhivery);
    return;
  }
  if (delhivery.code === "not_found") {
    const courier = await trackCourierWala(awb);
    if (courier.ok) {
      response.status(200).json(courier);
      return;
    }
    response.status(200).json(courier.code === "unavailable" ? unreachable() : missing());
    return;
  }
  const courier = await trackCourierWala(awb);
  response.status(200).json(courier.ok ? courier : unreachable());
}

function readInput(request) {
  const query = request.query && request.query.awb;
  let body = request.body;
  if (typeof body === "string") {
    try {
      body = JSON.parse(body);
    } catch {
      body = {};
    }
  }
  return String((body && body.awb) || query || "");
}

function readAwb(request) {
  return readInput(request).replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

// The order ID as typed, minus a leading "#": Shipway order IDs may contain "-" or "_".
function readTyped(request) {
  return readInput(request).trim().replace(/^#+/, "").trim().slice(0, 60);
}

// ---------- Shipway (orders pushed into Shipway Experience; tracked by order ID only) ----------

const SHIPWAY_URL = "https://shipway.in/api/getOrderShipmentDetails";
const SHIPWAY_TONES = {
  DEL: "ok",
  UND: "bad", RTO: "bad", RTD: "bad", CAN: "bad", LOST: "bad", DMG: "bad",
  INT: "move", OOD: "move", SHP: "move", PKP: "move", OFP: "move", RAD: "move",
};

function shipwayAuth() {
  const username = String(process.env.SHIPWAY_USERNAME || "").trim();
  const password = String(process.env.SHIPWAY_LICENCE_KEY || "").trim();
  return username && password ? { username, password } : null;
}

function pick(row, names) {
  for (const name of names) {
    const text = String((row && row[name]) ?? "").trim();
    if (text && !/^(null|none|n\/a|na|-)$/i.test(text)) return text;
  }
  return "";
}

// Scan history, if Shipway includes it. The documented sample has none, so key names vary.
function shipwayEvents(details) {
  for (const name of ["scan", "scans", "scan_details", "shipment_track_activities", "tracking_history", "history"]) {
    let rows = details[name];
    if (rows && typeof rows === "object" && !Array.isArray(rows)) rows = Object.values(rows);
    if (Array.isArray(rows) && rows.length) {
      return rows.filter((row) => row && typeof row === "object").map((row) => ({
        time: formatTime(pick(row, ["time", "datetime", "date", "scan_time", "updated_at"])),
        description: prettyStatus(pick(row, ["status_detail", "status", "activity", "remark", "remarks", "scan", "message"])),
        location: pick(row, ["location", "city", "scan_location", "place"]),
      })).filter((event) => event.description).sort((left, right) => sortKey(right.time).localeCompare(sortKey(left.time)));
    }
  }
  return [];
}

// "dd-mm-yyyy HH:MM" -> "yyyy-mm-dd HH:MM" so events sort newest first.
function sortKey(time) {
  const match = String(time || "").match(/^(\d{2})-(\d{2})-(\d{4})(.*)$/);
  return match ? `${match[3]}-${match[2]}-${match[1]}${match[4]}` : String(time || "");
}

// Looks up a Shipway shipment by order ID (field "order_id") or AWB (field "awbno").
async function shipwayOrder(auth, value, field) {
  const result = await fetch(SHIPWAY_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ username: auth.username, password: auth.password, [field]: value }),
  });
  if (result.status >= 500) return { ok: false, code: "unavailable" };
  const data = await result.json().catch(() => null);
  if (!data || typeof data !== "object") return { ok: false, code: "unavailable" };
  if (String(data.status || "").toLowerCase() !== "success") {
    const message = String(data.msg || data.message || "").toLowerCase();
    if (result.status === 401 || /password|email|licen/.test(message)) return { ok: false, code: "unavailable" };
    return { ok: false, code: "not_found" };
  }
  const details = Array.isArray(data.response) ? data.response[0] : data.response;
  if (!details || typeof details !== "object") return { ok: false, code: "not_found" };
  const waybill = pick(details, ["awbno", "awb", "tracking_number"]);
  const code = pick(details, ["current_status_code"]).toUpperCase();
  const status = pick(details, ["current_status"]) || "Status update";
  if (!waybill && (!code || code === "NFI") && /^(status update|no information yet)$/i.test(status)) {
    return { ok: false, code: "not_found" };
  }
  let events = shipwayEvents(details);
  const stamp = pick(details, ["time", "pickup_date"]);
  if (!events.length && stamp) {
    events = [{ time: formatTime(stamp), description: status, location: pick(details, ["to", "from"]) }];
  }
  const tone = SHIPWAY_TONES[code] || toneOf(status);
  const extra = details.extra_fields && typeof details.extra_fields === "object" ? details.extra_fields : {};
  const orderId = pick(details, ["order_id"]) || (field === "order_id" ? value : "");
  const carrier = pick(details, ["carrier", "courier_name", "courier"]);
  return {
    ok: true,
    shipment: {
      carrier: "shipway",
      carrierName: /^fedex$/i.test(carrier) ? "FedEx" : carrier || "Shipway",
      awb: waybill || value,
      orderId: orderLabel(orderId, waybill || value),
      status,
      tone,
      location: pick(details, ["current_location", "location"]) || (events[0] && events[0].location) || "",
      eta: tone === "ok" ? "" : formatTime(pick(extra, ["expected_delivery_date"]) || pick(details, ["expected_delivery_date", "edd", "etd"])),
      events: events.slice(0, 40),
    },
  };
}

async function trackShipway(typed, cleaned) {
  const auth = shipwayAuth();
  if (!auth) return null;
  const tried = new Set();
  let reached = false;
  const values = [typed, cleaned].filter((value) => value && !tried.has(value) && tried.add(value));
  // Try the input as an order ID first, then as an AWB.
  for (const field of ["order_id", "awbno"]) {
    for (const value of values) {
      try {
        const found = await shipwayOrder(auth, value, field);
        if (found.ok) return found;
        if (found.code === "not_found") reached = true;
      } catch {
        /* Shipway unreachable: fall through to the other carriers */
      }
    }
  }
  return { ok: false, code: reached ? "not_found" : "unavailable" };
}

let delhiveryToken = { value: "", expires: 0 };

function delhiveryConfig() {
  const names = ["D1_CLIENT_ID", "D1_CLIENT_SECRET", "D1_AUTH_URL", "D1_REALM", "D1_CLIENT_CMS", "D1_MCP_URL"];
  const config = {};
  for (const name of names) {
    const value = String(process.env[name] || "").trim();
    if (!value) return null;
    config[name] = value;
  }
  return config;
}

async function trackDelhivery(awb) {
  const account = await trackDelhiveryAccount(awb);
  const web = await trackDelhiveryPublic(awb);
  if (account && account.ok) {
    if (web && web.ok && web.shipment.eta) account.shipment.eta = web.shipment.eta;
    if (web && web.ok && web.shipment.events.length > account.shipment.events.length) {
      account.shipment.events = web.shipment.events;
    }
    return account;
  }
  return web;
}

async function trackDelhiveryAccount(awb) {
  const config = delhiveryConfig();
  if (!config) return null;
  try {
    const details = await delhiveryDetails(config, awb);
    if (!details || !details.awb_number) return { ok: false, code: "not_found" };
    const waybill = String(details.awb_number).trim();
    let history = {};
    try {
      history = await delhiveryTool(config, "get-awb-transit-history", { awb_number: waybill });
    } catch {
      history = {};
    }
    const rows = Array.isArray(history && history.transit_history) ? history.transit_history.slice() : [];
    rows.sort((left, right) => String(right.transit_at_string || right.transit_at || "").localeCompare(String(left.transit_at_string || left.transit_at || "")));
    const events = rows.map((item) => {
      const description = String(item.status || item.remarks || "").trim();
      if (!description) return null;
      return {
        time: formatTime(item.transit_at_string || item.transit_at || ""),
        description,
        location: scanPlace(description),
      };
    }).filter(Boolean);
    const status = prettyStatus(details.unmapped_shipment_status || details.shipment_status || "");
    const tone = toneOf(status);
    return {
      ok: true,
      shipment: {
        carrier: "delhivery",
        carrierName: "Delhivery",
        awb: waybill,
        orderId: orderLabel(details.order_number, waybill),
        status: status || "Status update",
        tone,
        location: scanPlace(details.last_update) || (events[0] && events[0].location) || "",
        eta: tone === "ok" ? "" : epochText(details.estimated_delivery_date || details.promised_delivery_date),
        events: events.slice(0, 40),
      },
    };
  } catch (error) {
    if (error && error.code === "not_found") return { ok: false, code: "not_found" };
    return null;
  }
}

async function delhiveryDetails(config, awb) {
  try {
    return await delhiveryTool(config, "get-shipment-details-by-awb", { awb_number: awb });
  } catch (error) {
    if (!(error && error.code === "not_found")) throw error;
    // Order IDs are often all digits (e.g. 88806958), so any unmatched number is also searched as an order ID.
    const match = await findDelhiveryOrder(config, awb);
    const waybill = (match && match.awb_number) || (await findUnshippedOrder(config, awb));
    if (!waybill) return null;
    return delhiveryTool(config, "get-shipment-details-by-awb", { awb_number: String(waybill) });
  }
}

async function findDelhiveryOrder(config, orderId) {
  const rows = await searchShipments(config, orderId, 5);
  const term = orderId.toUpperCase();
  return rows.find((row) => String((row && row.order_number) || "").toUpperCase() === term) || null;
}

// Order number -> AWB pairs seen while scanning (kept while this server instance is warm).
// Delhivery's firewall blocks bursts of requests (403), so scans are small, run one at a
// time, and never re-read an AWB whose order is already known.
const orderAwbs = new Map();
const knownSerials = new Set();
const missedOrders = new Map();
let scanQueue = Promise.resolve();
const SCAN_WORKERS = 4;
const SCAN_BACK = 30;
const SCAN_AHEAD_MAX = 80;
const SCAN_GAP = 6;
const MISS_TTL_MS = 10 * 60 * 1000;

// Delhivery AWBs end in a check digit: (serial - 1) mod 7.
function awbFromSerial(serial) {
  return `${serial}${(serial - 1) % 7}`;
}

async function orderOfAwb(config, awb) {
  try {
    const details = await delhiveryTool(config, "get-shipment-details-by-awb", { awb_number: awb });
    if (!details || !details.awb_number) return "";
    const orderNumber = String(details.order_number || "").trim().toUpperCase();
    if (orderNumber) {
      orderAwbs.set(orderNumber, String(details.awb_number));
      knownSerials.add(Math.floor(Number(awb) / 10));
    }
    return orderNumber;
  } catch (error) {
    if (error && error.code === "not_found") return "";
    throw error;
  }
}

// Reads the AWBs for these serials. Returns { matched, hits } where hits marks used AWBs.
async function scanSerials(config, serials, term) {
  const todo = serials.filter((serial) => !knownSerials.has(serial));
  const found = await Promise.all(todo.map((serial) => orderOfAwb(config, awbFromSerial(serial))));
  if (found.includes(term)) return { matched: true, hits: [] };
  const hitBySerial = new Map(todo.map((serial, index) => [serial, Boolean(found[index])]));
  return { matched: false, hits: serials.map((serial) => (hitBySerial.has(serial) ? hitBySerial.get(serial) : true)) };
}

// Delhivery's search only lists parcels that have been picked up. This account's AWBs are
// issued in sequence, so AWBs near the newest shipped one are read directly and matched on order number.
function findUnshippedOrder(config, orderId) {
  const run = scanQueue.then(() => scanForOrder(config, orderId));
  scanQueue = run.catch(() => {});
  return run;
}

async function scanForOrder(config, orderId) {
  const term = orderId.toUpperCase();
  if (orderAwbs.has(term)) return orderAwbs.get(term);
  if (Date.now() - (missedOrders.get(term) || 0) < MISS_TTL_MS) return "";
  const latest = await searchShipments(config, "", 20);
  const serials = latest
    .map((row) => String((row && row.awb_number) || ""))
    .filter((awb) => /^\d{10,}$/.test(awb))
    .map((awb) => Math.floor(Number(awb) / 10));
  if (!serials.length) return "";
  const anchor = Math.max(...serials);
  let ahead = 1;
  let misses = 0;
  while (ahead <= SCAN_AHEAD_MAX && misses < SCAN_GAP) {
    const batch = Array.from({ length: SCAN_WORKERS }, (_, index) => anchor + ahead + index);
    const { matched, hits } = await scanSerials(config, batch, term);
    if (matched) return orderAwbs.get(term);
    for (const hit of hits) misses = hit ? 0 : misses + 1;
    ahead += SCAN_WORKERS;
  }
  for (let start = 0; start < SCAN_BACK; start += SCAN_WORKERS) {
    const batch = Array.from({ length: SCAN_WORKERS }, (_, index) => anchor - start - index);
    const { matched } = await scanSerials(config, batch, term);
    if (matched) return orderAwbs.get(term);
  }
  missedOrders.set(term, Date.now());
  return "";
}

async function searchShipments(config, term, pageSize) {
  const stamp = "2020-01-01T00:00:00+05:30";
  const data = await delhiveryTool(config, "shipment-search", {
    search_on: ["oid"],
    search_term: term,
    search_type: "CONTAINS",
    page: 1,
    page_size: pageSize,
    field: "manifested_at",
    direction: "DESC",
    op: "GTE",
    value: stamp,
    range_updated_at_op: "GTE",
    range_updated_at_value: stamp,
    range_manifested_at_op: "GTE",
    range_manifested_at_value: stamp,
    range_estimated_delivery_date_op: "GTE",
    range_estimated_delivery_date_value: stamp,
    range_shipped_at_op: "GTE",
    range_shipped_at_value: stamp,
    range_dispatch_count_op: "GTE",
    range_dispatch_count_value: 0,
  });
  return Array.isArray(data && data.results) ? data.results.filter((row) => row && typeof row === "object") : [];
}

function orderLabel(orderId, awb) {
  const text = String(orderId || "").trim();
  return text && text.toUpperCase() !== String(awb || "").toUpperCase() ? text : "";
}

async function delhiveryTool(config, name, arguments_) {
  const token = await delhiveryAccessToken(config);
  const result = await fetch(config.D1_MCP_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "x-hq-client-id": config.D1_CLIENT_CMS,
      "X-UCP-User-Email": "--",
      "X-UCP-Realm": config.D1_REALM,
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name, arguments: arguments_ },
    }),
  });
  if (!result.ok) throw new Error("unavailable");
  const data = JSON.parse(mcpText(await result.text()) || "{}");
  if (data && data.error_code === "NOT_FOUND") {
    const error = new Error("not_found");
    error.code = "not_found";
    throw error;
  }
  if (data && data.error_code) throw new Error("unavailable");
  return data;
}

async function delhiveryAccessToken(config) {
  if (delhiveryToken.value && delhiveryToken.expires > Date.now()) return delhiveryToken.value;
  const body = new URLSearchParams({
    grant_type: "client_credentials",
    client_id: config.D1_CLIENT_ID,
    client_secret: config.D1_CLIENT_SECRET,
  });
  const result = await fetch(`${config.D1_AUTH_URL.replace(/\/$/, "")}/realms/${config.D1_REALM}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  const payload = await result.json().catch(() => ({}));
  if (!result.ok || !payload.access_token) throw new Error("unavailable");
  delhiveryToken = {
    value: payload.access_token,
    expires: Date.now() + Math.max(30, Number(payload.expires_in || 300) - 60) * 1000,
  };
  return delhiveryToken.value;
}

function mcpText(text) {
  for (const line of String(text || "").split(/\r?\n/)) {
    if (!line.startsWith("data: ")) continue;
    const message = JSON.parse(line.slice(6));
    const content = ((message.result || {}).content || [{}])[0];
    return content.text || "";
  }
  return text;
}

function scanPlace(text) {
  const raw = String(text || "");
  const at = raw.lastIndexOf(" at ");
  return at > 0 ? raw.slice(at + 4).trim() : "";
}

function epochText(value) {
  const number = Number(value);
  if (!number) return "";
  const moment = new Date(number > 10000000000 ? number : number * 1000);
  if (Number.isNaN(moment.getTime())) return "";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(moment);
}

async function trackDelhiveryPublic(awb) {
  let payload;
  try {
    const result = await fetch(`https://dlv-api.delhivery.com/v3/unified-tracking-new?wbn=${encodeURIComponent(awb)}`, {
      headers: {
        Accept: "application/json",
        Origin: "https://www.delhivery.com",
        Referer: "https://www.delhivery.com/",
        "User-Agent": "Mozilla/5.0",
      },
    });
    if (result.status === 400 || result.status === 404) return { ok: false, code: "not_found" };
    if (!result.ok) return { ok: false, code: "unavailable" };
    payload = await result.json();
  } catch {
    return { ok: false, code: "unavailable" };
  }
  const item = payload && payload.data && payload.data[0];
  if (!item) return { ok: false, code: "not_found" };
  const current = item.status || {};
  const headline = prettyStatus(current.status || current.instructions || "");
  const events = [];
  for (const state of item.trackingStates || []) {
    for (const scan of (state && state.scans) || []) {
      const description = String(scan.scanNslRemark || scan.scan || current.instructions || "").trim();
      if (!description) continue;
      events.push({
        time: formatTime(scan.scanDateTime || scan.scanDate || current.statusDateTime || ""),
        description,
        location: String(scan.scannedLocation || scan.cityLocation || "").trim(),
      });
    }
  }
  if (!events.length && headline) {
    events.push({
      time: formatTime(current.statusDateTime || ""),
      description: String(current.instructions || headline).trim(),
      location: "",
    });
  }
  const tone = toneOf(headline);
  return {
    ok: true,
    shipment: {
      carrier: "delhivery",
      carrierName: "Delhivery",
      awb: String(item.awb || awb).trim(),
      status: headline || "Status update",
      tone,
      location: (events[0] && events[0].location) || "",
      eta: tone === "ok" ? "" : formatTime(item.promiseDeliveryDate || item.deliveryDate || ""),
      events: events.slice(0, 40),
    },
  };
}

async function trackCourierWala(awb) {
  const byAwb = await trackCourierWalaQuery(awb, "A");
  if (byAwb.ok || byAwb.code !== "not_found") return byAwb;
  return trackCourierWalaQuery(awb, "R");
}

async function trackCourierWalaQuery(awb, queryType) {
  let payload;
  try {
    const result = await fetch("https://xpresion.courierwalaexpress.in/api/v1/Tracking/Tracking", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        UserID: "CUSTRC",
        Password: "83D54B1C37",
        AWBNo: awb,
        Type: queryType,
        RequiredUrl: "yes",
        ShowAllFields: "yes",
      }),
    });
    if (!result.ok) return { ok: false, code: "unavailable" };
    payload = await result.json();
  } catch {
    return { ok: false, code: "unavailable" };
  }
  const data = (payload && payload.Response) || {};
  const rows = Array.isArray(data.Tracking) ? data.Tracking : data.Tracking ? [data.Tracking] : [];
  const disc = String(data.ErrorDisc || "");
  if (!/success/i.test(disc) || !rows.length) {
    if (/not exist|not found|no record|no data/i.test(disc)) return missing();
    return { ok: false, code: "unavailable" };
  }
  const row = rows[0];
  const events = (Array.isArray(data.Events) ? data.Events : []).map((item) => {
    const description = String((item && (item.Status || item.Remark)) || "").trim();
    if (!description) return null;
    return {
      time: [formatTime(item.EventDate || ""), clockText(item.EventTime || "")].filter(Boolean).join(" "),
      description,
      location: String(item.Location || item.City || "").trim(),
    };
  }).filter(Boolean);
  const status = String(row.Status || (events[0] && events[0].description) || "Status update").trim();
  const vendor = String(row.VendorName || row.Vendor || "").trim();
  const tone = toneOf(status);
  return {
    ok: true,
    shipment: {
      carrier: "courierwala",
      carrierName: vendor && !/^\d+$/.test(vendor) ? vendor : "Courier Wala",
      awb: String(row.AWBNo || awb).trim(),
      orderId: orderLabel(row.RefNo || row.OrderNo || row.OrderID, row.AWBNo || awb),
      status,
      tone,
      location: (events[0] && events[0].location) || String(row.Destination || "").trim(),
      eta: tone === "ok" ? "" : formatTime(row.ExpectedDeliveryDate || ""),
      events: events.slice(0, 40),
    },
  };
}

function prettyStatus(value) {
  const text = String(value || "").trim();
  if (text && text === text.toUpperCase()) {
    return text.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
  }
  return text;
}

function formatTime(value) {
  const text = String(value || "").trim();
  let match = text.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (match) return `${match[3]}-${match[2]}-${match[1]} ${match[4]}:${match[5]}`;
  match = text.match(/^(\d{2})[/-](\d{2})[/-](\d{4})/);
  return match ? `${match[1]}-${match[2]}-${match[3]}` : text;
}

function clockText(value) {
  const match = String(value || "").trim().match(/^(\d{2})(\d{2})(?:\d{2})?$/);
  return match ? `${match[1]}:${match[2]}` : String(value || "").trim();
}

function toneOf(status) {
  const text = String(status || "").toLowerCase();
  if (/deliver|signed/.test(text)) return "ok";
  if (/fail|exception|undeliver|return|cancel|refused/.test(text)) return "bad";
  if (/transit|out for|pickup|picked|depart|arriv|customs|manifest/.test(text)) return "move";
  return "wait";
}

function missing() {
  return { ok: false, code: "not_found", message: "No shipment was found for this AWB or order ID." };
}

function unreachable() {
  return { ok: false, code: "unavailable", message: "The courier tracking service could not be reached. Please try again in a moment." };
}
