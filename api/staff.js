import crypto from "crypto";
import {
  trackShipway,
  trackDelhivery,
  trackCourierWala,
  delhiveryConfig,
  delhiveryTool,
  searchShipments,
  missing,
  unreachable,
} from "./track.js";

const COURIER_LABELS = {
  origin: "Origin",
  destination: "Destination",
  consignee: "Consignee",
  receivername: "Receiver",
  receiver: "Receiver",
  receivertelephone: "Receiver phone",
  receivermobile: "Receiver phone",
  receiveraddress: "Receiver address",
  receiveraddress1: "Receiver address",
  shipper_name: "Shipper",
  shippername: "Shipper",
  shipperaddress: "Shipper address",
  shipperaddress1: "Shipper address",
  vendorname: "Handed to",
  vendor: "Handed to",
  vendorawbno1: "Vendor AWB",
  vendorawbno2: "Vendor AWB 2",
  weight: "Weight",
  actualweight: "Weight",
  chargedweight: "Charged weight",
  pcs: "Pieces",
  pieces: "Pieces",
  nop: "Pieces",
  service: "Service",
  servicename: "Service",
  goodsdesc: "Contents",
  description: "Contents",
  product: "Contents",
  content: "Contents",
  invoicevalue: "Invoice value",
  invoiceno: "Invoice number",
  bookingdate: "Booked on",
};

export default async function handler(request, response) {
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("Access-Control-Allow-Origin", "*");
  response.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  response.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (request.method === "OPTIONS") {
    response.status(204).end();
    return;
  }
  if (request.method !== "POST") {
    response.status(405).json({ ok: false, code: "invalid", message: "Unknown request." });
    return;
  }
  const body = readBody(request);
  if (body.action === "login") {
    response.status(200).json(staffLogin(body.email, body.password));
    return;
  }
  if (body.action === "track") {
    if (!staffAuthorized(body.token)) {
      response.status(200).json({ ok: false, code: "denied", message: "Please sign in again." });
      return;
    }
    response.status(200).json(await staffTrack(body.awb));
    return;
  }
  response.status(404).json({ ok: false, code: "invalid", message: "Unknown request." });
}

function readBody(request) {
  let body = request.body;
  if (typeof body === "string") {
    try {
      body = JSON.parse(body);
    } catch {
      body = {};
    }
  }
  return body && typeof body === "object" ? body : {};
}

function staffAccount() {
  return {
    email: String(process.env.STAFF_EMAIL || "tonfrologistics@gmail.com").trim().toLowerCase(),
    password: String(process.env.STAFF_PASSWORD || "TonFro@2026"),
  };
}

function same(left, right) {
  const a = crypto.createHash("sha256").update(String(left)).digest();
  const b = crypto.createHash("sha256").update(String(right)).digest();
  return crypto.timingSafeEqual(a, b);
}

function sign(payload, password) {
  return crypto.createHmac("sha256", String(password)).update(String(payload)).digest("hex");
}

function staffLogin(email, password) {
  const account = staffAccount();
  if (!same(String(email || "").trim().toLowerCase(), account.email) || !same(password || "", account.password)) {
    return { ok: false, code: "denied", message: "That email or password is not correct." };
  }
  const expires = Math.floor(Date.now() / 1000) + 12 * 60 * 60;
  const payload = `${account.email}|${expires}`;
  return { ok: true, token: `${payload}|${sign(payload, account.password)}`, email: account.email };
}

