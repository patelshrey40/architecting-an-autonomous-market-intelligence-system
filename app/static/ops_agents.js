const opsState = {
  runs: [],
  selectedRunId: new URLSearchParams(window.location.search).get("run") || null,
};

async function fetchOpsJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`Request failed for ${url}`);
  }
  return response.json();
}

function formatJson(value) {
  return `<pre class="code-block">${JSON.stringify(value, null, 2)}</pre>`;
}

function opsQuery() {
  const params = new URLSearchParams(window.location.search);
  return {
    workflow_type: params.get("workflow_type") || "",
    status: params.get("status") || "",
    review_state: params.get("review_state") || "",
    entity_id: params.get("entity_id") || "",
    persona: params.get("persona") || "",
    promotion_state: params.get("promotion_state") || "",
    run: params.get("run") || "",
  };
}

function setOpsQuery(next) {
  const params = new URLSearchParams();
  Object.entries(next).forEach(([key, value]) => {
    if (value !== null && value !== undefined && String(value).trim() !== "") {
      params.set(key, value);
    }
  });
  window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
}

function initOpsFilters() {
  const query = opsQuery();
  document.getElementById("workflowFilter").value = query.workflow_type;
  document.getElementById("runStatusFilter").value = query.status;
  document.getElementById("reviewStateFilter").value = query.review_state;
  document.getElementById("entityFilter").value = query.entity_id;
  document.getElementById("personaFilter").value = query.persona;
  document.getElementById("promotionStateFilter").value = query.promotion_state;
  document.getElementById("applyOpsFilters").addEventListener("click", () => {
    const next = {
      workflow_type: document.getElementById("workflowFilter").value.trim(),
      status: document.getElementById("runStatusFilter").value,
      review_state: document.getElementById("reviewStateFilter").value,
      entity_id: document.getElementById("entityFilter").value.trim(),
      persona: document.getElementById("personaFilter").value,
      promotion_state: document.getElementById("promotionStateFilter").value,
      run: opsState.selectedRunId || "",
    };
    setOpsQuery(next);
    loadRuns();
  });
}

async function loadRuns() {
  const query = opsQuery();
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (key !== "run" && value) {
      params.set(key, value);
    }
  });
  document.getElementById("opsStatus").textContent = "Loading agent runs…";
  const runs = await fetchOpsJson(`/api/agent-runs?${params.toString()}`);
  opsState.runs = runs;
  renderRunList();
  document.getElementById("opsStatus").textContent = `${runs.length} swarm runs`;
  if (!opsState.selectedRunId && runs.length) {
    opsState.selectedRunId = runs[0].id;
  }
  if (opsState.selectedRunId) {
    await loadRunDetail(opsState.selectedRunId);
  }
}

function renderRunList() {
  const container = document.getElementById("opsRunList");
  if (!opsState.runs.length) {
    container.innerHTML = `
      <div class="detail-card">
        <h3>No runs</h3>
        <p class="muted">No agent runs matched the current filters.</p>
      </div>
    `;
    return;
  }
  container.innerHTML = opsState.runs
    .map(
      (run) => `
        <button class="ops-run-item ${run.id === opsState.selectedRunId ? "ops-run-item-active" : ""}" data-run-id="${run.id}">
          <strong>${run.entity_id}</strong>
          <span>${run.workflow_type}</span>
          <span>${run.status.replaceAll("_", " ")} · review ${run.review_state.replaceAll("_", " ")}</span>
          <span>${run.current_step || "No current step"}</span>
        </button>
      `,
    )
    .join("");
  container.querySelectorAll("[data-run-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      opsState.selectedRunId = button.dataset.runId;
      const next = { ...opsQuery(), run: opsState.selectedRunId };
      setOpsQuery(next);
      renderRunList();
      await loadRunDetail(opsState.selectedRunId);
    });
  });
}

async function postReviewAction(proposalId, approved) {
  const url = approved
    ? `/api/agent-reviews/${proposalId}/approve`
    : `/api/agent-reviews/${proposalId}/reject`;
  await fetchOpsJson(url, { method: "POST" });
  await loadRunDetail(opsState.selectedRunId);
  await loadRuns();
}

async function runAction(runId, action) {
  await fetchOpsJson(`/api/agent-runs/${runId}/${action}`, { method: "POST" });
  await loadRuns();
  await loadRunDetail(runId);
}

