"use strict";

const CATEGORIES = ["Groceries", "Beverages", "Personal Care", "Household", "Electronics",
                    "Fashion", "Home & Kitchen", "Other"];
const POLL_MS = 1000;       // never poll faster than once a second
const SEARCH_DEBOUNCE_MS = 350;
const PAGE_SIZE = 20;

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------ API helper

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) {
    throw new Error((data && data.error) || `HTTP ${res.status}`);
  }
  return data;
}

// ------------------------------------------------------------ CSV parsing

// RFC 4180-style parser: quoted fields, "" escapes, commas/newlines inside quotes.
function parseCSV(text) {
  const rows = [];
  let row = [], field = "", inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; } else { inQuotes = false; }
      } else {
        field += c;
      }
    } else if (c === '"') {
      inQuotes = true;
    } else if (c === ",") {
      row.push(field); field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row); row = []; field = "";
    } else {
      field += c;
    }
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((f) => f.trim() !== ""));
}

function csvToProducts(text) {
  const rows = parseCSV(text.replace(/^﻿/, ""));
  if (rows.length < 2) throw new Error("CSV needs a header row and at least one listing");
  const header = rows[0].map((h) => h.trim().toLowerCase());
  const col = (name) => header.indexOf(name);
  const iSku = col("sku"), iTitle = col("raw_title"), iDesc = col("raw_description");
  if (iSku < 0 || iTitle < 0) throw new Error("CSV header must include sku and raw_title");
  return rows.slice(1).map((r) => {
    const p = { sku: (r[iSku] || "").trim(), raw_title: r[iTitle] || "" };
    if (iDesc >= 0 && r[iDesc] !== undefined && r[iDesc] !== "") p.raw_description = r[iDesc];
    return p;
  });
}

// ------------------------------------------------------------ upload + job progress

let pollTimer = null;

async function onUpload(ev) {
  ev.preventDefault();
  const file = $("csv-file").files[0];
  const msg = $("upload-msg");
  if (!file) return;
  msg.className = "";
  try {
    const products = csvToProducts(await file.text());
    msg.textContent = `Submitting ${products.length} listings…`;
    const job = await api("/api/jobs", { method: "POST", body: { products } });
    msg.textContent = `Job ${job.id} submitted.`;
    showJob(job);
    schedulePoll(job.id);
  } catch (err) {
    msg.className = "error";
    msg.textContent = `Upload failed: ${err.message}`;
  }
}

function showJob(job) {
  $("job").hidden = false;
  $("job-id").textContent = job.id;
  $("job-status").textContent = job.status;
  $("job-done").textContent = job.done;
  $("job-failed").textContent = job.failed;
  $("job-cache").textContent = job.cache_hits;
  $("job-total").textContent = job.total;
  const bar = $("job-progress");
  bar.max = job.total;
  bar.value = job.done;
  const pct = job.total ? Math.floor((100 * job.done) / job.total) : 0;
  $("job-summary").textContent = `${job.done} of ${job.total} processed (${pct}%)`;
}

function schedulePoll(jobId) {
  clearTimeout(pollTimer);
  // setTimeout after each response (not setInterval) so slow responses never overlap.
  pollTimer = setTimeout(async () => {
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      showJob(job);
      // Refresh the list as items land, but not while the user is navigating it
      // with the keyboard or reviewing a product (re-rendering would steal focus).
      const busy = $("products").contains(document.activeElement) || $("review").open;
      if (job.status === "completed" || !busy) loadProducts();
      if (job.status !== "completed") schedulePoll(jobId);
    } catch (err) {
      $("upload-msg").textContent = `Could not refresh job: ${err.message}`;
      schedulePoll(jobId);
    }
  }, POLL_MS);
}

// ------------------------------------------------------------ catalogue

const state = { page: 1, q: "", category: "", total: 0 };
let listAbort = null;

async function loadProducts() {
  if (listAbort) listAbort.abort();  // drop stale responses when filters change quickly
  listAbort = new AbortController();
  const params = new URLSearchParams({ page: state.page, page_size: PAGE_SIZE });
  if (state.q) params.set("q", state.q);
  if (state.category) params.set("category", state.category);
  try {
    const data = await api(`/api/products?${params}`, { signal: listAbort.signal });
    state.total = data.total;
    renderProducts(data);
  } catch (err) {
    if (err.name === "AbortError") return;
    $("list-msg").textContent = `Could not load products: ${err.message}`;
  }
}

