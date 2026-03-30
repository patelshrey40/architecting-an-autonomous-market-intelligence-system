const currency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const compactCurrency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});

const number = new Intl.NumberFormat("en-US");

const compactNumber = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

const state = {
  summary: null,
  map: null,
  parcelLayer: null,
  buildingLayer: null,
  boundaryLayer: null,
  transitLayer: null,
  redevelopmentLayer: null,
  selectedParcelId: null,
  selectedLeadId: null,
  lastParcelFeatures: [],
  lastLeadItems: [],
  ownershipRequestToken: 0,
  leadRunId: null,
};

function queryState() {
  const params = new URLSearchParams(window.location.search);
  return {
    classification: params.get("classification") || "",
    tier: params.get("tier") || "",
    q: params.get("q") || "",
    min_value: params.get("min_value") || "",
    parcel: params.get("parcel") || "",
    lead_persona: params.get("lead_persona") || "",
    lead_score_min: params.get("lead_score_min") || "",
    lead_contactable: params.get("lead_contactable") || "",
    lead_q: params.get("lead_q") || "",
    lead: params.get("lead") || "",
  };
}

function setQueryState(nextState) {
  const params = new URLSearchParams();
  Object.entries(nextState).forEach(([key, value]) => {
    if (value !== null && value !== undefined && String(value).trim() !== "") {
      params.set(key, value);
    }
  });
  const nextUrl = `${window.location.pathname}?${params.toString()}`;
  window.history.replaceState({}, "", nextUrl);
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`Request failed for ${url}`);
  }
  return response.json();
}

function ownershipBadge(text, variant = "muted") {
  return `<span class="pill pill-${variant}">${text}</span>`;
}

function renderSummary(summary) {
  const cards = [
    ["Parcels", compactNumber.format(summary.property_count)],
    ["Tier 1 parcels", compactNumber.format(summary.tier_1_count)],
    ["Leads", compactNumber.format(summary.lead_count || 0)],
    ["Contactable leads", compactNumber.format(summary.contactable_lead_count || 0)],
    ["Addresses", compactNumber.format(summary.address_count)],
    ["Transit stops", compactNumber.format(summary.transit_stop_count)],
    ["Redevelopment areas", compactNumber.format(summary.redevelopment_area_count)],
    ["Total assessed value", compactCurrency.format(summary.total_assessed_value)],
    ["Average priority", summary.average_priority_score],
    ["Last ingest", summary.last_ingested_at ? new Date(summary.last_ingested_at).toLocaleString() : "Not loaded"],
  ];

  document.getElementById("summaryGrid").innerHTML = cards
    .map(
      ([label, value]) => `
        <article class="stat-card">
          <strong>${value}</strong>
          <span>${label}</span>
        </article>
      `,
    )
    .join("");
}

function initFilters() {
  const qs = queryState();
  document.getElementById("classificationFilter").value = qs.classification;
  document.getElementById("tierFilter").value = qs.tier;
  document.getElementById("searchFilter").value = qs.q;
  document.getElementById("minValueFilter").value = qs.min_value;
  state.selectedParcelId = qs.parcel || null;
  state.selectedLeadId = qs.lead || null;

  document.getElementById("applyFiltersButton").addEventListener("click", () => {
    const next = {
      ...queryState(),
      classification: document.getElementById("classificationFilter").value,
      tier: document.getElementById("tierFilter").value,
      q: document.getElementById("searchFilter").value.trim(),
      min_value: document.getElementById("minValueFilter").value.trim(),
      parcel: state.selectedParcelId || "",
      lead: state.selectedLeadId || "",
    };
    setQueryState(next);
    loadParcels();
  });

  document.getElementById("toggleBuildings").addEventListener("change", loadBuildings);
  document.getElementById("toggleTransit").addEventListener("change", syncLayerVisibility);
  document.getElementById("toggleRedevelopment").addEventListener("change", syncLayerVisibility);
}

function initLeadFilters() {
  const qs = queryState();
  document.getElementById("leadPersonaFilter").value = qs.lead_persona;
  document.getElementById("leadScoreFilter").value = qs.lead_score_min;
  document.getElementById("leadContactableFilter").value = qs.lead_contactable;
  document.getElementById("leadSearchFilter").value = qs.lead_q;

  document.getElementById("applyLeadFiltersButton").addEventListener("click", () => {
    const next = {
      ...queryState(),
      lead_persona: document.getElementById("leadPersonaFilter").value,
      lead_score_min: document.getElementById("leadScoreFilter").value.trim(),
      lead_contactable: document.getElementById("leadContactableFilter").value,
      lead_q: document.getElementById("leadSearchFilter").value.trim(),
      lead: state.selectedLeadId || "",
      parcel: state.selectedParcelId || "",
    };
    setQueryState(next);
    loadLeads();
  });

  document.getElementById("refreshLeadsButton").addEventListener("click", queueLeadEnrichment);
}

