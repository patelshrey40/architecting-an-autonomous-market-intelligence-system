const currency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const number = new Intl.NumberFormat("en-US");

let demoState = null;
let selectedPropertyId = null;

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Request failed for ${url}`);
  }
  return response.json();
}

function renderSummary(summary) {
  const cards = [
    ["Cities", summary.cities_covered],
    ["Properties", summary.property_count],
    ["Tier 1", summary.tier_1_count],
    ["Claims", summary.claim_count],
    ["Targets", summary.target_count],
    ["Allowed sources", summary.allowed_sources],
    ["Restricted sources", summary.restricted_sources],
  ];

  document.getElementById("summaryGrid").innerHTML = cards
    .map(
      ([label, value]) => `
        <article class="stat-card">
          <strong>${number.format(value)}</strong>
          <span>${label}</span>
        </article>
      `,
    )
    .join("");
}

function renderPhases(phases) {
  document.getElementById("phaseGrid").innerHTML = phases
    .map(
      (phase) => `
        <article class="phase-card">
          <div class="status">${phase.status.replaceAll("-", " ")}</div>
          <h3>${phase.name}</h3>
          <p class="muted">${phase.summary}</p>
        </article>
      `,
    )
    .join("");
}

function renderCitySummaries(citySummaries) {
  document.getElementById("citySummaryList").innerHTML = citySummaries
    .map(
      (summary) => `
        <article class="city-summary">
          <strong>${summary.city}</strong>
          <p class="muted">${summary.property_count} properties, ${summary.tier_1_count} tier 1</p>
          <p>Average priority score: <strong>${summary.avg_priority_score}</strong></p>
        </article>
      `,
    )
    .join("");
}

function renderPropertyTable(properties) {
  document.getElementById("propertyTableBody").innerHTML = properties
    .map(
      (property) => `
        <tr>
          <td>
            <button class="property-button" data-property-id="${property.id}">
              <span class="property-dot" style="background:${property.classification_color}"></span>
              <span>
                <strong>${property.name}</strong><br>
                <span class="muted">${property.address}</span>
              </span>
            </button>
          </td>
          <td>${property.city}</td>
          <td>${property.classification}</td>
          <td>${property.priority_tier}</td>
          <td>${property.priority_score}</td>
          <td>${property.owner_name}</td>
        </tr>
      `,
    )
    .join("");

  document.querySelectorAll(".property-button").forEach((button) => {
    button.addEventListener("click", () => selectProperty(button.dataset.propertyId));
  });
}

function renderLegend(properties) {
  const seen = new Map();
  properties.forEach((property) => {
    if (!seen.has(property.classification)) {
      seen.set(property.classification, property.classification_color);
    }
  });

  document.getElementById("mapLegend").innerHTML = [...seen.entries()]
    .map(
      ([label, color]) => `
        <div class="legend-item">
          <span class="legend-swatch" style="background:${color}"></span>
          <span>${label}</span>
        </div>
      `,
    )
    .join("");
}

function renderMap(properties) {
  const svg = document.getElementById("marketMap");
  const width = 900;
  const height = 420;
  const padding = 52;
  const lons = properties.map((property) => property.lon);
  const lats = properties.map((property) => property.lat);
  const minLon = Math.min(...lons);
  const maxLon = Math.max(...lons);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);

  const xScale = (lon) =>
    padding + ((lon - minLon) / Math.max(maxLon - minLon, 0.0001)) * (width - padding * 2);
  const yScale = (lat) =>
    height - padding - ((lat - minLat) / Math.max(maxLat - minLat, 0.0001)) * (height - padding * 2);

  const guides = [
    `<line x1="${padding}" y1="${height / 2}" x2="${width - padding}" y2="${height / 2}" stroke="rgba(29,53,87,0.08)" stroke-dasharray="6 6"></line>`,
    `<line x1="${width / 2}" y1="${padding}" x2="${width / 2}" y2="${height - padding}" stroke="rgba(29,53,87,0.08)" stroke-dasharray="6 6"></line>`,
    `<text x="${padding}" y="32" class="map-label">Newark and Jersey City seeded footprint</text>`,
  ];

  const nodes = properties
    .map((property) => {
      const x = xScale(property.lon);
      const y = yScale(property.lat);
      const radius = 8 + property.priority_score / 14;
      return `
        <g class="map-node ${property.id === selectedPropertyId ? "is-active" : ""}" data-property-id="${property.id}">
          <circle cx="${x}" cy="${y}" r="${radius}" fill="${property.classification_color}" fill-opacity="0.9"></circle>
          <text x="${x + radius + 8}" y="${y - 2}" class="map-label">${property.name}</text>
        </g>
      `;
    })
    .join("");

  svg.innerHTML = guides.join("") + nodes;
  svg.querySelectorAll(".map-node").forEach((node) => {
    node.addEventListener("click", () => selectProperty(node.dataset.propertyId));
  });
}

function renderTargets(targets) {
  document.getElementById("targetGrid").innerHTML = targets
    .slice(0, 6)
    .map(
      (target) => `
        <article class="target-card">
          <div class="target-score">${target.target_score}</div>
          <h3>${target.name}</h3>
          <p>${target.title}, ${target.organization_name}</p>
          <div class="target-notes">
            <div class="target-note">${target.why_it_matters}</div>
            <div class="target-note">
              <strong>Contact</strong>
              <span>${target.email}</span>
              <span>${target.phone}</span>
            </div>
            <div class="target-note">
              <strong>Signals</strong>
              <span>${target.linked_property_count} linked properties</span>
              <span>${target.in_existing_network ? "Warm relationship" : "Net-new outreach"}</span>
            </div>
          </div>
        </article>
      `,
    )
    .join("");
}

function renderSources(sources) {
  document.getElementById("sourceGrid").innerHTML = sources
    .map(
      (source) => `
        <article class="source-card">
          <div class="source-mode">${source.mode.replaceAll("_", " ")}</div>
          <h3>${source.source_name}</h3>
          <p class="muted">${source.access_method} · freshness ${source.freshness}</p>
          <p>${source.retention_rules}</p>
          <div class="source-list">
            ${source.legal_restrictions
              .map((restriction) => `<div class="source-item">${restriction}</div>`)
              .join("")}
          </div>
        </article>
      `,
    )
    .join("");
}

function metricRows(detail) {
  const breakdown = detail.scoring_breakdown;
  return Object.entries(breakdown)
    .map(
      ([label, value]) => `
        <div class="metric-row">
          <span>${label.replaceAll("_", " ")}</span>
          <strong>${value}</strong>
        </div>
      `,
    )
    .join("");
}

function renderPropertyDetail(detail) {
  const people = detail.people.length
    ? detail.people
        .map(
          (person) => `
            <div class="person-item">
              <strong>${person.name}</strong>
              <span>${person.title} · ${person.organization_name}</span>
              <span>${person.email}</span>
            </div>
          `,
        )
        .join("")
    : '<p class="muted">No linked people in demo data.</p>';

  const projects = detail.projects.length
    ? detail.projects
        .map(
          (project) => `
            <div class="project-item">
              <strong>${project.name}</strong>
              <span>${project.status} · ${project.key_date}</span>
              <span>${project.description}</span>
            </div>
          `,
        )
        .join("")
    : '<p class="muted">No active projects in demo data.</p>';

  const claims = detail.claims.length
    ? detail.claims
        .slice(0, 6)
        .map(
          (claim) => `
            <div class="claim-item">
              <strong>${claim.subject_label} → ${claim.predicate} → ${claim.object_label}</strong>
              <span>${claim.confidence} · ${claim.source_document.title}</span>
              <span>${claim.source_document.access_date}</span>
            </div>
          `,
        )
        .join("")
    : '<p class="muted">No claim evidence available.</p>';

  const lenders = detail.lenders.length
    ? detail.lenders
        .map(
          (lender) => `
            <div class="source-item">
              <strong>${lender.name}</strong>
              <span>${currency.format(lender.amount)} · ${lender.confidence}</span>
            </div>
          `,
        )
        .join("")
    : '<p class="muted">No financing records linked in the demo.</p>';

  document.getElementById("propertyDetail").innerHTML = `
    <div class="detail-title-row">
      <div>
        <p class="section-kicker">Property DNA</p>
        <h2>${detail.property.name}</h2>
        <p class="muted">${detail.property.address}</p>
      </div>
      <div class="pill">${detail.property.priority_tier} · ${detail.property.priority_score}</div>
    </div>

    <div class="detail-meta">
      <div class="detail-block">
        <h3>Parcel + building</h3>
        <div class="metric-list">
          <div class="metric-row"><span>Parcel PIN</span><strong>${detail.parcel.pin}</strong></div>
          <div class="metric-row"><span>Class code</span><strong>${detail.parcel.property_class_code}</strong></div>
          <div class="metric-row"><span>Classification</span><strong>${detail.building.primary_classification}</strong></div>
          <div class="metric-row"><span>Total assessed value</span><strong>${currency.format(detail.parcel.total_assessed_value)}</strong></div>
          <div class="metric-row"><span>Square feet</span><strong>${number.format(detail.building.square_feet)}</strong></div>
        </div>
      </div>
      <div class="detail-block">
        <h3>Scoring breakdown</h3>
        <div class="metric-list">${metricRows(detail)}</div>
      </div>
    </div>

    <div class="detail-block">
      <h3>Ownership + financing</h3>
      <div class="source-list">
        <div class="source-item">
          <strong>${detail.owner.name}</strong>
          <span>${detail.owner.category} · ${detail.owner.confidence}</span>
          <span>Registered agent: ${detail.owner.registered_agent}</span>
        </div>
        ${lenders}
      </div>
    </div>

    <div class="detail-block">
      <h3>People connected</h3>
      <div class="people-list">${people}</div>
    </div>

    <div class="detail-block">
      <h3>Projects</h3>
      <div class="project-list">${projects}</div>
    </div>

    <div class="detail-block">
      <h3>Evidence log</h3>
      <div class="claim-list">${claims}</div>
    </div>
  `;
}

async function selectProperty(propertyId) {
  selectedPropertyId = propertyId;
  renderMap(demoState.properties);
  const detail = await fetchJson(`/api/properties/${propertyId}`);
  renderPropertyDetail(detail);
}

async function init() {
  demoState = await fetchJson("/api/demo");
  renderSummary(demoState.summary);
  renderPhases(demoState.phases);
  renderCitySummaries(demoState.city_summaries);
  renderPropertyTable(demoState.properties);
  renderTargets(demoState.targets);
  renderSources(demoState.source_adapters);
  renderLegend(demoState.properties);
  selectedPropertyId = demoState.properties[0]?.id || null;
  renderMap(demoState.properties);
  if (selectedPropertyId) {
    await selectProperty(selectedPropertyId);
  }
}

window.addEventListener("DOMContentLoaded", () => {
  init().catch((error) => {
    document.getElementById("propertyDetail").innerHTML = `
      <div class="detail-block">
        <h3>Failed to load demo</h3>
        <p>${error.message}</p>
      </div>
    `;
  });
});
