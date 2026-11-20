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

async function trackDelhivery(awb) {
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
      events: events.slice(0, 12),
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
      events: events.slice(0, 12),
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