async function queueLeadEnrichment() {
  const button = document.getElementById("refreshLeadsButton");
  button.disabled = true;
  document.getElementById("leadStatus").textContent = "Queueing lead enrichment…";
  try {
    const payload = await fetchJson("/api/newark/leads/enrich", { method: "POST" });
    state.leadRunId = payload.agent_run_id;
    document.getElementById("leadStatus").textContent = `Lead run queued: ${payload.agent_run_id}`;
    await loadLeads();
  } catch (error) {
    document.getElementById("leadStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function parcelStyle(feature) {
  return {
    color: feature.properties.classification_color,
    weight: feature.properties.id === state.selectedParcelId ? 2.4 : 1,
    fillColor: feature.properties.classification_color,
    fillOpacity: feature.properties.id === state.selectedParcelId ? 0.48 : 0.24,
  };
}

function highlightSelection() {
  if (!state.parcelLayer) {
    return;
  }
  state.parcelLayer.setStyle(parcelStyle);
}

function initMap(summary) {
  state.map = L.map("marketMap", {
    zoomControl: true,
    preferCanvas: true,
  }).setView(summary.center, 14);

  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(state.map);

  state.boundaryLayer = L.geoJSON(summary.boundary_geojson, {
    style: {
      color: "#17324d",
      weight: 2,
      opacity: 0.75,
      fillOpacity: 0,
      dashArray: "4 6",
    },
  }).addTo(state.map);

  state.parcelLayer = L.geoJSON([], {
    style: parcelStyle,
    onEachFeature(feature, layer) {
      layer.bindTooltip(
        `${feature.properties.display_name}<br>${feature.properties.classification} · ${feature.properties.priority_tier}`,
        { className: "parcel-tooltip" },
      );
      layer.on("click", () => selectParcel(feature.properties.id));
    },
  }).addTo(state.map);

  state.buildingLayer = L.geoJSON([], {
    style: {
      color: "#17324d",
      weight: 0.8,
      opacity: 0.85,
      fillColor: "#ffffff",
      fillOpacity: 0.08,
    },
  }).addTo(state.map);

  state.redevelopmentLayer = L.geoJSON([], {
    style: {
      color: "#0b7285",
      weight: 2,
      opacity: 0.8,
      fillColor: "#15aabf",
      fillOpacity: 0.12,
      dashArray: "6 4",
    },
    onEachFeature(feature, layer) {
      layer.bindTooltip(feature.properties.display_name, { className: "parcel-tooltip" });
    },
  }).addTo(state.map);

  state.transitLayer = L.geoJSON([], {
    pointToLayer(feature, latlng) {
      return L.circleMarker(latlng, {
        radius: 5,
        color: "#7c2d12",
        weight: 1.5,
        fillColor: "#f97316",
        fillOpacity: 0.9,
      });
    },
    onEachFeature(feature, layer) {
      const details = [feature.properties.stop_type, feature.properties.rail_line].filter(Boolean).join(" · ");
      layer.bindTooltip(`${feature.properties.display_name}<br>${details}`, { className: "parcel-tooltip" });
    },
  }).addTo(state.map);

  state.map.on("moveend", () => {
    loadParcels();
    loadBuildings();
    loadTransitStops();
    loadRedevelopmentAreas();
  });
}

function currentBboxString() {
  const bounds = state.map.getBounds();
  return [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].join(",");
}

async function loadParcels() {
  const qs = queryState();
  const params = new URLSearchParams({ bbox: currentBboxString() });
  if (qs.classification) {
    params.set("classification", qs.classification);
  }
  if (qs.tier) {
    params.set("tier", qs.tier);
  }
  if (qs.q) {
    params.set("q", qs.q);
  }
  if (qs.min_value) {
    params.set("min_value", qs.min_value);
  }

  document.getElementById("mapStatus").textContent = "Loading parcel data…";
  const featureCollection = await fetchJson(`/api/newark/parcels?${params.toString()}`);
  state.lastParcelFeatures = featureCollection.features;
  state.parcelLayer.clearLayers();
  state.parcelLayer.addData(featureCollection);
  highlightSelection();
  renderParcelTable(featureCollection.features);
  document.getElementById("mapStatus").textContent = `${featureCollection.features.length} parcels in current viewport`;
}

async function loadBuildings() {
  if (!state.map || !document.getElementById("toggleBuildings").checked || state.map.getZoom() < 16) {
    state.buildingLayer.clearLayers();
    return;
  }
  const featureCollection = await fetchJson(`/api/newark/buildings?bbox=${encodeURIComponent(currentBboxString())}`);
  state.buildingLayer.clearLayers();
  state.buildingLayer.addData(featureCollection);
}

async function loadTransitStops() {
  if (!state.map) {
    return;
  }
  const featureCollection = await fetchJson(`/api/newark/transit-stops?bbox=${encodeURIComponent(currentBboxString())}`);
  state.transitLayer.clearLayers();
  state.transitLayer.addData(featureCollection);
  syncLayerVisibility();
}

async function loadRedevelopmentAreas() {
  if (!state.map) {
    return;
  }
  const featureCollection = await fetchJson(`/api/newark/redevelopment-areas?bbox=${encodeURIComponent(currentBboxString())}`);
  state.redevelopmentLayer.clearLayers();
  state.redevelopmentLayer.addData(featureCollection);
  syncLayerVisibility();
}

function syncLayerVisibility() {
  if (!state.map) {
    return;
  }
  const showBuildings = document.getElementById("toggleBuildings").checked && state.map.getZoom() >= 16;
  const showTransit = document.getElementById("toggleTransit").checked;
  const showRedevelopment = document.getElementById("toggleRedevelopment").checked;

  if (showBuildings && !state.map.hasLayer(state.buildingLayer)) {
    state.map.addLayer(state.buildingLayer);
  } else if (!showBuildings && state.map.hasLayer(state.buildingLayer)) {
    state.map.removeLayer(state.buildingLayer);
  }

  if (showTransit && !state.map.hasLayer(state.transitLayer)) {
    state.map.addLayer(state.transitLayer);
  } else if (!showTransit && state.map.hasLayer(state.transitLayer)) {
    state.map.removeLayer(state.transitLayer);
  }

  if (showRedevelopment && !state.map.hasLayer(state.redevelopmentLayer)) {
    state.map.addLayer(state.redevelopmentLayer);
  } else if (!showRedevelopment && state.map.hasLayer(state.redevelopmentLayer)) {
    state.map.removeLayer(state.redevelopmentLayer);
  }
}

function renderParcelTable(features) {
  const tableBody = document.getElementById("parcelTableBody");
  document.getElementById("tableStatus").textContent = `${features.length} parcels loaded`;

  tableBody.innerHTML = features
    .slice(0, 60)
    .map((feature) => {
      const parcel = feature.properties;
      return `
        <tr>
          <td>
            <button class="table-row-button" data-parcel-id="${parcel.id}">
              <span class="parcel-chip">
                <span class="parcel-dot" style="background:${parcel.classification_color}"></span>
                <span>
                  <strong>${parcel.display_name}</strong><br>
                  <span class="muted">${parcel.parcel_pin}</span>
                </span>
              </span>
            </button>
          </td>
          <td>${parcel.classification}</td>
          <td>${parcel.priority_tier}</td>
          <td>${parcel.priority_score}</td>
          <td>${compactCurrency.format(parcel.total_assessed_value)}</td>
          <td>${compactNumber.format(parcel.building_count)}</td>
          <td>${compactNumber.format(parcel.place_count)}</td>
        </tr>
      `;
    })
    .join("");

  tableBody.querySelectorAll("[data-parcel-id]").forEach((button) => {
    button.addEventListener("click", () => selectParcel(button.dataset.parcelId));
  });
}

async function loadLeads() {
  const qs = queryState();
  const params = new URLSearchParams();
  if (qs.lead_persona) {
    params.set("persona", qs.lead_persona);
  }
  if (qs.lead_score_min) {
    params.set("score_min", qs.lead_score_min);
  }
  if (qs.lead_contactable) {
    params.set("contactable", qs.lead_contactable);
  }
  if (qs.lead_q) {
    params.set("q", qs.lead_q);
  }

  document.getElementById("leadStatus").textContent = "Loading lead intelligence…";
  const payload = await fetchJson(`/api/newark/leads?${params.toString()}`);
  state.lastLeadItems = payload.items || [];
  renderLeadList(state.lastLeadItems);
  const latestRun = payload.latest_agent_run;
  state.leadRunId = latestRun ? latestRun.id : state.leadRunId;
  document.getElementById("leadStatus").textContent = `${state.lastLeadItems.length} ranked leads${latestRun ? ` · latest run ${latestRun.status.replaceAll("_", " ")}` : ""}`;

  if (!state.selectedLeadId && state.lastLeadItems.length) {
    state.selectedLeadId = state.lastLeadItems[0].id;
  }
  if (state.selectedLeadId) {
    const selected = state.lastLeadItems.find((item) => item.id === state.selectedLeadId);
    if (selected) {
      await selectLead(selected.id, false);
    }
  }
}

function renderLeadList(items) {
  const container = document.getElementById("leadList");
  if (!items.length) {
    container.innerHTML = `
      <div class="detail-card">
        <h3>No leads yet</h3>
        <p class="muted">Run lead enrichment after ownership enrichment to build a recruiter-ready Newark lead list.</p>
      </div>
    `;
    return;
  }
  container.innerHTML = items
    .map(
      (lead) => `
        <button class="ops-run-item ${lead.id === state.selectedLeadId ? "ops-run-item-active" : ""}" data-lead-id="${lead.id}">
          <strong>${lead.person.full_name}</strong>
          <span>${lead.persona} · ${lead.title || "Untitled role"}</span>
          <span>${lead.organization ? lead.organization.name : "Public source"}</span>
          <span>Score ${lead.lead_score} · ${lead.contacts.length} contact point${lead.contacts.length === 1 ? "" : "s"}</span>
        </button>
      `,
    )
    .join("");
  container.querySelectorAll("[data-lead-id]").forEach((button) => {
    button.addEventListener("click", () => selectLead(button.dataset.leadId));
  });
}

async function selectLead(leadId, syncQuery = true) {
  state.selectedLeadId = leadId;
  if (syncQuery) {
    const next = { ...queryState(), lead: leadId, parcel: state.selectedParcelId || "" };
    setQueryState(next);
  }
  renderLeadList(state.lastLeadItems);
  const detail = await fetchJson(`/api/newark/leads/${leadId}`);
  renderLeadDetail(detail);
}

function renderLeadDetail(detail) {
  const contacts = detail.contacts.length
    ? detail.contacts
        .map(
          (contact) => `
            <div class="list-item">
              <strong>${contact.contact_type.replaceAll("_", " ")}</strong>
              <div>${contact.contact_value}</div>
              <div>${contact.label || contact.source_family || "Public source"}</div>
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No public business contact points are stored for this lead yet.</div>';

  const affiliations = detail.affiliations.length
    ? detail.affiliations
        .map(
          (affiliation) => `
            <div class="list-item">
              <strong>${affiliation.title}</strong>
              <div>${affiliation.affiliation_type} · ${affiliation.persona || detail.persona}</div>
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No affiliations are stored for this lead yet.</div>';

  const evidence = detail.evidence_links.length
    ? detail.evidence_links
        .map(
          (item) => `
            <div class="list-item">
              <strong>${item.evidence_type.replaceAll("_", " ")}</strong>
              <div>${item.notes || "No extra notes."}</div>
              ${item.parcel_id ? `<div>Parcel: ${item.parcel_id}</div>` : ""}
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No evidence links were promoted for this lead.</div>';

  const sources = detail.source_provenance.length
    ? detail.source_provenance
        .map(
          (source) => `
            <div class="list-item">
              <strong>${source.title}</strong>
              <div>${source.source_name} · ${source.document_type}</div>
              <div>${source.access_date}</div>
              ${source.source_url ? `<div><a href="${source.source_url}" target="_blank" rel="noreferrer">${source.source_url}</a></div>` : ""}
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No provenance records are stored for this lead yet.</div>';

  const runSummary = detail.agent_run
    ? `
      <div class="list-item">
        <strong>Contact swarm</strong>
        <div>${detail.agent_run.status.replaceAll("_", " ")} · ${detail.agent_run.current_step || "completed"}</div>
        <div><a href="/ops/agents?run=${encodeURIComponent(detail.agent_run.id)}" target="_blank" rel="noreferrer">Open agent ops</a></div>
      </div>
    `
    : '<div class="list-item">No person-level contact swarm has been queued yet.</div>';

  document.getElementById("leadDetailPanel").innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <div class="detail-head">
          <div>
            <p class="section-kicker">Lead detail</p>
            <h2>${detail.person.full_name}</h2>
            <p class="muted">${detail.persona} · ${detail.organization ? detail.organization.name : "Public source"}</p>
          </div>
          <div class="badge-row">
            <span class="pill">${detail.lead_score}</span>
            <span class="pill pill-muted">${detail.review_state.replaceAll("_", " ")}</span>
          </div>
        </div>
        <p>${detail.why_person_matters}</p>
      </section>

      <section class="detail-card">
        <h3>Score breakdown</h3>
        <div class="metrics-grid">
          <div class="metric-item"><span>Role relevance</span><strong>${detail.role_relevance_score}</strong></div>
          <div class="metric-item"><span>Market relevance</span><strong>${detail.market_relevance_score}</strong></div>
          <div class="metric-item"><span>Contactability</span><strong>${detail.contactability_score}</strong></div>
          <div class="metric-item"><span>Evidence</span><strong>${detail.evidence_score}</strong></div>
        </div>
      </section>

      <section class="detail-card">
        <h3>Business contact points</h3>
        <div class="list-stack">${contacts}</div>
      </section>

      <section class="detail-card">
        <div class="detail-head">
          <h3>Affiliations</h3>
          <button class="primary-button ops-inline-button" type="button" id="contactEnrichButton">Refresh contact evidence</button>
        </div>
        <div class="list-stack">${affiliations}</div>
      </section>

      <section class="detail-card">
        <h3>Evidence links</h3>
        <div class="list-stack">${evidence}</div>
      </section>

      <section class="detail-card">
        <h3>Source provenance</h3>
        <div class="list-stack">${sources}</div>
      </section>

      <section class="detail-card">
        <h3>Agentic trace</h3>
        <div class="list-stack">${runSummary}</div>
      </section>
    </div>
  `;

  document.getElementById("contactEnrichButton").addEventListener("click", async () => {
    const button = document.getElementById("contactEnrichButton");
    button.disabled = true;
    try {
      await fetchJson(`/api/newark/people/${detail.person.id}/contact-enrich`, { method: "POST" });
      await loadLeads();
      const refreshed = await fetchJson(`/api/newark/leads/${detail.id}`);
      renderLeadDetail(refreshed);
    } finally {
      button.disabled = false;
    }
  });
}

async function selectParcel(parcelId) {
  state.selectedParcelId = parcelId;
  const nextState = { ...queryState(), parcel: parcelId, lead: state.selectedLeadId || "" };
  setQueryState(nextState);
  highlightSelection();
  const detail = await fetchJson(`/api/newark/parcels/${parcelId}`);
  renderParcelDetail(detail);
  loadOwnership(parcelId);
}

function renderParcelDetail(detail) {
  const parcel = detail.parcel;

  const buildings = detail.buildings.length
    ? detail.buildings
        .map(
          (building) => `
            <div class="list-item">
              <strong>${building.building_name}</strong>
              <div>${compactNumber.format(Math.round(building.building_sqft))} sq ft · ${compactNumber.format(building.place_count)} places</div>
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No matched buildings in current dataset.</div>';

  const addresses = detail.addresses.length
    ? detail.addresses
        .map((address) => `<div class="list-item">${address.display_name}</div>`)
        .join("")
    : '<div class="list-item">No matched addresses in current dataset.</div>';

  const sources = detail.sources.length
    ? detail.sources
        .map(
          (source) => `
            <div class="list-item">
              <strong>${source.title}</strong>
              <div>${source.source_name} · ${source.document_type}</div>
              <div>${source.access_date}</div>
              <div><a href="${source.source_url}" target="_blank" rel="noreferrer">${source.source_url}</a></div>
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No provenance records found.</div>';

  const redevelopmentAreas = detail.redevelopment_areas.length
    ? detail.redevelopment_areas
        .map(
          (area) => `
            <div class="list-item">
              <strong>${area.short_name || area.name}</strong>
              <div>${area.name}</div>
              ${area.plan_link ? `<div><a href="${area.plan_link}" target="_blank" rel="noreferrer">${area.plan_link}</a></div>` : ""}
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">Parcel is not currently matched to a redevelopment area.</div>';

  const nearbyTransit = detail.nearby_transit.length
    ? detail.nearby_transit
        .map(
          (stop) => `
            <div class="list-item">
              <strong>${stop.stop_name}</strong>
              <div>${stop.stop_type}${stop.rail_line ? ` · ${stop.rail_line}` : ""}</div>
              <div>${number.format(stop.distance_m)} m from parcel centroid</div>
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No nearby transit stops found in the current dataset.</div>';

  document.getElementById("detailPanel").innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <div class="detail-head">
          <div>
            <p class="section-kicker">Parcel detail</p>
            <h2>${parcel.display_name}</h2>
            <p class="muted">${parcel.parcel_pin} · block ${parcel.block} lot ${parcel.lot}${parcel.qualifier ? ` · ${parcel.qualifier}` : ""}</p>
          </div>
          <div class="pill">${parcel.priority_tier} · ${parcel.priority_score}</div>
        </div>
      </section>

      <section class="detail-card">
        <h3>Facts</h3>
        <div class="metrics-grid">
          <div class="metric-item"><span>Classification</span><strong>${parcel.classification}</strong></div>
          <div class="metric-item"><span>Class code</span><strong>${parcel.property_class_code}</strong></div>
          <div class="metric-item"><span>Land value</span><strong>${compactCurrency.format(parcel.land_value)}</strong></div>
          <div class="metric-item"><span>Improvement value</span><strong>${compactCurrency.format(parcel.improvement_value)}</strong></div>
          <div class="metric-item"><span>Total assessed</span><strong>${compactCurrency.format(parcel.total_assessed_value)}</strong></div>
          <div class="metric-item"><span>Buildings / places</span><strong>${compactNumber.format(parcel.building_count)} / ${compactNumber.format(parcel.place_count)}</strong></div>
        </div>
      </section>

      <section class="detail-card">
        <h3>Score breakdown</h3>
        <div class="metrics-grid">
          <div class="metric-item"><span>Assessed value percentile</span><strong>${detail.score_breakdown.assessed_value_percentile}</strong></div>
          <div class="metric-item"><span>Class relevance</span><strong>${detail.score_breakdown.class_relevance_score}</strong></div>
          <div class="metric-item"><span>Building sqft percentile</span><strong>${detail.score_breakdown.building_sqft_percentile}</strong></div>
          <div class="metric-item"><span>Place density signal</span><strong>${detail.score_breakdown.place_density_signal}</strong></div>
        </div>
      </section>

      <section class="detail-card">
        <div class="detail-head">
          <h3>Ownership</h3>
          <div id="ownershipBadges" class="badge-row">
            <span class="pill pill-muted">Loading</span>
          </div>
        </div>
        <div id="ownershipContent" class="list-stack">
          <div class="list-item">Loading owner-of-record and NJ entity summary data…</div>
        </div>
      </section>

      <section class="detail-card">
        <h3>Buildings</h3>
        <div class="list-stack">${buildings}</div>
      </section>

      <section class="detail-card">
        <h3>Addresses</h3>
        <div class="list-stack">${addresses}</div>
      </section>

      <section class="detail-card">
        <h3>Redevelopment context</h3>
        <div class="list-stack">${redevelopmentAreas}</div>
      </section>

      <section class="detail-card">
        <h3>Nearby transit</h3>
        <div class="list-stack">${nearbyTransit}</div>
      </section>

      <section class="detail-card">
        <h3>Source provenance</h3>
        <div class="list-stack">${sources}</div>
      </section>
    </div>
  `;
}

function renderAgentRunSummary(agentRun) {
  if (!agentRun) {
    return `
      <div class="list-item">
        <strong>Swarm run</strong>
        <div>No ownership swarm has been queued for this parcel yet.</div>
      </div>
    `;
  }
  return `
    <div class="list-item">
      <strong>Swarm run</strong>
      <div>Run ID: ${agentRun.id}</div>
      <div>Status: ${agentRun.status.replaceAll("_", " ")}</div>
      <div>Review: ${(agentRun.review_state || "not_required").replaceAll("_", " ")}</div>
      <div>Current step: ${agentRun.current_step || "Unavailable"}</div>
      <div><a href="/ops/agents?run=${encodeURIComponent(agentRun.id)}" target="_blank" rel="noreferrer">Open agent ops</a></div>
    </div>
  `;
}

async function loadOwnership(parcelId) {
  const requestToken = ++state.ownershipRequestToken;
  const badgeTarget = document.getElementById("ownershipBadges");
  const contentTarget = document.getElementById("ownershipContent");
  if (!badgeTarget || !contentTarget) {
    return;
  }

  badgeTarget.innerHTML = ownershipBadge("Loading", "muted");
  contentTarget.innerHTML = '<div class="list-item">Loading owner-of-record and NJ entity summary data…</div>';

  try {
    const detail = await fetchJson(`/api/newark/parcels/${parcelId}/ownership`);
    if (requestToken !== state.ownershipRequestToken || parcelId !== state.selectedParcelId) {
      return;
    }
    renderOwnership(detail);
  } catch (error) {
    if (requestToken !== state.ownershipRequestToken || parcelId !== state.selectedParcelId) {
      return;
    }
    badgeTarget.innerHTML = ownershipBadge("Failed", "danger");
    contentTarget.innerHTML = `
      <div class="list-item">
        <strong>Ownership lookup failed</strong>
        <div>${error.message}</div>
      </div>
    `;
  }
}

function renderOwnership(detail) {
  const badgeTarget = document.getElementById("ownershipBadges");
  const contentTarget = document.getElementById("ownershipContent");
  if (!badgeTarget || !contentTarget) {
    return;
  }

  const statusVariant = {
    completed: "success",
    needs_review: "warning",
    failed: "danger",
    in_progress: "muted",
    not_started: "muted",
  }[detail.enrichment_status] || "muted";

  const confidenceVariant = {
    probable: "accent",
    verified: "success",
    inferred: "warning",
    conflict: "danger",
    partial: "warning",
    unknown: "muted",
  }[detail.confidence_tier] || "muted";

  const badges = [ownershipBadge(detail.enrichment_status.replaceAll("_", " "), statusVariant)];
  if (detail.confidence_tier) {
    badges.push(ownershipBadge(detail.confidence_tier, confidenceVariant));
  }
  badgeTarget.innerHTML = badges.join("");

  const qa = detail.qa || {};
  const qaIssues = qa.issues || [];
  const qaWarnings = qa.warnings || [];
  const reviewReasons = detail.review_reasons || [];
  const enrichmentError = detail.enrichment_error || null;
  const matchCandidatesList = detail.match_candidates || [];
  const deedHistoryList = detail.deed_history || [];
  const financingClaimsList = detail.financing_claims || [];
  const relatedFilingsList = detail.related_filings || [];
  const ownershipSourcesList = detail.source_provenance || [];
  const relatedLeadsList = detail.related_leads || [];
  const agentRunSummary = renderAgentRunSummary(detail.agent_run);

  const ownershipQaList = [
    ...qaIssues.map(
      (item) => `
        <div class="list-item">
          <strong>Issue</strong>
          <div>${item.message || "Ownership quality issue detected."}</div>
        </div>
      `,
    ),
    ...qaWarnings.map(
      (item) => `
        <div class="list-item">
          <strong>Warning</strong>
          <div>${item.message || "Ownership quality warning detected."}</div>
        </div>
      `,
    ),
    ...reviewReasons.map(
      (item) => `
        <div class="list-item">
          <strong>Review reason</strong>
          <div>${item}</div>
        </div>
      `,
    ),
  ];

  const qaBlock = `
    <div class="detail-subsection">
      <h4>Quality signals</h4>
      <div class="list-stack">
        ${ownershipQaList.length ? ownershipQaList.join("") : '<div class="list-item">No quality blockers are currently present.</div>'}
        <div class="list-item">Active claims: ${detail.active_claim_count || 0}</div>
        ${enrichmentError ? `<div class="list-item"><strong>Enrichment error</strong><div>${enrichmentError.code || "ownership_error"}</div><div>${enrichmentError.message || "No extra detail."}</div></div>` : ""}
      </div>
    </div>
  `;

  const deedHistory = deedHistoryList.length
    ? deedHistoryList
        .map(
          (filing) => `
            <div class="list-item">
              <strong>${filing.document_number || "Deed"}</strong>
              <div>Recorded: ${filing.recorded_at || "Unavailable"}</div>
              <div>Grantor(s): ${filing.grantors && filing.grantors.length ? filing.grantors.join(", ") : "Unavailable"}</div>
              <div>Grantee(s): ${filing.grantees && filing.grantees.length ? filing.grantees.join(", ") : "Unavailable"}</div>
              <div>Book / page: ${filing.book || "—"} / ${filing.page || "—"}</div>
              <div>Consideration: ${filing.consideration ? compactCurrency.format(filing.consideration) : "Not exposed in current public search results"}</div>
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No deed history is currently stored for this parcel.</div>';

  const financingClaims = financingClaimsList.length
    ? financingClaimsList
        .map(
          (claim) => `
            <div class="list-item">
              <strong>${claim.lender_name || "Unknown filing party"}</strong>
              <div>${claim.claim_type ? claim.claim_type.replaceAll("_", " ") : "Recorder filing"} · ${claim.confidence_tier || "probable"}</div>
              <div>Doc #: ${claim.document?.document_number || "Unavailable"}</div>
              <div>Recorded: ${claim.document?.recorded_at || "Unavailable"}</div>
              <div>Amount: ${claim.amount ? compactCurrency.format(claim.amount) : "Unavailable"}</div>
              <div>Grantors: ${claim.document?.grantors && claim.document.grantors.length ? claim.document.grantors.join(", ") : "Unavailable"}</div>
              <div>Grantees: ${claim.document?.grantees && claim.document.grantees.length ? claim.document.grantees.join(", ") : "Unavailable"}</div>
              <div>Organization: ${
                claim.organization
                  ? `${claim.organization.name}${claim.organization.matched_to_nj_entity ? ` · NJ ${claim.organization.nj_entity_id}` : " · recorder-only"}`
                  : "No organization row stored"
              }</div>
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No financing or lender filings are currently stored for this parcel.</div>';

  const relatedFilings = relatedFilingsList.length
    ? relatedFilingsList
        .map(
          (filing) => `
            <div class="list-item">
              <strong>${filing.document_type || filing.document_category || "Recorder filing"}</strong>
              <div>Doc #: ${filing.document_number || "Unavailable"}</div>
              <div>Recorded: ${filing.recorded_at || "Unavailable"}</div>
              <div>Grantors: ${filing.grantors && filing.grantors.length ? filing.grantors.join(", ") : "Unavailable"}</div>
              <div>Grantees: ${filing.grantees && filing.grantees.length ? filing.grantees.join(", ") : "Unavailable"}</div>
              <div>Consideration: ${filing.consideration ? compactCurrency.format(filing.consideration) : "Unavailable"}</div>
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No related recorder filings were stored yet.</div>';

  const ownershipSources = ownershipSourcesList.length
    ? ownershipSourcesList
        .map(
          (source) => `
            <div class="list-item">
              <strong>${source.title}</strong>
              <div>${source.source_name} · ${source.document_type}</div>
              <div>${source.access_date}</div>
              ${source.source_url ? `<div><a href="${source.source_url}" target="_blank" rel="noreferrer">${source.source_url}</a></div>` : ""}
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No ownership provenance records are stored yet.</div>';

  const relatedLeads = relatedLeadsList.length
    ? relatedLeadsList
        .map(
          (lead) => `
            <div class="list-item">
              <strong>${lead.person.full_name}</strong>
              <div>${lead.persona} · ${lead.title || "Untitled role"}</div>
              <div>${lead.organization ? lead.organization.name : "Public source"}</div>
              <div>Score ${lead.lead_score}</div>
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No promoted leads are linked to this parcel yet.</div>';

  if (!detail.deed) {
    contentTarget.innerHTML = `
      <div class="list-item">
        <strong>No current deed summary</strong>
        <div>${detail.message || "No ownership record is stored for this parcel yet."}</div>
      </div>
      <div class="detail-subsection">
        <h4>Agentic run</h4>
        <div class="list-stack">${agentRunSummary}</div>
      </div>
      <div class="detail-subsection">
        <h4>Deed history</h4>
        <div class="list-stack">${deedHistory}</div>
      </div>
      <div class="detail-subsection">
        <h4>Financing / Lender filings</h4>
        <div class="list-stack">${financingClaims}</div>
      </div>
      <div class="detail-subsection">
        <h4>Related leads</h4>
        <div class="list-stack">${relatedLeads}</div>
      </div>
      <div class="detail-subsection">
        <h4>Ownership provenance</h4>
        <div class="list-stack">${ownershipSources}</div>
      </div>
      ${qaBlock}
    `;
    return;
  }

  const organization = detail.organization;
  const hasAmbiguousMatch = detail.enrichment_status === "needs_review" && matchCandidatesList.length && !organization?.matched_to_nj_entity;
  const officers = (detail.officers || []).length
    ? detail.officers
        .map(
          (officer) => `
            <div class="list-item">
              <strong>${officer.name}</strong>
              <div>${officer.title}</div>
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No officer detail is currently available from the public NJ entity response stored for this parcel.</div>';

  const matchCandidates = matchCandidatesList.length
    ? matchCandidatesList
        .map(
          (candidate) => `
            <div class="list-item">
              <strong>${candidate.business_name}</strong>
              <div>${candidate.entity_id} · score ${candidate.match_score}</div>
            </div>
          `,
        )
        .join("")
    : "";

  const matchState = hasAmbiguousMatch
    ? `
      <div class="list-item">
        <strong>Owner match is ambiguous</strong>
        <div>Multiple NJ business records matched; analyst review is required before declaring a definitive ownership organization.</div>
      </div>
    `
    : organization?.matched_to_nj_entity
    ? `
      <div class="list-item">
        <strong>Owner matched</strong>
        <div>Owner was matched to a public NJ entity record.</div>
      </div>
    `
    : `
      <div class="list-item">
        <strong>No NJ match stored</strong>
        <div>No owner match was confirmed from public NJ business entity search.</div>
      </div>
    `;

  contentTarget.innerHTML = `
    <div class="metrics-grid">
      <div class="metric-item"><span>Owner of record</span><strong>${organization ? organization.name : detail.deed.owner_name}</strong></div>
      <div class="metric-item"><span>Latest deed</span><strong>${detail.deed.document_number}</strong></div>
      <div class="metric-item"><span>Recorded</span><strong>${detail.deed.recorded_at || "Unavailable"}</strong></div>
      <div class="metric-item"><span>Last enriched</span><strong>${detail.last_enriched_at ? new Date(detail.last_enriched_at).toLocaleString() : "Unavailable"}</strong></div>
      <div class="metric-item"><span>Recorder history</span><strong>${detail.recorder_history_last_enriched_at ? new Date(detail.recorder_history_last_enriched_at).toLocaleString() : "Unavailable"}</strong></div>
      <div class="metric-item"><span>Financing filings</span><strong>${compactNumber.format(financingClaimsList.length)}</strong></div>
    </div>

    <div class="list-item">
      <strong>Deed summary</strong>
      <div>Grantor(s): ${(detail.deed.grantors || []).join(", ") || "Unavailable"}</div>
      <div>Grantee(s): ${(detail.deed.grantees || []).join(", ") || "Unavailable"}</div>
      <div>Book / page: ${detail.deed.book || "—"} / ${detail.deed.page || "—"}</div>
      <div>Consideration: ${detail.deed.consideration ? compactCurrency.format(detail.deed.consideration) : "Not exposed in current public search results"}</div>
    </div>

    <div class="detail-subsection">
      <h4>Ownership entity status</h4>
      <div class="list-stack">${matchState}</div>
    </div>

    <div class="list-item">
      <strong>NJ entity</strong>
      <div>${organization && organization.matched_to_nj_entity ? "Matched to NJ entity search" : "No NJ entity match stored"}</div>
      <div>Entity ID: ${organization?.nj_entity_id || "Unavailable"}</div>
      <div>Type: ${organization?.entity_type_title || organization?.entity_type || "Unavailable"}</div>
      <div>Formation date: ${organization?.formation_date || "Unavailable"}</div>
      <div>Status: ${organization?.status || "Unavailable from current public response"}</div>
      <div>Registered agent: ${organization?.registered_agent || "Unavailable from current public response"}</div>
    </div>

    ${detail.message ? `<div class="list-item"><strong>Notes</strong><div>${detail.message}</div></div>` : ""}

    ${matchCandidates ? `<div class="detail-subsection"><h4>Review candidates</h4><div class="list-stack">${matchCandidates}</div></div>` : ""}

    <div class="detail-subsection">
      <h4>Agentic run</h4>
      <div class="list-stack">${agentRunSummary}</div>
    </div>

    <div class="detail-subsection">
      <h4>Deed history</h4>
      <div class="list-stack">${deedHistory}</div>
    </div>

    <div class="detail-subsection">
      <h4>Financing / Lender filings</h4>
      <div class="list-stack">${financingClaims}</div>
    </div>

    <div class="detail-subsection">
      <h4>Related recorder filings</h4>
      <div class="list-stack">${relatedFilings}</div>
    </div>

    <div class="detail-subsection">
      <h4>Officers</h4>
      <div class="list-stack">${officers}</div>
    </div>

    <div class="detail-subsection">
      <h4>Related leads</h4>
      <div class="list-stack">${relatedLeads}</div>
    </div>

    <div class="detail-subsection">
      <h4>Ownership provenance</h4>
      <div class="list-stack">${ownershipSources}</div>
    </div>

    ${qaBlock}
  `;
}

async function init() {
  initFilters();
  initLeadFilters();
  const summary = await fetchJson("/api/newark/summary");
  state.summary = summary;
  renderSummary(summary);
  await loadLeads();
  initMap(summary);
  await loadParcels();
  await loadBuildings();
  await loadTransitStops();
  await loadRedevelopmentAreas();
  syncLayerVisibility();

  if (state.selectedParcelId) {
    await selectParcel(state.selectedParcelId);
  }
  if (state.selectedLeadId) {
    await selectLead(state.selectedLeadId, false);
  }
}

window.addEventListener("DOMContentLoaded", () => {
  init().catch((error) => {
    document.getElementById("mapStatus").textContent = error.message;
    document.getElementById("leadStatus").textContent = error.message;
    document.getElementById("detailPanel").innerHTML = `
      <div class="detail-card">
        <h3>Unable to load Newark data</h3>
        <p>${error.message}</p>
        <p class="muted">Run the Newark ingest command first: <code>python -m app.cli ingest-newark</code></p>
      </div>
    `;
    document.getElementById("leadDetailPanel").innerHTML = `
      <div class="detail-card">
        <h3>Unable to load Newark leads</h3>
        <p>${error.message}</p>
      </div>
    `;
  });
});
