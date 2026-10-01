// Capture README screenshots of the review UI with synthetic demo data.
//
// No backend, OCR or LLM is needed: the two fictional invoices in scripts/demo/
// are rendered to PNG, every `.t` span becomes a fake OCR token, and all /api
// calls are answered by Playwright route mocks built from those boxes.
// Writes docs/images/*.png, plus hero.png (scripts/demo/hero.html framing review-invoice.png).
//
// Usage (frontend dev server already running):
//   cd frontend && npx vite --host 127.0.0.1 --port 4210
//   node scripts/capture-screenshots.cjs http://127.0.0.1:4210
// Requires the `playwright` package resolvable (e.g. NODE_PATH=<dir>/node_modules).
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.argv[2] || "http://127.0.0.1:4210";
const ROOT = path.resolve(__dirname, "..");
const OUT = path.join(ROOT, "docs", "images");
const now = "2025-03-14T09:30:00Z";

const poly = (r) => {
  const x1 = Math.round(r.x), y1 = Math.round(r.y);
  const x2 = Math.round(r.x + r.width), y2 = Math.round(r.y + r.height);
  return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]];
};

async function renderDoc(browser, file) {
  const page = await browser.newPage({ viewport: { width: 1240, height: 1754 }, deviceScaleFactor: 1 });
  await page.goto("file:///" + path.join(ROOT, "scripts", "demo", file).replace(/\\/g, "/"));
  await page.evaluate(() => document.fonts.ready);
  const data = await page.evaluate(() => {
    const rect = (el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; };
    const union = (els) => {
      const rs = els.map(rect);
      const x = Math.min(...rs.map((r) => r.x)), y = Math.min(...rs.map((r) => r.y));
      return { x, y, width: Math.max(...rs.map((r) => r.x + r.width)) - x, height: Math.max(...rs.map((r) => r.y + r.height)) - y };
    };
    const tokens = [...document.querySelectorAll(".t")].map((el) => ({ text: el.textContent.trim(), r: rect(el) }));
    const fields = {};
    for (const el of document.querySelectorAll("[data-f]")) (fields[el.dataset.f] ||= []).push(el);
    const fieldRects = Object.fromEntries(Object.entries(fields).map(([k, els]) => [k, union(els)]));
    const rows = [...document.querySelectorAll("tr[data-row]")].map(rect);
    return { tokens, fieldRects, rows };
  });
  const png = await page.screenshot();
  await page.close();
  return {
    png,
    tokens: data.tokens.map((t, i) => ({ text: t.text, bbox: poly(t.r), score: 0.9 + ((i * 7) % 10) / 100 })),
    fields: Object.fromEntries(Object.entries(data.fieldRects).map(([k, r]) => [k, poly(r)])),
    rows: data.rows.map(poly),
  };
}

