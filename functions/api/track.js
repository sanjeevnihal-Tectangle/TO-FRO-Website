export async function onRequest(context) {
  const request = context.request;
  if (request.method === "OPTIONS") {
    return json(204, null);
  }
  if (request.method !== "POST" && request.method !== "GET") {
    return json(405, { ok: false, code: "invalid", message: "Enter a valid AWB or tracking number." });
  }
  let awb = "";
  if (request.method === "GET") {
    awb = new URL(request.url).searchParams.get("awb") || "";
  } else {
    const body = await request.json().catch(() => ({}));
    awb = body.awb || "";
  }
  awb = String(awb).replace(/[^A-Za-z0-9]/g, "").toUpperCase();
  if (awb.length < 8 || awb.length > 40) {
    return json(200, { ok: false, code: "invalid", message: "Enter a valid AWB or tracking number." });
  }
  const delhivery = await trackDelhivery(awb);
  if (delhivery && delhivery.ok) return json(200, delhivery);
  const courier = await trackCourierWala(awb);
  if (courier && courier.ok) return json(200, courier);
  if (delhivery && delhivery.code === "unavailable" && courier && courier.code === "unavailable") {
    return json(200, {
      ok: false,
      code: "unavailable",
      message: "The courier tracking service could not be reached. Please try again in a moment.",
    });
  }
  return json(200, { ok: false, code: "not_found", message: "No shipment was found for this tracking number." });
}

async function trackDelhivery(awb) {
  let payload;
  try {
    const response = await fetch(`https://dlv-api.delhivery.com/v3/unified-tracking-new?wbn=${encodeURIComponent(awb)}`, {
      headers: {
        Accept: "application/json",
        Origin: "https://www.delhivery.com",
        Referer: "https://www.delhivery.com/",
        "User-Agent": "Mozilla/5.0",
      },
    });
    if (!response.ok) return { ok: false, code: "unavailable" };
    payload = await response.json();
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
      eta: tone === "ok" ? "" : formatTime(item.promiseDeliveryDate || item.deliveryDateText || ""),
      events: events.slice(0, 12),
    },
  };
}

async function trackCourierWala(awb) {
  let payload;
  try {
    const response = await fetch("https://xpresion.courierwalaexpress.in/api/v1/Tracking/Tracking", {
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
    if (!response.ok) return { ok: false, code: "unavailable" };
    payload = await response.json();
  } catch {
    return { ok: false, code: "unavailable" };
  }
  const data = (payload && payload.Response) || {};
  const rows = Array.isArray(data.Tracking) ? data.Tracking : data.Tracking ? [data.Tracking] : [];
  if (!/success/i.test(String(data.ErrorDisc || "")) || !rows.length) {
    return { ok: false, code: "not_found" };
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
  if (text && text === text.toUpperCase()) return text.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
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

function json(status, payload) {
  return new Response(payload == null ? null : JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    },
  });
}
