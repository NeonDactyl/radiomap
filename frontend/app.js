const API = "/api";

// Matches radio-locator.com's published definitions, not an FCC standard,
// so contours here are comparable to what people are used to seeing there:
// FM local/distant/fringe = 60/50/40 dBu. AM's are given as mV/m of
// groundwave (2.0/0.5/0.15 per their FAQ); converted to dBu here to match the
// unit the rest of this app (and the backend) uses throughout.
const THRESHOLD_PRESETS = {
  FM: [
    { key: "local", label: "Local (60 dBu) — strong, reliable on any radio", value: 60 },
    { key: "distant", label: "Distant (50 dBu) — needs a good radio/antenna", value: 50 },
    { key: "fringe", label: "Fringe (40 dBu) — very weak, may not be usable", value: 40 },
  ],
  AM: [
    { key: "local", label: "Local (66 dBu / 2.0 mV/m) — strong, reliable", value: 66 },
    { key: "distant", label: "Distant (54 dBu / 0.5 mV/m) — needs a good radio", value: 54 },
    { key: "fringe", label: "Fringe (43.5 dBu / 0.15 mV/m) — very weak", value: 43.5 },
  ],
};
const DEFAULT_THRESHOLD_KEY = "distant";

// Comfortably above the total station count of either service nationwide
// (~11.4k FM, ~4.3k AM) -- with marker clustering handling the rendering
// side, there's no more need to cap how much of a wide viewport's data we
// actually fetch (a fixed row cap with no ORDER BY was previously making
// wide views silently show only whichever handful of states happened to
// sit first in the table).
const VIEWPORT_FETCH_LIMIT = 20000;
// Above this, render only a prefix of the sidebar list (all stations still
// get a clustered marker either way) -- a few thousand <li> elements is
// real DOM weight for no benefit when nobody scrolls that far.
const LIST_RENDER_CAP = 500;

const state = {
  service: "FM",
  genre: "",
  stations: [],
  stateBboxes: {},
  selected: null,
  markerLayer: null,
  coverageLayer: null,
  markersById: new Map(),
};

const map = L.map("map", { zoomControl: true }).setView([39.5, -98.35], 4);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
  maxZoom: 18,
}).addTo(map);

state.markerLayer = L.markerClusterGroup({
  showCoverageOnHover: false,
  maxClusterRadius: 60,
});
map.addLayer(state.markerLayer);
state.coverageLayer = L.layerGroup().addTo(map);

const el = (id) => document.getElementById(id);

