const API = "/api";

const THRESHOLD_PRESETS = [
  { label: "60 dBu — city grade (strong, reliable)", value: 60 },
  { label: "54 dBu — normally protected service", value: 54 },
  { label: "48 dBu — usable in quiet areas", value: 48 },
  { label: "40 dBu — fringe / weak-signal edge", value: 40 },
];

const MIN_ZOOM_TO_LOAD = 6; // below this, a viewport bbox is too big to be a useful "stations here" list

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

state.markerLayer = L.layerGroup().addTo(map);
state.coverageLayer = L.layerGroup().addTo(map);

const el = (id) => document.getElementById(id);

function populateThresholdSelect() {
  const sel = el("threshold-select");
  sel.innerHTML = "";
  for (const p of THRESHOLD_PRESETS) {
    const opt = document.createElement("option");
    opt.value = p.value;
    opt.textContent = p.label;
    if (p.value === 54) opt.selected = true;
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

function scheduleViewportLoad() {
  clearTimeout(viewportTimer);
  viewportTimer = setTimeout(loadStationsInView, 300);
}

async function loadStationsInView() {
  // A live search takes over the list/markers; don't let a map pan/zoom
  // stomp on search results out from under the user.
  if (el("station-search").value.trim().length >= 2) return;

  clearSelection();
  if (map.getZoom() < MIN_ZOOM_TO_LOAD) {
    state.stations = [];
    showStations([]);
    el("station-count").textContent = "Zoom in to see stations here";
    return;
  }
  const b = map.getBounds();
  const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(",");
  const params = new URLSearchParams({ service: state.service, bbox, limit: "1500" });
  if (state.genre) params.set("genre", state.genre);
  const res = await fetch(`${API}/stations?${params}`);
  state.stations = await res.json();
  showStations(state.stations);
}

function showStations(stations) {
  el("station-count").textContent = `${stations.length} stations`;
  renderMarkers(stations);
  renderList(stations);
}

function renderMarkers(stations) {
  state.markerLayer.clearLayers();
  state.markersById.clear();
  const color = state.service === "FM" ? "#2f6f4f" : "#2e5c8a";
  for (const s of stations) {
    const marker = L.circleMarker([s.lat, s.lon], {
      radius: 5,
      color,
      weight: 1,
      fillColor: color,
      fillOpacity: 0.75,
    });
    marker.bindTooltip(`${s.callsign} — ${freqLabel(s)}`);
    marker.on("click", () => selectStation(s.id));
    marker.addTo(state.markerLayer);
    state.markersById.set(s.id, marker);
  }
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
  showStations(results);
}

function clearSelection() {
  state.selected = null;
  state.coverageLayer.clearLayers();
  el("detail-panel").classList.add("hidden");
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

  map.panTo([s.lat, s.lon]);
  const marker = state.markersById.get(id);
  if (marker) marker.openTooltip();

  el("detail-panel").classList.remove("hidden");
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
  el("coverage-status").textContent = "";
}

async function showCoverage() {
  if (!state.selected) return;
  const btn = el("coverage-btn");
  btn.disabled = true;
  el("coverage-status").textContent = "Computing terrain profile...";

  const params = new URLSearchParams({
    threshold_dbu: el("threshold-select").value,
    max_radius_km: el("radius-input").value,
  });
  if (state.selected.service === "AM") {
    params.set("ground_conductivity_mmho", el("conductivity-select").value);
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
    map.fitBounds(poly.getBounds(), { padding: [30, 30] });
    el("coverage-status").textContent = `Model: ${data.model} · threshold ${data.threshold_dbu} dBu`;
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
  map.on("moveend", scheduleViewportLoad);
}

async function init() {
  populateThresholdSelect();
  wireControls();
  await loadStates();
  await loadGenres();
  loadStationsInView();
}

init();