// ---- synthetic job payloads -------------------------------------------------
function acmeJob(doc) {
  const inv = {
    company_name: "Acme Trading Co.", company_address: "12 Harbor Road, Unit 4, Port Example",
    company_country: "United Arab Emirates", company_phone: "+971 4 000 0000",
    invoice_number: "ACM-2025-0417", invoice_date: "2025-03-14", currency: "USD", total_amount: 4400,
    invoice_type: "Commercial Invoice", customer_name: "Bob's Hardware Supply",
    consignee_name: "Bob's Hardware Warehouse", consignee_address: "88 Sample Street, Testville",
    exporter_name: null, exporter_address: null, exporter_tel: null, payable_to: null, incoterm: "FOB",
    total_quantity_carton: null, total_quantity_box: null, total_aggregate_amount: null,
    deposit_received: null, deposit_date: null, remain_money: null,
    payment_terms: "30% deposit, balance before shipment", packing: null, remarks: [], extra_notes: [],
    bank: {
      bank_name: "Example National Bank", swift_code: "EXNBAEXX", bank_country: "United Arab Emirates",
      address: null, beneficiary: "Acme Trading Co.", account_number: null,
      accounts: [{ currency: "USD", account_number: null, iban: "AE00 0000 0000 0000 0000 000", extra: null }],
      notices: [],
    },
    line_items: [],
  };
  const items = [
    ["Stainless steel hinges, 100 mm", "Hardware", "piece", "pcs", 1200, 0.85, 1020],
    ["Brass cabinet handles, satin", "Hardware", "piece", "pcs", 600, 2.4, 1440],
    ["Wood screws 4x40, zinc", "Fasteners", "piece", "box", 80, 6.5, 520],
    ["Drawer slides, soft-close 450 mm", "Hardware", "piece", "pair", 150, 7.9, 1185],
  ].map(([product_name, category, count_type, unit, quantity, unit_price, line_total], i) => ({
    no: i + 1, product_name, category, count_type, unit, quantity, unit_price, line_currency: null, line_total,
    size: null, colour: null, weight_per_box: null, box_per_carton: null, piece_per_box: null,
    quantity_carton: null, quantity_box: null, fob_unit_price_usd: null, aggregate_amount: null,
  }));
  inv.line_items = items;
  // Demo linking scores: mostly strong, a few medium, one deliberately unlinked.
  const scores = { company_address: 78, consignee_address: 74, incoterm: 66, payment_terms: 81 };
  const get = (p) => p.split(".").reduce((o, k) => (o == null ? null : o[k]), inv);
  const paths = Object.keys(doc.fields).concat(["bank.bank_country"]);
  const fields = paths.map((p) => ({
    field_path: p, value: get(p), page: doc.fields[p] ? 1 : null, bbox: doc.fields[p] || null,
    score: doc.fields[p] ? scores[p] ?? 96 + (p.length % 4) : 0, edited: false,
  }));
  return detail("acme", "acme-trading-invoice.pdf", "ready", "invoice", null, {
    kind: "invoice", invoice: inv, fields,
    line_items: items.map((data, i) => ({ row_index: i, data, page: 1, bbox: doc.rows[i], edited: false })),
    pages: [page("acme")],
  });
}

function faJob(doc) {
  const data = {
    invoice: { number: "۱۴۰۴-۰۲۱۸", date: "۱۴۰۴/۰۲/۱۸" },
    seller: { name: "شرکت بازرگانی آلفا (نمونه)", address: "تهران، خیابان نمونه، پلاک ۱۲", phone: "۰۲۱-۰۰۰۰۰۰۰۰" },
    buyer: { name: "فروشگاه لوازم خانگی بتا", address: "اصفهان، بلوار آزمایشی، واحد ۳" },
    items: [
      { description: "کتری برقی استیل", quantity: 20 },
      { description: "چای\u200cساز رومیزی", quantity: 12 },
      { description: "توستر دو خانه", quantity: 8 },
    ],
    total_rial: "۷۷۸٬۰۰۰٬۰۰۰ ریال",
  };
  const get = (p) => p.replace(/\[(\d+)\]/g, ".$1").split(".").reduce((o, k) => o?.[k], data);
  const fields = Object.entries(doc.fields).map(([p, bbox]) => ({
    field_path: p, value: get(p), page: 1, bbox, score: p.includes("address") ? 79 : 94, edited: false,
  }));
  return {
    ...detail("fa", "faktor-foroosh-1404.png", "ready", "prompt", "json", { kind: "prompt_json", data, fields, pages: [page("fa")] }),
  };
}