function populateThresholdSelect(service) {
  const sel = el("threshold-select");
  sel.innerHTML = "";
  for (const p of THRESHOLD_PRESETS[service]) {
    const opt = document.createElement("option");
    opt.value = p.value;
    opt.textContent = p.label;
    if (p.key === DEFAULT_THRESHOLD_KEY) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function loadStates() {
  const res = await fetch(`${API}/meta/states`);
  const rows = await res.json();
  const sel = el("state-filter");
  sel.innerHTML = '<option value="">Jump to a state...</option>';
  state.stateBboxes = {};
  for (const row of rows) {
    if (row.bbox) state.stateBboxes[row.state] = row.bbox;
    const opt = document.createElement("option");
    opt.value = row.state;
    const fm = row.counts.FM || 0;
    const am = row.counts.AM || 0;
    opt.textContent = `${row.state} (${fm} FM / ${am} AM)`;
    sel.appendChild(opt);
  }
}

async function loadGenres() {
  const params = new URLSearchParams({ service: state.service });
  const res = await fetch(`${API}/meta/genres?${params}`);
  const rows = await res.json();
  const sel = el("genre-filter");
  const previous = state.genre;
  sel.innerHTML = '<option value="">All</option>';
  for (const row of rows) {
    const opt = document.createElement("option");
    opt.value = row.genre;
    opt.textContent = `${row.genre} (${row.count})`;
    sel.appendChild(opt);
  }
  // Genre lists differ between FM/AM; keep the filter only if it still applies.
  state.genre = rows.some((r) => r.genre === previous) ? previous : "";
  sel.value = state.genre;
}

function freqLabel(s) {
  return s.service === "FM" ? `${s.frequency_mhz.toFixed(1)} MHz` : `${Math.round(s.frequency_mhz * 1000)} kHz`;
}

let viewportTimer = null;
let suppressNextMoveReload = false;

function scheduleViewportLoad() {
  clearTimeout(viewportTimer);
  viewportTimer = setTimeout(loadStationsInView, 300);
}

// selectStation() pans the map to center the newly selected station, which
// fires the same "moveend" event a real user pan does. Without this, that
// self-triggered move would immediately reload the viewport and clear the
// selection it just made (the station would flash selected then deselect).
function panWithoutTriggeringReload(fn) {
  suppressNextMoveReload = true;
  fn();
  // Safety net: if the pan doesn't actually move the map (station already
  // centered), moveend never fires to consume the flag -- don't leave it
  // stuck suppressing a later, genuine user pan.
  setTimeout(() => { suppressNextMoveReload = false; }, 1000);
}

async function loadStationsInView() {
  // A live search takes over the list/markers; don't let a map pan/zoom
  // stomp on search results out from under the user.
  if (el("station-search").value.trim().length >= 2) return;

  clearSelection();
  const b = map.getBounds();
  const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(",");
  const params = new URLSearchParams({ service: state.service, bbox, limit: String(VIEWPORT_FETCH_LIMIT) });
  if (state.genre) params.set("genre", state.genre);
  const res = await fetch(`${API}/stations?${params}`);
  state.stations = await res.json();
  const totalMatching = res.headers.get("X-Total-Matching");
  showStations(state.stations, totalMatching ? Number(totalMatching) : null);
}

function showStations(stations, totalMatching) {
  // Only relevant if a viewport somehow still exceeds VIEWPORT_FETCH_LIMIT
  // (see /api/stations) -- shouldn't happen at this dataset's size, but if
  // it ever does, say so rather than silently showing a partial view.
  el("station-count").textContent =
    totalMatching && totalMatching > stations.length
      ? `${stations.length} of ${totalMatching} stations (zoom in to see them all)`
      : `${stations.length} stations`;
  renderMarkers(stations);
  renderList(stations.length > LIST_RENDER_CAP ? stations.slice(0, LIST_RENDER_CAP) : stations);
}

function stationIcon(service) {
  const color = service === "FM" ? "#2f6f4f" : "#2e5c8a";
  return L.divIcon({
    className: "station-dot",
    html: `<span style="background:${color}"></span>`,
    iconSize: [12, 12],
  });
}

function renderMarkers(stations) {
  state.markerLayer.clearLayers();
  state.markersById.clear();
  const markers = stations.map((s) => {
    const marker = L.marker([s.lat, s.lon], { icon: stationIcon(s.service) });
    marker.bindTooltip(`${s.callsign} — ${freqLabel(s)}`);
    marker.on("click", () => selectStation(s.id));
    state.markersById.set(s.id, marker);
    return marker;
  });
  // addLayers() is markercluster's bulk-add API -- one index rebuild
  // instead of one per marker, which matters once this can be thousands.
  state.markerLayer.addLayers(markers);
}

function renderList(stations) {
  const listEl = el("station-list");
  listEl.innerHTML = "";
  for (const s of stations) {
    const li = document.createElement("li");
    li.dataset.id = s.id;
    if (state.selected && state.selected.id === s.id) li.classList.add("selected");
    const genreSuffix = s.genre ? ` &middot; ${s.genre}` : "";
    li.innerHTML = `<span class="call">${s.callsign}</span><span class="freq">${freqLabel(s)} &middot; ${s.city || ""}, ${s.state || ""}${genreSuffix}</span>`;
    li.addEventListener("click", () => selectStation(s.id));
    listEl.appendChild(li);
  }
}

let searchTimer = null;

function onSearchInput(text) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runSearch(text.trim()), 250);
}

async function runSearch(text) {
  if (text.length < 2) {
    loadStationsInView();
    return;
  }
  // Search nationwide (ignores the current map viewport): a station's city
  // of license often isn't the market it actually serves, so restricting
  // to what's on-screen can hide the exact station someone is looking for.
  const params = new URLSearchParams({ service: state.service, search: text, limit: "50" });
  const res = await fetch(`${API}/stations?${params}`);
  const results = await res.json();
  state.stations = results;
  const totalMatching = res.headers.get("X-Total-Matching");
  showStations(results, totalMatching ? Number(totalMatching) : null);
}

function parseUrlState() {
  const params = new URLSearchParams(window.location.search);
  const bboxParam = params.get("bbox");
  let bbox = null;
  if (bboxParam) {
    const parts = bboxParam.split(",").map(Number);
    if (parts.length === 4 && parts.every(Number.isFinite)) bbox = parts;
  }
  const stationId = Number(params.get("station"));
  return { bbox, stationId: Number.isFinite(stationId) && stationId > 0 ? stationId : null };
}

// Keeps the current viewport and selected station reflected in the URL so
// it's bookmarkable/shareable -- doesn't touch browser history (replaceState
// only), since map panning fires this on every move and pushState would
// flood the back button.
function updateUrl() {
  const b = map.getBounds();
  const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((n) => n.toFixed(4)).join(",");
  const params = new URLSearchParams(window.location.search);
  params.set("bbox", bbox);
  if (state.selected) {
    params.set("station", state.selected.id);
  } else {
    params.delete("station");
  }
  history.replaceState(null, "", `${window.location.pathname}?${params}`);
}

function clearSelection() {
  state.selected = null;
  state.coverageLayer.clearLayers();
  el("detail-panel").classList.add("hidden");
  el("sidebar").classList.remove("has-selection");
  updateUrl();
}

