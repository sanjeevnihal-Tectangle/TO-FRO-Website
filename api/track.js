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
  if (awb.length < 8 || awb.length > 40) {
    response.status(200).json({ ok: false, code: "invalid", message: "Enter a valid AWB or tracking number." });
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

function readAwb(request) {
  const query = request.query && request.query.awb;
  let body = request.body;
  if (typeof body === "string") {
    try {
      body = JSON.parse(body);
    } catch {
      body = {};
    }
  }
  const source = (body && body.awb) || query || "";
  return String(source).replace(/[^A-Za-z0-9]/g, "").toUpperCase();
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
    const details = await delhiveryTool(config, "get-shipment-details-by-awb", { awb_number: awb });
    if (!details || !details.awb_number) return { ok: false, code: "not_found" };
    let history = {};
    try {
      history = await delhiveryTool(config, "get-awb-transit-history", { awb_number: awb });
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
        awb: String(details.awb_number || awb).trim(),
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
  let payload;
  try {
    const result = await fetch("https://xpresion.courierwalaexpress.in/api/v1/Tracking/Tracking", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        UserID: "CUSTRC",
        Password: "83D54B1C37",
        AWBNo: awb,
        Type: "A",
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
  return { ok: false, code: "not_found", message: "No shipment was found for this tracking number." };
}

function unreachable() {
  return { ok: false, code: "unavailable", message: "The courier tracking service could not be reached. Please try again in a moment." };
}