function renderProducts(data) {
  const list = $("products");
  list.replaceChildren();
  for (const p of data.items) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = p.clean_title || p.raw_title;
    const meta = document.createElement("div");
    meta.className = "meta";
    const badge = document.createElement("span");
    badge.className = `badge ${p.status}`;
    badge.textContent = p.status;
    meta.append(badge, ` ${p.sku} · ${p.category || "—"} · ${p.brand || "no brand"}`);
    btn.append(title, meta);
    btn.setAttribute("aria-label", `Review ${p.sku}: ${title.textContent}, ${p.status}`);
    btn.addEventListener("click", () => openReview(p.sku, btn));
    li.append(btn);
    list.append(li);
  }
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  $("page-info").textContent = `Page ${data.page} of ${pages} · ${data.total} products`;
  $("prev").disabled = data.page <= 1;
  $("next").disabled = data.page >= pages;
  $("list-msg").textContent = data.total ? "" : "No products match.";
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ------------------------------------------------------------ review dialog

let reviewing = null;
let returnFocus = null;

async function openReview(sku, opener) {
  returnFocus = opener;
  try {
    const p = await api(`/api/products/${encodeURIComponent(sku)}`);
    reviewing = p;
    $("r-sku").textContent = p.sku;
    $("r-raw-title").textContent = p.raw_title;
    $("r-raw-desc").textContent = p.raw_description || "—";
    $("r-title").value = p.clean_title || "";
    $("r-category").value = p.category || "";
    $("r-tags").value = (p.tags || []).join(", ");
    $("r-brand").textContent = p.brand || "unknown";
    $("r-status").textContent = p.status;
    const errBox = $("r-error");
    errBox.hidden = !p.error;
    errBox.textContent = p.error ? `Enrichment failed: ${p.error}` : "";
    $("r-msg").textContent = "";
    $("review").showModal();
    $("r-title").focus();
  } catch (err) {
    $("list-msg").textContent = `Could not open ${sku}: ${err.message}`;
  }
}

async function onApprove(ev) {
  ev.preventDefault();
  if (!reviewing) return;
  const tags = $("r-tags").value.split(",").map((t) => t.trim().toLowerCase()).filter(Boolean);
  const msg = $("r-msg");
  if (tags.length > 5) { msg.textContent = "At most 5 tags."; return; }
  try {
    const updated = await api(`/api/products/${encodeURIComponent(reviewing.sku)}`, {
      method: "PATCH",
      body: { clean_title: $("r-title").value, category: $("r-category").value, tags },
    });
    reviewing = updated;
    $("review").close();
    loadProducts();
  } catch (err) {
    msg.textContent = `Could not save: ${err.message}`;
  }
}

// ------------------------------------------------------------ init

function fillCategories(select, withBlank) {
  if (withBlank) {
    const o = document.createElement("option");
    o.value = ""; o.textContent = "Choose…"; o.disabled = true;
    select.append(o);
  }
  for (const c of CATEGORIES) {
    const o = document.createElement("option");
    o.value = c; o.textContent = c;
    select.append(o);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  fillCategories($("category"), false);
  fillCategories($("r-category"), true);

  $("upload-form").addEventListener("submit", onUpload);
  $("filters").addEventListener("submit", (e) => e.preventDefault());
  $("q").addEventListener("input", debounce(() => {
    state.q = $("q").value.trim();
    state.page = 1;
    loadProducts();
  }, SEARCH_DEBOUNCE_MS));
  $("category").addEventListener("change", () => {
    state.category = $("category").value;
    state.page = 1;
    loadProducts();
  });
  $("prev").addEventListener("click", () => { state.page--; loadProducts(); });
  $("next").addEventListener("click", () => { state.page++; loadProducts(); });

  $("review-form").addEventListener("submit", onApprove);
  $("r-cancel").addEventListener("click", () => $("review").close());
  $("review").addEventListener("close", () => { if (returnFocus) returnFocus.focus(); });

  api("/api/health")
    .then((h) => { $("health").textContent = `LLM: ${h.llm_provider} · concurrency ${h.llm_concurrency}`; })
    .catch(() => { $("health").textContent = "Server unreachable"; });
  loadProducts();
});