async function selectStation(id) {
  const res = await fetch(`${API}/stations/${id}`);
  if (!res.ok) return;
  const s = await res.json();
  state.selected = s;
  state.coverageLayer.clearLayers();

  document.querySelectorAll("#station-list li").forEach((li) => {
    li.classList.toggle("selected", Number(li.dataset.id) === id);
  });

  const marker = state.markersById.get(id);
  panWithoutTriggeringReload(() => {
    // zoomToShowLayer breaks the marker out of its cluster if it's
    // currently grouped (and is a no-op movement if it's already visible)
    // -- a plain panTo left clustered stations selected-but-invisible,
    // since a marker hidden inside an unexpanded cluster isn't actually on
    // the map for openTooltip() to anchor to.
    if (marker) {
      state.markerLayer.zoomToShowLayer(marker, () => marker.openTooltip());
    } else {
      map.panTo([s.lat, s.lon]);
    }
  });

  el("detail-panel").classList.remove("hidden");
  el("sidebar").classList.add("has-selection");
  el("detail-callsign").textContent = `${s.callsign} — ${freqLabel(s)}`;
  el("detail-sub").textContent = `${s.city || "?"}, ${s.state || "?"} &middot; ${s.licensee || "Unknown licensee"}`.replace("&middot;", "·");

  const rows = [
    ["Genre", s.genre || "Unknown (no Wikidata match)"],
    ["Service", s.service],
    ["Class", s.class_ || "–"],
    ["Status", s.status || "–"],
    ["ERP / power", s.erp_kw != null ? `${s.erp_kw} kW` : "–"],
    ["HAAT", s.haat_m != null ? `${s.haat_m} m` : "n/a (AM)"],
    ["Directional", s.directional ? "Yes" : "No"],
    ["Night power", s.power_night_kw != null ? `${s.power_night_kw} kW` : "–"],
    ["Facility ID", s.facility_id],
  ];
  el("detail-table").innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");

  el("conductivity-row").style.display = s.service === "AM" ? "flex" : "none";
  el("fm-model-row").style.display = s.service === "FM" ? "flex" : "none";
  el("coverage-status").textContent = "";
  updateUrl();
}

async function showCoverage() {
  if (!state.selected) return;
  const btn = el("coverage-btn");
  btn.disabled = true;
  el("coverage-status").textContent =
    "Computing terrain profile... (high-power/tall-tower stations search a wider radius and can take up to ~40s)";

  const params = new URLSearchParams({
    threshold_dbu: el("threshold-select").value,
  });
  if (state.selected.service === "AM") {
    params.set("ground_conductivity_mmho", el("conductivity-select").value);
  } else {
    params.set("fm_model", el("fm-model-select").value);
  }

  try {
    const res = await fetch(`${API}/stations/${state.selected.id}/coverage?${params}`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    state.coverageLayer.clearLayers();
    const color = state.selected.service === "FM" ? "#2f6f4f" : "#2e5c8a";
    const poly = L.polygon(data.contour, {
      color,
      weight: 2,
      fillColor: color,
      fillOpacity: 0.18,
    }).addTo(state.coverageLayer);
    panWithoutTriggeringReload(() => map.fitBounds(poly.getBounds(), { padding: [30, 30] }));
    const cacheNote = data.cached ? " (cached)" : "";
    el("coverage-status").textContent =
      `Model: ${data.model} · threshold ${data.threshold_dbu} dBu · searched out to ${Math.round(data.max_radius_km)} km${cacheNote}`;
  } catch (err) {
    el("coverage-status").textContent = err.message || "Failed to compute coverage.";
    console.error(err);
  } finally {
    btn.disabled = false;
  }
}

function wireControls() {
  el("service-filter").addEventListener("change", async (e) => {
    state.service = e.target.value;
    el("station-search").value = "";
    populateThresholdSelect(state.service);
    await loadGenres();
    loadStationsInView();
  });
  el("genre-filter").addEventListener("change", (e) => {
    state.genre = e.target.value;
    el("station-search").value = "";
    loadStationsInView();
  });
  el("state-filter").addEventListener("change", (e) => {
    const bbox = state.stateBboxes[e.target.value];
    e.target.selectedIndex = 0; // it's a one-shot "jump to", not a sticky filter
    if (!bbox) return;
    const [minLon, minLat, maxLon, maxLat] = bbox;
    map.fitBounds([[minLat, minLon], [maxLat, maxLon]]); // triggers moveend -> loadStationsInView
  });
  el("station-search").addEventListener("input", (e) => onSearchInput(e.target.value));
  el("detail-close").addEventListener("click", clearSelection);
  el("coverage-btn").addEventListener("click", showCoverage);
  map.on("moveend", () => {
    updateUrl();
    if (suppressNextMoveReload) {
      suppressNextMoveReload = false;
      return;
    }
    scheduleViewportLoad();
  });
}

async function init() {
  populateThresholdSelect(state.service);
  wireControls();
  await loadStates();
  await loadGenres();

  const { bbox, stationId } = parseUrlState();
  if (bbox) {
    const [minLon, minLat, maxLon, maxLat] = bbox;
    panWithoutTriggeringReload(() => map.fitBounds([[minLat, minLon], [maxLat, maxLon]]));
  }
  await loadStationsInView();
  if (stationId) {
    await selectStation(stationId);
    // No bbox in the URL to anchor on (a bare station link) -- zoom to it
    // instead of leaving the map at the default nationwide view.
    if (!bbox && state.selected) {
      panWithoutTriggeringReload(() => map.setView([state.selected.lat, state.selected.lon], 10));
    }
  }
}

init();