function page(id) {
  return { page: 1, image_url: `/api/jobs/${id}/page/1.png`, width: 1240, height: 1754 };
}
function detail(id, source_filename, status, mode, output_format, result, progress_pct = 100) {
  return { id, source_filename, page_count: 1, status, progress_pct, mode, output_format, error: null,
    created_at: now, updated_at: now, result };
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const acme = await renderDoc(browser, "acme-invoice.html");
  const fa = await renderDoc(browser, "fa-invoice.html");

  const jobs = {
    acme: acmeJob(acme),
    fa: faJob(fa),
    fresh: detail("fresh", "acme-trading-invoice.pdf", "created", "prompt", null, null, 0),
    running: detail("running", "acme-trading-invoice.pdf", "extract", "invoice", null, null, 62),
  };
  const pngs = { acme: acme.png, fa: fa.png, fresh: acme.png, running: acme.png };
  const ocr = { acme: acme.tokens, fa: fa.tokens };
  const list = [
    { ...jobs.acme, created_at: "2025-03-14T09:30:00Z" },
    { ...jobs.fa, created_at: "2025-03-13T15:05:00Z" },
    { id: "q3", source_filename: "globex-packing-list.pdf", page_count: 3, status: "approved", progress_pct: 100, created_at: "2025-03-12T11:20:00Z" },
    { id: "q4", source_filename: "initech-receipt-scan.jpg", page_count: 1, status: "failed", progress_pct: 40, created_at: "2025-03-11T08:45:00Z" },
  ].map(({ result, ...s }) => s);

  async function mock(ctx) {
    // Match on pathname: a "**/api/**" glob would also swallow Vite's /src/api/*.ts modules.
    await ctx.route((u) => u.pathname.startsWith("/api/"), async (route) => {
      const req = route.request();
      const u = new URL(req.url());
      const m = u.pathname.match(/^\/api\/jobs(?:\/([^/]+))?(?:\/(.*))?$/);
      const json = (body) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      if (!m) return route.fulfill({ status: 404 });
      const [, id, rest] = m;
      if (!id) return json(list);
      if (rest?.startsWith("page/")) return route.fulfill({ status: 200, contentType: "image/png", body: pngs[id] });
      if (rest === "ocr") return json({ pages: [{ page: 1, width: 1240, height: 1754, tokens: ocr[id] || [] }] });
      if (rest === "rois") return json({ rois: [] });
      if (!rest && req.method() === "GET") return json(jobs[id]);
      return json(req.postDataJSON() || {});
    });
  }

  async function shoot(name, url, { dark = false, mobile = false, act } = {}) {
    const ctx = await browser.newContext({
      viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 },
      deviceScaleFactor: 2, colorScheme: dark ? "dark" : "light", isMobile: mobile, hasTouch: mobile,
    });
    await mock(ctx);
    const p = await ctx.newPage();
    await p.goto(BASE + url, { waitUntil: "networkidle" });
    await p.waitForTimeout(600);
    if (act) await act(p).catch((e) => console.warn(name, "interaction skipped:", e.message.split("\n")[0]));
    await p.waitForTimeout(500);
    await p.screenshot({ path: path.join(OUT, name) });
    console.log("saved", name);
    await ctx.close();
  }

  const focusField = (label) => async (p) => {
    const row = p.getByText(label, { exact: true }).first();
    await row.hover({ timeout: 5000 });
    await row.click();
  };

  await shoot("review-invoice.png", "/jobs/acme", {
    act: async (p) => {
      await focusField("Total Amount")(p);
      // Selecting a field scrolls the page to its box; scroll back so the invoice header shows.
      await p.waitForTimeout(800);
      await p.locator('[class*="scroller"]').first().evaluate((el) => { el.scrollTop = 0; });
    },
  });
  await shoot("review-invoice-dark.png", "/jobs/acme", { dark: true, act: focusField("Company Name") });
  await shoot("review-line-items.png", "/jobs/acme", {
    act: async (p) => {
      await p.getByRole("button", { name: /^Items/ }).first().click();
      await p.waitForTimeout(500);
      await p.locator("tbody tr").nth(1).click();
    },
  });
  await shoot("review-persian-json.png", "/jobs/fa", { act: focusField("name") });
  await shoot("upload.png", "/");
  await shoot("setup.png", "/jobs/fresh/preview");
  await shoot("processing.png", "/jobs/running");
  await shoot("review-mobile.png", "/jobs/acme", { mobile: true });

  // README banner framing the review screenshot captured above.
  const hero = await browser.newPage({ viewport: { width: 1280, height: 640 }, deviceScaleFactor: 2 });
  await hero.goto("file:///" + path.join(ROOT, "scripts", "demo", "hero.html").replace(/\\/g, "/"));
  await hero.evaluate(() => document.fonts.ready);
  await hero.screenshot({ path: path.join(OUT, "hero.png") });
  console.log("saved hero.png");
  await browser.close();
}

main().catch((e) => { console.error(e); process.exit(1); });