async function loadRunDetail(runId) {
  const [run, tasks, artifacts, proposals] = await Promise.all([
    fetchOpsJson(`/api/agent-runs/${runId}`),
    fetchOpsJson(`/api/agent-runs/${runId}/tasks`),
    fetchOpsJson(`/api/agent-runs/${runId}/artifacts`),
    fetchOpsJson(`/api/agent-runs/${runId}/proposals`),
  ]);

  const runActions = `
    <div class="badge-row">
      <button class="primary-button ops-inline-button" type="button" id="retryRunButton">Retry run</button>
      <button class="primary-button ops-inline-button ops-danger-button" type="button" id="cancelRunButton">Cancel run</button>
    </div>
  `;

  const taskList = tasks.length
    ? tasks
        .map(
          (task) => `
            <div class="list-item">
              <strong>${task.task_type}</strong>
              <div>${task.status} · attempt ${task.attempt}</div>
              ${task.error_message ? `<div>${task.error_message}</div>` : ""}
              ${task.output_payload && Object.keys(task.output_payload).length ? formatJson(task.output_payload) : ""}
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No tasks were recorded for this run.</div>';

  const artifactList = artifacts.length
    ? artifacts
        .map(
          (artifact) => `
            <div class="list-item">
              <strong>${artifact.artifact_type}</strong>
              ${artifact.source_document_id ? `<div>Source document: ${artifact.source_document_id}</div>` : ""}
              ${formatJson(artifact.payload)}
            </div>
          `,
        )
        .join("")
    : '<div class="list-item">No artifacts were recorded for this run.</div>';

  const proposalList = proposals.length
    ? proposals
        .map((proposal) => {
          const validationList = proposal.validations.length
            ? proposal.validations
                .map(
                  (validation) => `
                    <div class="list-item">
                      <strong>${validation.validator_name}</strong>
                      <div>${validation.outcome}</div>
                      <div>${validation.message}</div>
                    </div>
                  `,
                )
                .join("")
            : '<div class="list-item">No validations recorded.</div>';
          const reviewControls =
            proposal.review_state === "pending"
              ? `
                <div class="badge-row">
                  <button class="primary-button ops-inline-button" type="button" data-approve-proposal="${proposal.id}">Approve</button>
                  <button class="primary-button ops-inline-button ops-danger-button" type="button" data-reject-proposal="${proposal.id}">Reject</button>
                </div>
              `
              : "";
          return `
            <div class="detail-card">
              <div class="detail-head">
                <div>
                  <h3>${proposal.proposal_type}</h3>
                  <p class="muted">${proposal.status} · review ${proposal.review_state}</p>
                </div>
                <div class="pill">${proposal.decision || "no decision"}</div>
              </div>
              ${formatJson(proposal.payload)}
              <div class="detail-subsection">
                <h4>Validations</h4>
                <div class="list-stack">${validationList}</div>
              </div>
              ${
                proposal.review
                  ? `
                    <div class="detail-subsection">
                      <h4>Review</h4>
                      <div class="list-stack">
                        <div class="list-item">
                          <strong>${proposal.review.status}</strong>
                          <div>${proposal.review.notes || "No notes"}</div>
                        </div>
                      </div>
                    </div>
                  `
                  : ""
              }
              ${reviewControls}
            </div>
          `;
        })
        .join("")
    : '<div class="list-item">No proposals were recorded for this run.</div>';

  document.getElementById("opsDetailPanel").innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <div class="detail-head">
          <div>
            <p class="section-kicker">Run detail</p>
            <h2>${run.id}</h2>
            <p class="muted">${run.workflow_type} · ${run.entity_id}</p>
          </div>
          <div class="badge-row">
            <span class="pill">${run.status.replaceAll("_", " ")}</span>
            <span class="pill pill-muted">review ${run.review_state.replaceAll("_", " ")}</span>
          </div>
        </div>
        <div class="metrics-grid">
          <div class="metric-item"><span>Current step</span><strong>${run.current_step || "Unavailable"}</strong></div>
          <div class="metric-item"><span>Tasks</span><strong>${run.task_count}</strong></div>
          <div class="metric-item"><span>Artifacts</span><strong>${run.artifact_count}</strong></div>
          <div class="metric-item"><span>Proposals</span><strong>${run.proposal_count}</strong></div>
          <div class="metric-item"><span>Pending reviews</span><strong>${run.pending_review_count}</strong></div>
          <div class="metric-item"><span>Requested</span><strong>${run.requested_at || "Unavailable"}</strong></div>
        </div>
        ${runActions}
      </section>

      <section class="detail-card">
        <h3>Run events</h3>
        <div class="list-stack">
          ${
            run.events.length
              ? run.events
                  .map(
                    (event) => `
                      <div class="list-item">
                        <strong>${event.step_name}</strong>
                        <div>${event.status}</div>
                        <div>${event.message}</div>
                      </div>
                    `,
                  )
                  .join("")
              : '<div class="list-item">No events were recorded.</div>'
          }
        </div>
      </section>

      <section class="detail-card">
        <h3>Tasks</h3>
        <div class="list-stack">${taskList}</div>
      </section>

      <section class="detail-card">
        <h3>Artifacts</h3>
        <div class="list-stack">${artifactList}</div>
      </section>

      <section class="detail-card">
        <h3>Proposals</h3>
        <div class="detail-stack">${proposalList}</div>
      </section>
    </div>
  `;

  document.getElementById("retryRunButton").addEventListener("click", () => runAction(runId, "retry"));
  document.getElementById("cancelRunButton").addEventListener("click", () => runAction(runId, "cancel"));
  document.querySelectorAll("[data-approve-proposal]").forEach((button) => {
    button.addEventListener("click", () => postReviewAction(button.dataset.approveProposal, true));
  });
  document.querySelectorAll("[data-reject-proposal]").forEach((button) => {
    button.addEventListener("click", () => postReviewAction(button.dataset.rejectProposal, false));
  });
}

window.addEventListener("DOMContentLoaded", () => {
  initOpsFilters();
  loadRuns().catch((error) => {
    document.getElementById("opsStatus").textContent = error.message;
    document.getElementById("opsDetailPanel").innerHTML = `
      <div class="detail-card">
        <h3>Unable to load agent ops</h3>
        <p>${error.message}</p>
      </div>
    `;
  });
});