function staffAuthorized(token) {
  const account = staffAccount();
  const parts = String(token || "").split("|");
  if (parts.length !== 3) return false;
  const [email, expires, signature] = parts;
  if (email !== account.email) return false;
  if (!/^\d+$/.test(expires) || Number(expires) < Math.floor(Date.now() / 1000)) return false;
  const expected = sign(`${email}|${expires}`, account.password);
  const left = Buffer.from(expected);
  const right = Buffer.from(String(signature));
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

function cleaned(value) {
  return String(value || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

async function staffTrack(raw) {
  const typed = String(raw || "").trim().replace(/^#+/, "").trim().slice(0, 60);
  const awb = cleaned(raw);
  if (awb.length < 4 || awb.length > 40) {
    return { ok: false, code: "invalid", message: "Enter a valid AWB or order ID." };
  }
  const found = await lookup(typed, awb);
  if (!found.ok) return found;
  try {
    found.shipment.facts = await staffFacts(found.shipment);
  } catch {
    found.shipment.facts = [];
  }
  return found;
}

async function lookup(typed, awb) {
  const shipway = await trackShipway(typed, awb);
  if (shipway && shipway.ok) return shipway;
  const delhivery = await trackDelhivery(awb);
  if (delhivery.ok) return delhivery;
  if (delhivery.code === "not_found") {
    const courier = await trackCourierWala(awb);
    if (courier.ok) return courier;
    return courier.code === "unavailable" ? unreachable() : missing();
  }
  const courier = await trackCourierWala(awb);
  return courier.ok ? courier : unreachable();
}

async function staffFacts(shipment) {
  const facts = [];
  const seen = new Set();
  const add = (label, value) => {
    const text = textOf(value);
    if (!text || seen.has(label)) return;
    seen.add(label);
    facts.push({ label, value: text });
  };
  add("Carrier", shipment.carrierName);
  const rich = await delhiveryRow(shipment);
  if (rich) delhiveryFacts(add, rich);
  const history = await delhiveryHistory(rich && rich.awb_number ? rich.awb_number : shipment.awb);
  if (history) {
    add("Consignee", history.consignee_name);
    add("Consignee phone", history.consignee_phone);
    if (history.delivery_attempts !== undefined && history.delivery_attempts !== null && history.delivery_attempts !== "") {
      add("Delivery attempts", history.delivery_attempts);
    }
    add("Delivered on", epochText(history.delivered_at) || history.delivered_at_string);
  }
  if (shipment.carrier === "courierwala") {
    const row = await courierRow(shipment.awb);
    if (row) courierFacts(add, row);
  }
  if (!facts.some((item) => item.label === "Destination")) add("Current location", shipment.location);
  if (shipment.eta) add("Estimated delivery", shipment.eta);
  return facts.slice(0, 40);
}

async function delhiveryRow(shipment) {
  const config = delhiveryConfig();
  if (!config) return null;
  const awb = String(shipment.awb || "").trim();
  const orderId = String(shipment.orderId || "").trim();
  const queries = [];
  if (awb) queries.push([["wbn"], awb]);
  if (orderId) queries.push([["oid"], orderId]);
  if (awb && awb.toUpperCase() !== orderId.toUpperCase()) queries.push([["oid"], awb]);
  for (const [searchOn, term] of queries) {
    try {
      const rows = await searchShipments(config, term, 5, searchOn);
      const exact = rows.find((row) => {
        const rowAwb = String(row.awb_number || "").toUpperCase();
        const rowOrder = String(row.order_number || "").toUpperCase();
        return (awb && rowAwb === awb.toUpperCase()) || (orderId && rowOrder === orderId.toUpperCase());
      });
      if (exact) return exact;
      if (rows.length === 1) return rows[0];
    } catch {
      /* try the next search */
    }
  }
  return null;
}

async function delhiveryHistory(awb) {
  const config = delhiveryConfig();
  if (!config || !awb) return null;
  try {
    const history = await delhiveryTool(config, "get-awb-transit-history", { awb_number: String(awb) });
    return history && typeof history === "object" ? history : null;
  } catch {
    return null;
  }
}

function delhiveryFacts(add, row) {
  const customer = [textOf(row.customer_first_name), textOf(row.customer_last_name)].filter(Boolean).join(" ");
  add("Customer", customer);
  add("Customer phone", row.customer_phone);
  add("Consignee", addressText(row.shipping_address));
  add("Billing address", addressText(row.billing_address));
  add("Origin", addressText(row.origin_address));
  add("Destination", row.destination);
  add("Pickup location", row.pickup_location_name);
  add("Seller", row.seller_name || addressText(row.seller_address));
  add("Return address", addressText(row.return_address));
  add("Contents", row.product_description);
  add("Payment", pretty(row.payment_mode));
  const currency = textOf(row.currency) || "INR";
  const amount = textOf(row.grand_total_value);
  if (amount) add("Shipment value", `${currency} ${amount}`);
  const cod = textOf(row.cod_amount);
  if (cod) add("COD amount", `${currency} ${cod}`);
  add("Shipment type", pretty(row.shipment_type));
  add("Package status", pretty(row.package_status || row.package_status_type));
  const fragile = String(row.contains_fragile_items || "").toUpperCase();
  if (fragile === "ON" || fragile === "OFF") add("Fragile", fragile === "ON" ? "Yes" : "No");
  const weight = textOf(row.client_total_weight);
  if (weight) add("Weight", `${weight} g`);
  add("Quantity", row.item_quantity || row.box_count);
  const boxes = Array.isArray(row.box_list) ? row.box_list.map(boxText).filter(Boolean).join("; ") : "";
  add("Package size", boxes);
  add("Zone", row.zone);
  add("Delivery attempts", row.delivery_attempt_count);
  add("NDR status", pretty(row.ndr_status));
  const score = textOf(row.rto_score);
  if (score && score.toUpperCase() !== "NONE") add("RTO score", pretty(score));
  add("Address check", pretty(row.shipping_address_validity));
  add("Invoice number", row.invoice_number);
  add("Invoice date", epochText(row.invoice_date));
  add("E-way bill", row.ewaybill_number);
  add("Promised delivery", epochText(row.promised_delivery_date));
  add("Estimated delivery", epochText(row.estimated_delivery_date));
  add("Picked up", epochText(row.pickup_date));
  add("Delivered on", epochText(row.delivered_date));
  add("Latest update", row.last_update || row.remarks);
  add("Client", row.client_name);
}

function addressText(block) {
  if (!block || typeof block !== "object") return "";
  const name = [textOf(block.first_name), textOf(block.last_name)].filter(Boolean).join(" ");
  const city = [textOf(block.city), textOf(block.state), textOf(block.pin_code || block.pincode)].filter(Boolean).join(", ");
  const country = textOf(block.country);
  return [name, textOf(block.address_line1 || block.address_line || block.address), textOf(block.address_line2), city, country.toUpperCase() === "IN" ? "" : country, textOf(block.phone || block.phone_number), textOf(block.email)]
    .filter(Boolean)
    .join("\n");
}

function boxText(box) {
  if (!box || typeof box !== "object") return "";
  const parts = [];
  const weight = textOf(box.weight);
  if (weight) parts.push(`${weight} ${(textOf(box.weight_unit) || "GM").toLowerCase()}`);
  const length = textOf(box.length);
  const breadth = textOf(box.breadth);
  const height = textOf(box.height);
  if (length && breadth && height) parts.push(`${length} × ${breadth} × ${height} ${(textOf(box.length_unit) || "CM").toLowerCase()}`);
  const mode = pretty(box.shipping_mode);
  if (mode) parts.push(mode);
  return parts.join(", ");
}

async function courierRow(awb) {
  for (const queryType of ["A", "R"]) {
    try {
      const result = await fetch("https://xpresion.courierwalaexpress.in/api/v1/Tracking/Tracking", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          UserID: process.env.COURIERWALA_USER_ID || "CUSTRC",
          Password: process.env.COURIERWALA_PASSWORD || "83D54B1C37",
          AWBNo: awb,
          Type: queryType,
          RequiredUrl: "yes",
          ShowAllFields: "yes",
        }),
      });
      if (!result.ok) continue;
      const payload = await result.json();
      const data = (payload && payload.Response) || {};
      const rows = Array.isArray(data.Tracking) ? data.Tracking : data.Tracking ? [data.Tracking] : [];
      if (/success/i.test(String(data.ErrorDisc || "")) && rows[0]) return rows[0];
    } catch {
      return null;
    }
  }
  return null;
}

function courierFacts(add, row) {
  const folded = {};
  for (const [key, value] of Object.entries(row)) folded[String(key).toLowerCase()] = value;
  for (const [key, label] of Object.entries(COURIER_LABELS)) {
    if (key in folded) add(label, folded[key]);
  }
}

function textOf(value) {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") {
    if (!value) return "";
    if (value > 10000000000) return epochText(value);
    return String(value);
  }
  const text = String(value ?? "").trim();
  if (!text || /^(null|none|n\/a|na|-|0)$/i.test(text)) return "";
  return text;
}

function pretty(value) {
  const text = textOf(value);
  if (text && text === text.toUpperCase()) {
    return text.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
  }
  return text;
}

function epochText(value) {
  const number = Number(value);
  if (!number) return "";
  const moment = new Date(number > 10000000000 ? number : number * 1000);
  if (Number.isNaN(moment.getTime())) return "";
  return new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "numeric" }).format(moment);
}
