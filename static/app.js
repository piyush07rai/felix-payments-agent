/**
 * Payments Decline Analyst Agent - Complete Frontend Dashboard Controller
 * Connects to actual backend APIs and renders live Chart.js visualizations.
 */

// Global Chart Instances to prevent canvas collision
let chartApprovalTime = null;
let chartResponseCodes = null;
let chartNetwork = null;
let chartChannel = null;

// Global state
let currentFindings = [];
let totalActivityCount = 0;
let currentActiveRole = "admin";

document.addEventListener("DOMContentLoaded", () => {
  initNavigation();
  initModelVersioning();
  initUploadForm();
  initGroundTruthUpload();
  initChatInterface();
  initPrivacyShield();
  initIntegrationsDashboard();
  initRoleSwitcher();
  initActionApprovalFramework();
  refreshAllDashboardData();
});

/**
 * Configure tab navigation between Overview, AI Investigation, Findings, and Evaluation.
 */
function initNavigation() {
  const tabs = document.querySelectorAll(".nav-tab");
  const panes = document.querySelectorAll(".tab-pane");

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const targetTab = tab.getAttribute("data-tab");

      tabs.forEach((t) => {
        t.classList.remove("active");
        t.setAttribute("aria-selected", "false");
      });
      panes.forEach((p) => p.classList.remove("active"));

      tab.classList.add("active");
      tab.setAttribute("aria-selected", "true");

      const activePane = document.getElementById(`tab-${targetTab}`);
      if (activePane) {
        activePane.classList.add("active");
      }

      // If switching to Overview, trigger chart resize/redraw
      if (targetTab === "overview") {
        resizeCharts();
      }
      // If switching to Findings, refresh action proposals
      if (targetTab === "findings") {
        fetchActionProposals();
      }
      // If switching to Evaluation, refresh evaluation status
      if (targetTab === "evaluation") {
        fetchEvaluationStatus();
      }
      // If switching to Privacy Shield, refresh privacy data
      if (targetTab === "privacy") {
        fetchPrivacyData();
      }
      // If switching to Enterprise Integrations, refresh integrations data
      if (targetTab === "integrations") {
        fetchIntegrationsOverview();
        fetchIntegrationActivities();
      }
    });
  });

  // "Go to AI Investigation" shortcut button from Findings empty state
  const goToInvestBtn = document.getElementById("go-to-investigate-btn");
  if (goToInvestBtn) {
    goToInvestBtn.addEventListener("click", () => {
      const investTab = document.querySelector('.nav-tab[data-tab="investigation"]');
      if (investTab) investTab.click();
    });
  }
}

/**
 * Admin Multi-Model Selector & Resiliency Controls (Gemini / ChatGPT / Local Fallback + Auto-Failover).
 */
function initModelVersioning() {
  const modelSelect = document.getElementById("model-provider-select");
  const failoverBtn = document.getElementById("failover-toggle-btn");
  const viewLogsBtn = document.getElementById("view-system-logs-btn");
  const logsModal = document.getElementById("system-logs-modal");
  const closeLogsBtn = document.getElementById("close-logs-modal-btn");
  const refreshLogsBtn = document.getElementById("refresh-logs-btn");
  const clearLogsBtn = document.getElementById("clear-logs-btn");
  const logsContent = document.getElementById("system-logs-content");

  let isFailoverEnabled = true;

  // 1. Fetch initial configuration from backend
  fetch("/api/config/model")
    .then(r => r.json())
    .then(d => {
      if (modelSelect && d.provider && d.model) {
        const targetVal = `${d.provider}:${d.model}`;
        for (const opt of modelSelect.options) {
          if (opt.value === targetVal || opt.value.startsWith(`${d.provider}:`)) {
            modelSelect.value = opt.value;
            break;
          }
        }
      }
      if (d.enable_failover !== undefined) {
        isFailoverEnabled = Boolean(d.enable_failover);
        updateFailoverBadge();
      }
    })
    .catch(() => {});

  // 2. Model selection change
  if (modelSelect) {
    modelSelect.addEventListener("change", async () => {
      const parts = modelSelect.value.split(":");
      const provider = parts[0];
      const model = parts[1] || "";

      try {
        const resp = await fetch("/api/config/model", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            provider: provider,
            model: model,
            enable_failover: isFailoverEnabled
          })
        });
        const data = await resp.json();
        if (resp.ok && data.success) {
          await fetchSystemStatus();
        } else {
          alert(data.detail || "Failed to switch AI model.");
        }
      } catch (err) {
        alert(`Error updating AI model: ${err.message}`);
      }
    });
  }

  // 3. Failover toggle
  function updateFailoverBadge() {
    if (!failoverBtn) return;
    if (isFailoverEnabled) {
      failoverBtn.style.background = "#ecfdf5";
      failoverBtn.style.color = "#047857";
      failoverBtn.style.borderColor = "#a7f3d0";
      failoverBtn.textContent = "⚡ Failover: ON";
      failoverBtn.title = "Automatic Failover active: Seamlessly switches if primary model times out or hits 500 error.";
    } else {
      failoverBtn.style.background = "#f1f5f9";
      failoverBtn.style.color = "#64748b";
      failoverBtn.style.borderColor = "#cbd5e1";
      failoverBtn.textContent = "⚡ Failover: OFF";
      failoverBtn.title = "Failover disabled: Only the primary model will be invoked.";
    }
  }

  if (failoverBtn) {
    failoverBtn.addEventListener("click", async () => {
      isFailoverEnabled = !isFailoverEnabled;
      updateFailoverBadge();
      try {
        await fetch("/api/config/model", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enable_failover: isFailoverEnabled })
        });
      } catch (e) {
        console.warn("Failed to persist failover toggle", e);
      }
    });
  }

  // 4. System Logs Modal & Live Polling
  async function loadSystemLogs() {
    if (!logsContent) return;
    logsContent.textContent = "Loading real-time system logs...";
    try {
      const resp = await fetch("/api/logs?limit=150");
      const data = await resp.json();
      if (data.logs && data.logs.length > 0) {
        logsContent.textContent = data.logs.map(entry => {
          const dt = entry.timestamp ? entry.timestamp.split("T")[1].replace("Z", "") : "";
          const cat = entry.category || "INFO";
          const msg = entry.message || "";
          const det = entry.details && Object.keys(entry.details).length > 0 ? " " + JSON.stringify(entry.details) : "";
          return `[${dt}] [${cat}] ${msg}${det}`;
        }).join("\n");
        logsContent.scrollTop = logsContent.scrollHeight;
      } else {
        logsContent.textContent = "No lifecycle logs recorded yet. Send a query via Slack, Teams, or Web Chat to observe live logs.";
      }
    } catch (err) {
      logsContent.textContent = `Error loading logs: ${err.message}`;
    }
  }

  if (viewLogsBtn && logsModal) {
    viewLogsBtn.addEventListener("click", () => {
      logsModal.style.display = "flex";
      loadSystemLogs();
    });
  }

  if (closeLogsBtn && logsModal) {
    closeLogsBtn.addEventListener("click", () => {
      logsModal.style.display = "none";
    });
  }

  if (refreshLogsBtn) {
    refreshLogsBtn.addEventListener("click", () => {
      loadSystemLogs();
    });
  }

  if (clearLogsBtn) {
    clearLogsBtn.addEventListener("click", async () => {
      if (confirm("Clear in-memory request lifecycle logs?")) {
        await fetch("/api/logs", { method: "DELETE" });
        loadSystemLogs();
      }
    });
  }
}

/**
 * Refresh all dashboard components, status badges, metrics, and charts.
 */
async function refreshAllDashboardData() {
  await Promise.all([
    fetchSystemStatus(),
    fetchDataStatusAndAnalytics(),
    fetchEvaluationStatus()
  ]);
}

/**
 * Fetch application and agent health status from backend API.
 */
async function fetchSystemStatus() {
  const agentBadge = document.getElementById("agent-status-badge");
  const agentText = document.getElementById("agent-status-text");

  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error(`HTTP error ${response.status}`);
    const data = await response.json();

    if (data.gemini) {
      const dot = agentBadge.querySelector(".status-dot");
      if (data.gemini.configured) {
        dot.className = "status-dot active";
        agentText.textContent = `Gemini: Ready (${data.gemini.model})`;
      } else {
        dot.className = "status-dot neutral";
        agentText.textContent = "Gemini: API Key Required";
      }
    }
  } catch (err) {
    console.warn("Could not retrieve system status:", err);
    agentText.textContent = "Status: Server Offline";
  }
}

/**
 * Fetch data status and actual transaction analytics for Overview.
 */
async function fetchDataStatusAndAnalytics() {
  const dataBadge = document.getElementById("data-status-badge");
  const dataText = document.getElementById("data-status-text");
  const noDataNotice = document.getElementById("overview-no-data-notice");
  const metricsGrid = document.getElementById("overview-metrics-grid");
  const chartsGrid = document.getElementById("overview-charts-grid");
  const investWarning = document.getElementById("investigation-no-data-banner");
  const chatInput = document.getElementById("chat-input");
  const chatSubmitBtn = document.getElementById("chat-submit-btn");
  const autonomousBtn = document.getElementById("run-autonomous-btn");
  const clearBtn = document.getElementById("clear-db-btn");

  try {
    const response = await fetch("/api/analytics/overview");
    if (!response.ok) throw new Error(`HTTP error ${response.status}`);
    const data = await response.json();

    const dot = dataBadge.querySelector(".status-dot");

    if (data.dataset_loaded && data.transaction_count > 0) {
      // Data is present
      dot.className = "status-dot active";
      dataText.textContent = `Data: ${data.transaction_count.toLocaleString()} Loaded`;

      if (noDataNotice) noDataNotice.style.display = "none";
      if (metricsGrid) metricsGrid.style.display = "grid";
      if (chartsGrid) chartsGrid.style.display = "grid";
      if (investWarning) investWarning.style.display = "none";
      if (clearBtn) clearBtn.style.display = "inline-flex";

      // Unlock investigation inputs
      if (chatInput) chatInput.disabled = false;
      if (chatSubmitBtn) chatSubmitBtn.disabled = false;
      if (autonomousBtn) autonomousBtn.disabled = false;

      // Update KPI Cards with actual metrics
      if (data.metrics) {
        document.getElementById("kpi-total-txns").textContent = data.metrics.total_transactions.toLocaleString();
        document.getElementById("kpi-approval-rate").textContent = `${data.metrics.approval_rate}%`;
        document.getElementById("kpi-decline-rate").textContent = `${data.metrics.decline_rate}%`;
        document.getElementById("kpi-technical-rate").textContent = `${data.metrics.technical_failure_rate}%`;
        document.getElementById("kpi-business-rate").textContent = `${data.metrics.business_decline_rate}%`;
      }

      // Render actual charts
      if (data.charts) {
        renderCharts(data.charts);
      }
    } else {
      // Data is missing
      dot.className = "status-dot neutral";
      dataText.textContent = "Data: No Dataset";

      if (noDataNotice) noDataNotice.style.display = "block";
      if (metricsGrid) metricsGrid.style.display = "none";
      if (chartsGrid) chartsGrid.style.display = "none";
      if (investWarning) investWarning.style.display = "flex";
      if (clearBtn) clearBtn.style.display = "none";

      // Lock investigation inputs
      if (chatInput) chatInput.disabled = true;
      if (chatSubmitBtn) chatSubmitBtn.disabled = true;
      if (autonomousBtn) autonomousBtn.disabled = true;

      destroyCharts();
    }
  } catch (err) {
    console.warn("Could not retrieve overview analytics:", err);
    dataText.textContent = "Data: Status Error";
  }
}

/**
 * Fetch and render Evaluation status.
 */
async function fetchEvaluationStatus() {
  const missingNotice = document.getElementById("eval-missing-notice");
  const actionBar = document.getElementById("eval-action-bar");
  const resultsContainer = document.getElementById("eval-results-container");
  const gtBadge = document.getElementById("gt-status-badge");

  try {
    const res = await fetch("/api/evaluation");
    if (!res.ok) return;
    const data = await res.json();

    if (data.evaluation_status === "unavailable" || !data.ground_truth_loaded) {
      if (missingNotice) missingNotice.style.display = "block";
      if (actionBar) actionBar.style.display = "none";
      if (resultsContainer) resultsContainer.style.display = "none";
      if (gtBadge) {
        gtBadge.textContent = "Ground Truth: Missing";
        gtBadge.style.color = "var(--danger)";
      }
    } else {
      if (missingNotice) missingNotice.style.display = "none";
      if (actionBar) actionBar.style.display = "block";
      if (gtBadge) {
        gtBadge.textContent = "Ground Truth: Loaded";
        gtBadge.style.color = "var(--success)";
      }

      if (data.latest_evaluation && data.latest_evaluation.metrics) {
        renderEvaluationResults(data.latest_evaluation);
      }
    }
  } catch (err) {
    console.warn("Could not retrieve evaluation status:", err);
  }
}

/**
 * Configure Ground Truth Upload form.
 */
function initGroundTruthUpload() {
  const form = document.getElementById("gt-upload-form");
  const fileInput = document.getElementById("gt-file-input");
  const uploadBtn = document.getElementById("gt-upload-btn");
  const successAlert = document.getElementById("gt-alert-success");
  const successText = document.getElementById("gt-success-text");
  const errorAlert = document.getElementById("gt-alert-error");
  const errorText = document.getElementById("gt-error-text");
  const runEvalBtn = document.getElementById("run-eval-btn");

  fileInput.addEventListener("change", () => {
    uploadBtn.disabled = !(fileInput.files && fileInput.files[0]);
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!fileInput.files || !fileInput.files[0]) return;

    const file = fileInput.files[0];
    if (!file.name.toLowerCase().endsWith(".json")) {
      errorAlert.style.display = "block";
      errorText.textContent = "Please select a valid .json ground-truth file.";
      successAlert.style.display = "none";
      return;
    }

    uploadBtn.disabled = true;
    uploadBtn.textContent = "Uploading...";
    successAlert.style.display = "none";
    errorAlert.style.display = "none";

    const formData = new FormData();
    formData.append("file", file);

    try {
      const resp = await fetch("/api/ground-truth/upload", {
        method: "POST",
        body: formData
      });
      const data = await resp.json();

      if (resp.ok && data.success) {
        successAlert.style.display = "block";
        successText.textContent = data.message || "Ground truth uploaded successfully.";
        fileInput.value = "";
        await fetchEvaluationStatus();
      } else {
        errorAlert.style.display = "block";
        errorText.textContent = data.detail || data.message || "Failed to upload ground truth.";
      }
    } catch (err) {
      errorAlert.style.display = "block";
      errorText.textContent = `Upload error: ${err.message}`;
    } finally {
      uploadBtn.disabled = false;
      uploadBtn.textContent = "Upload Ground Truth";
    }
  });

  // Run Evaluation Button
  if (runEvalBtn) {
    runEvalBtn.addEventListener("click", async () => {
      runEvalBtn.disabled = true;
      runEvalBtn.textContent = "Evaluating...";

      try {
        const resp = await fetch("/api/evaluate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        });
        const data = await resp.json();

        if (resp.ok && data.evaluation_status === "evaluated") {
          renderEvaluationResults(data);
        } else {
          alert(`Evaluation Notice: ${data.message || data.detail || "Unable to run evaluation."}`);
        }
      } catch (err) {
        alert(`Evaluation error: ${err.message}`);
      } finally {
        runEvalBtn.disabled = false;
        runEvalBtn.textContent = "Run Evaluation Benchmark";
      }
    });
  }
}

function renderEvaluationResults(evalData) {
  const container = document.getElementById("eval-results-container");
  if (!container || !evalData || !evalData.metrics) return;

  const m = evalData.metrics;
  document.getElementById("eval-detection-rate").textContent = `${m.detection_rate}%`;
  document.getElementById("eval-precision").textContent = `${m.precision}%`;
  document.getElementById("eval-recall").textContent = `${m.recall}%`;
  document.getElementById("eval-false-positives").textContent = m.false_positives;
  document.getElementById("eval-runtime").textContent = `${m.runtime_seconds}s`;
  document.getElementById("eval-sql-count").textContent = m.sql_query_count;
  document.getElementById("eval-steps").textContent = m.agent_steps;
  document.getElementById("eval-cost").textContent = `$${m.estimated_cost_usd.toFixed(4)}`;

  const tbody = document.getElementById("eval-matrix-tbody");
  if (tbody && evalData.ground_truth_issues) {
    tbody.innerHTML = "";
    evalData.ground_truth_issues.forEach(gt => {
      const tr = document.createElement("tr");
      const tag = gt.detected
        ? `<span class="tag-detected">✓ Detected</span>`
        : `<span class="tag-missed">✕ Missed</span>`;

      tr.innerHTML = `
        <td style="font-weight: 700; color: var(--text-secondary);">${escapeHtml(gt.id)}</td>
        <td style="font-weight: 600;">${escapeHtml(gt.title)}</td>
        <td><code>${escapeHtml(gt.dimension)}: ${escapeHtml(gt.dimension_value)}</code></td>
        <td><span class="finding-cause-tag">${escapeHtml(gt.cause_category)}</span></td>
        <td>${tag}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  container.style.display = "block";
}

/**
 * Render Chart.js charts using actual aggregated backend analytics.
 */
function renderCharts(chartsData) {
  // 1. Approval Rate Over Time
  const timeCtx = document.getElementById("chart-approval-time");
  if (timeCtx && chartsData.approval_over_time) {
    if (chartApprovalTime) chartApprovalTime.destroy();

    const labels = chartsData.approval_over_time.map(d => d.time_bucket.replace("T", " "));
    const values = chartsData.approval_over_time.map(d => d.approval_rate);

    chartApprovalTime = new Chart(timeCtx, {
      type: "line",
      data: {
        labels: labels,
        datasets: [{
          label: "Approval Rate (%)",
          data: values,
          borderColor: "#2563eb",
          backgroundColor: "rgba(37, 99, 235, 0.08)",
          fill: true,
          tension: 0.25,
          borderWidth: 2,
          pointRadius: 3,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { min: 0, max: 100, ticks: { callback: v => v + "%" } }
        }
      }
    });
  }

  // 2. Response-Code Distribution
  const codeCtx = document.getElementById("chart-response-codes");
  if (codeCtx && chartsData.response_code_distribution) {
    if (chartResponseCodes) chartResponseCodes.destroy();

    const labels = chartsData.response_code_distribution.map(d => `Code ${d.response_code}`);
    const counts = chartsData.response_code_distribution.map(d => d.count);

    chartResponseCodes = new Chart(codeCtx, {
      type: "doughnut",
      data: {
        labels: labels,
        datasets: [{
          data: counts,
          backgroundColor: [
            "#16a34a", // 00 Approved (green)
            "#d97706", // 05 Do Not Honor (orange)
            "#0284c7", // 51 Insufficient funds (blue)
            "#dc2626", // 91 Timeout (red)
            "#9333ea", // 96 Malfunction (purple)
            "#64748b", "#cbd5e1", "#475569"
          ]
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "right", labels: { boxWidth: 12, font: { size: 11 } } }
        }
      }
    });
  }

  // 3. Approval Rate by Network
  const netCtx = document.getElementById("chart-approval-network");
  if (netCtx && chartsData.approval_by_network) {
    if (chartNetwork) chartNetwork.destroy();

    const labels = chartsData.approval_by_network.map(d => d.network);
    const rates = chartsData.approval_by_network.map(d => d.approval_rate);

    chartNetwork = new Chart(netCtx, {
      type: "bar",
      data: {
        labels: labels,
        datasets: [{
          label: "Approval Rate (%)",
          data: rates,
          backgroundColor: "#3b82f6",
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { min: 0, max: 100, ticks: { callback: v => v + "%" } }
        }
      }
    });
  }

  // 4. Approval Rate by Channel
  const chanCtx = document.getElementById("chart-approval-channel");
  if (chanCtx && chartsData.approval_by_channel) {
    if (chartChannel) chartChannel.destroy();

    const labels = chartsData.approval_by_channel.map(d => d.channel);
    const rates = chartsData.approval_by_channel.map(d => d.approval_rate);

    chartChannel = new Chart(chanCtx, {
      type: "bar",
      data: {
        labels: labels,
        datasets: [{
          label: "Approval Rate (%)",
          data: rates,
          backgroundColor: "#0ea5e9",
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { min: 0, max: 100, ticks: { callback: v => v + "%" } }
        }
      }
    });
  }
}

function destroyCharts() {
  if (chartApprovalTime) { chartApprovalTime.destroy(); chartApprovalTime = null; }
  if (chartResponseCodes) { chartResponseCodes.destroy(); chartResponseCodes = null; }
  if (chartNetwork) { chartNetwork.destroy(); chartNetwork = null; }
  if (chartChannel) { chartChannel.destroy(); chartChannel = null; }
}

function resizeCharts() {
  if (chartApprovalTime) chartApprovalTime.resize();
  if (chartResponseCodes) chartResponseCodes.resize();
  if (chartNetwork) chartNetwork.resize();
  if (chartChannel) chartChannel.resize();
}

/**
 * Configure file selection, drag-and-drop, and upload submission.
 */
function initUploadForm() {
  const form = document.getElementById("csv-upload-form");
  const fileInput = document.getElementById("csv-file-input");
  const dropzone = document.getElementById("dropzone");
  const selectedFileBadge = document.getElementById("selected-file-name");
  const uploadBtn = document.getElementById("upload-btn");
  const uploadBtnText = document.getElementById("upload-btn-text");
  const replaceCheckbox = document.getElementById("replace-dataset-checkbox");
  const modeIndicator = document.getElementById("upload-mode-indicator");
  const successAlert = document.getElementById("upload-alert-success");
  const successText = document.getElementById("upload-success-text");
  const errorAlert = document.getElementById("upload-alert-error");
  const errorSummary = document.getElementById("upload-error-summary");
  const errorList = document.getElementById("upload-error-list");
  const clearBtn = document.getElementById("clear-db-btn");

  if (replaceCheckbox && modeIndicator) {
    replaceCheckbox.addEventListener("change", () => {
      if (replaceCheckbox.checked) {
        modeIndicator.textContent = "Mode: Demo Reset (Purges prior transactions)";
        modeIndicator.className = "upload-mode-badge badge-reset";
      } else {
        modeIndicator.textContent = "Mode: Append-Only (Financial Integrity)";
        modeIndicator.className = "upload-mode-badge badge-append";
      }
    });
  }

  let selectedFile = null;

  function setSelectedFile(file) {
    if (!file) {
      selectedFile = null;
      selectedFileBadge.style.display = "none";
      selectedFileBadge.textContent = "";
      uploadBtn.disabled = true;
      return;
    }

    if (!file.name.toLowerCase().endsWith(".csv")) {
      showErrors("Invalid file type", ["Please select a file with a .csv extension."]);
      setSelectedFile(null);
      return;
    }

    selectedFile = file;
    selectedFileBadge.style.display = "inline-flex";
    selectedFileBadge.textContent = `${file.name} (${formatBytes(file.size)})`;
    uploadBtn.disabled = false;
    hideAlerts();
  }

  function hideAlerts() {
    successAlert.style.display = "none";
    errorAlert.style.display = "none";
    errorList.innerHTML = "";
  }

  function showErrors(summary, errors) {
    successAlert.style.display = "none";
    errorAlert.style.display = "flex";
    errorSummary.textContent = summary;
    errorList.innerHTML = "";
    (errors || []).forEach((msg) => {
      const li = document.createElement("li");
      li.textContent = msg;
      errorList.appendChild(li);
    });
  }

  function showSuccess(msg) {
    errorAlert.style.display = "none";
    successAlert.style.display = "flex";
    successText.textContent = msg;
  }

  fileInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files[0]) {
      setSelectedFile(e.target.files[0]);
    }
  });

  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("dragover");
    });
  });

  dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
      setSelectedFile(e.dataTransfer.files[0]);
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!selectedFile) return;

    hideAlerts();
    uploadBtn.disabled = true;
    uploadBtnText.textContent = "Uploading & Validating...";

    const formData = new FormData();
    formData.append("file", selectedFile);
    const replaceParam = replaceCheckbox.checked ? "true" : "false";

    try {
      const response = await fetch(`/api/data/upload?replace=${replaceParam}`, {
        method: "POST",
        body: formData,
      });
      const result = await response.json();

      if (response.ok && result.success) {
        showSuccess(result.message || `Loaded ${result.rows_loaded.toLocaleString()} transactions successfully.`);
        setSelectedFile(null);
        fileInput.value = "";
        await refreshAllDashboardData();
      } else {
        const errors = result.errors && result.errors.length > 0
          ? result.errors
          : [result.message || "An error occurred during CSV loading."];
        showErrors(result.message || "Failed to load CSV", errors);
        uploadBtn.disabled = false;
      }
    } catch (err) {
      showErrors("Network or Server Error", [err.message || "Unable to reach server."]);
      uploadBtn.disabled = false;
    } finally {
      uploadBtnText.textContent = "Upload & Validate CSV";
    }
  });

  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      if (!confirm("Reset Demo Environment:\n\nAre you sure you want to clear the transaction dataset to prepare for a fresh demo presentation?")) return;
      try {
        const resp = await fetch("/api/data/clear", { method: "DELETE" });
        const data = await resp.json();
        if (resp.ok) {
          hideAlerts();
          showSuccess(data.message || "Demo database reset successfully. Immutability protections re-armed for the next demo.");
          await refreshAllDashboardData();
        } else {
          showErrors("Reset Error", [data.detail || data.message || "Failed to reset demo database."]);
        }
      } catch (err) {
        showErrors("Error resetting database", [err.message]);
      }
    });
  }
}

/**
 * Configure AI Investigation chat interface, prompt chips, and activity log.
 */
function initChatInterface() {
  const chatForm = document.getElementById("chat-form");
  const chatInput = document.getElementById("chat-input");
  const chatSubmitBtn = document.getElementById("chat-submit-btn");
  const chatStream = document.getElementById("chat-stream");
  const chips = document.querySelectorAll(".chip-btn");
  const autonomousBtn = document.getElementById("run-autonomous-btn");

  let activeProgressBubble = null;

  function updateLiveChatProgress(ev) {
    if (!chatStream) return;
    if (!activeProgressBubble) {
      activeProgressBubble = document.createElement("div");
      activeProgressBubble.className = "chat-message assistant-message live-progress-bubble";
      activeProgressBubble.innerHTML = `
        <div class="message-sender">
          <span class="sender-avatar">AI</span>
          <span id="progress-agent-label">Felix Decline Analyst</span>
        </div>
        <div class="message-bubble" style="display: flex; align-items: center; gap: 12px; background: #f8fafc; border: 1px dashed #3b82f6; padding: 12px 16px; border-radius: 8px;">
          <div class="spinner-sm" style="width: 20px; height: 20px; border: 2px solid #cbd5e1; border-top-color: #3b82f6; border-radius: 50%; animation: spin 0.8s linear infinite; flex-shrink: 0;"></div>
          <div style="flex: 1;">
            <div style="font-weight: 600; font-size: 0.82rem; color: #1e40af; display: flex; align-items: center; gap: 6px;">
              <span id="chat-step-badge" style="background: #2563eb; color: #fff; padding: 1px 6px; border-radius: 4px; font-size: 0.72rem;">Step 1/15</span>
              <span id="chat-stage-label" style="text-transform: uppercase; font-size: 0.72rem; color: #64748b;">ANALYZING</span>
            </div>
            <div id="chat-progress-desc" style="font-size: 0.82rem; color: #1e293b; margin-top: 4px; font-weight: 500;">
              Formulating investigative hypothesis and analytical strategy...
            </div>
          </div>
        </div>
      `;
      chatStream.appendChild(activeProgressBubble);
      chatStream.scrollTop = chatStream.scrollHeight;
    }

    const badge = activeProgressBubble.querySelector("#chat-step-badge");
    const stage = activeProgressBubble.querySelector("#chat-stage-label");
    const desc = activeProgressBubble.querySelector("#chat-progress-desc");
    const agentLbl = activeProgressBubble.querySelector("#progress-agent-label");

    if (ev.step && badge) badge.textContent = `Step ${ev.step}/${ev.max_steps || 15}`;
    if ((ev.stage || ev.status) && stage) stage.textContent = (ev.stage || ev.status).toUpperCase().replace(/_/g, " ");
    const text = ev.action_text || ev.message;
    if (text && desc) desc.textContent = text;
    const agName = ev.agent_name || ev.agent;
    if (agName && agentLbl) agentLbl.textContent = agName;
    chatStream.scrollTop = chatStream.scrollHeight;
  }

  function removeLiveChatProgress() {
    if (activeProgressBubble) {
      activeProgressBubble.remove();
      activeProgressBubble = null;
    }
  }

  chips.forEach((chip) => {
    chip.addEventListener("click", () => {
      const promptText = chip.getAttribute("data-prompt");
      if (chatInput) {
        chatInput.value = promptText;
        chatInput.focus();
      }
    });
  });

  if (chatForm) {
    chatForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const query = chatInput.value.trim();
      if (!query) return;

      appendChatMessage("user", query);
      chatInput.value = "";
      setChatLoading(true);

      updateLiveBanner({
        agent_name: "Felix Decline Analyst",
        step: 1,
        max_steps: 15,
        status: "thinking",
        action_text: "Formulating investigative strategy and plan..."
      });

      updateLiveChatProgress({
        step: 1,
        max_steps: 15,
        stage: "thinking",
        action_text: "Formulating investigative strategy and hypothesis..."
      });

      let finalResponseHandled = false;

      await consumeEventStream(
        "/api/chat?stream=true",
        { message: query, max_steps: 15 },
        {
          onEvent: (ev) => {
            updateLiveBanner(ev);
            updateLiveChatProgress(ev);
            appendLiveToolEvent(ev);
          },
          onError: (errMsg) => {
            removeLiveChatProgress();
            appendChatMessage("assistant", `⚠️ ${errMsg}`);
            updateLiveBanner({
              status: "facing_error_retrying",
              action_text: `Encountered error: ${errMsg}`
            });
          },
          onComplete: (data) => {
            finalResponseHandled = true;
            removeLiveChatProgress();
            const respText = data.response || "Investigation completed.";
            appendChatMessage("assistant", respText, data.privacy_audit);
            if (data.findings && data.findings.length > 0) {
              updateFindings(data.findings);
            }
            updateLiveBanner({
              status: "completed",
              action_text: "Investigation concluded. Insights ready."
            });
            fetchPrivacyData();
          }
        }
      );

      removeLiveChatProgress();
      setChatLoading(false);
    });
  }

  if (autonomousBtn) {
    autonomousBtn.addEventListener("click", async () => {
      const promptText = "Perform full decline anomaly investigation across networks, issuers, channels, and response codes.";
      appendChatMessage("user", "⚡ Launched Full Autonomous Decline Audit");
      setChatLoading(true);

      updateLiveBanner({
        agent_name: "Felix Decline Analyst",
        step: 1,
        max_steps: 15,
        status: "thinking",
        action_text: "Initializing autonomous decline investigation chain..."
      });

      updateLiveChatProgress({
        step: 1,
        max_steps: 15,
        stage: "thinking",
        action_text: "Initializing autonomous decline investigation chain..."
      });

      await consumeEventStream(
        "/api/investigate?stream=true",
        { objective: promptText, max_steps: 15 },
        {
          onEvent: (ev) => {
            updateLiveBanner(ev);
            updateLiveChatProgress(ev);
            appendLiveToolEvent(ev);
          },
          onError: (errMsg) => {
            removeLiveChatProgress();
            appendChatMessage("assistant", `⚠️ ${errMsg}`);
            updateLiveBanner({
              status: "facing_error_retrying",
              action_text: `Encountered error: ${errMsg}`
            });
          },
          onComplete: (data) => {
            removeLiveChatProgress();
            const respText = data.response || "Autonomous audit completed.";
            appendChatMessage("assistant", respText, data.privacy_audit);
            if (data.findings && data.findings.length > 0) {
              updateFindings(data.findings);
            }
            updateLiveBanner({
              status: "completed",
              action_text: "Autonomous investigation complete. Findings generated."
            });
            fetchPrivacyData();
          }
        }
      );

      removeLiveChatProgress();
      setChatLoading(false);
    });
  }

  function setChatLoading(isLoading) {
    chatSubmitBtn.disabled = isLoading;
    if (autonomousBtn) autonomousBtn.disabled = isLoading;
    if (isLoading) {
      chatSubmitBtn.innerHTML = `<span>Thinking...</span>`;
    } else {
      chatSubmitBtn.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="22" y1="2" x2="11" y2="13"></line>
          <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
        </svg>
        Send
      `;
    }
  }

  function appendChatMessage(role, text, privacyMeta = null) {
    const msgEl = document.createElement("div");
    msgEl.className = `chat-message ${role}-message`;

    const senderTitle = role === "user" ? "You (Payments PM)" : "Felix Decline Analyst Agent";
    const senderAvatar = role === "user" ? "PM" : "AI";

    let privacyHtml = "";
    if (privacyMeta && privacyMeta.total_entities_scrubbed > 0) {
      privacyHtml = `
        <div style="margin-top: 8px; font-size: 0.72rem; color: #065f46; background: #ecfdf5; border: 1px solid #a7f3d0; padding: 4px 8px; border-radius: 4px; display: inline-flex; align-items: center; gap: 4px;">
          <span>🛡️</span>
          <span><strong>Zero-PII Decoupling:</strong> ${privacyMeta.total_entities_scrubbed} entities scrubbed &amp; rehydrated locally.</span>
        </div>
      `;
    }

    msgEl.innerHTML = `
      <div class="message-sender">
        <span class="sender-avatar">${senderAvatar}</span>
        <span>${senderTitle}</span>
      </div>
      <div class="message-body">${escapeHtml(text)}${privacyHtml}</div>
    `;

    chatStream.appendChild(msgEl);
    chatStream.scrollTop = chatStream.scrollHeight;
  }
}

/**
 * Update top Live Agent Execution Banner.
 */
function updateLiveBanner(ev) {
  const banner = document.getElementById("live-agent-banner");
  const agentName = document.getElementById("live-agent-name");
  const chainStep = document.getElementById("live-chain-step");
  const actionText = document.getElementById("live-action-text");
  const stagePill = document.getElementById("live-stage-pill");

  if (!banner) return;
  banner.style.display = "flex";

  const agent = ev.agent_name || ev.agent;
  if (agent && agentName) {
    agentName.textContent = agent;
  }
  if (ev.step && chainStep) {
    chainStep.textContent = `Step ${ev.step}/${ev.max_steps || 15}`;
  }
  const text = ev.action_text || ev.message;
  if (text && actionText) {
    actionText.textContent = text;
  }

  const stage = ev.stage || ev.status;
  if (stagePill && stage) {
    stagePill.className = "stage-pill";
    if (stage === "thinking") {
      stagePill.classList.add("stage-thinking");
      stagePill.textContent = "THINKING";
    } else if (stage === "extracting_data") {
      stagePill.classList.add("stage-extracting");
      stagePill.textContent = "EXTRACTING DATA";
    } else if (stage === "facing_error_retrying") {
      stagePill.classList.add("stage-retry");
      stagePill.textContent = "FACING ERROR / RETRYING";
    } else if (stage === "drafting_action") {
      stagePill.classList.add("stage-drafting");
      stagePill.textContent = "DRAFTING ACTION";
    } else if (stage === "completed") {
      stagePill.classList.add("stage-completed");
      stagePill.textContent = "COMPLETED";
    } else {
      stagePill.classList.add("stage-thinking");
      stagePill.textContent = stage.toUpperCase().replace(/_/g, " ");
    }
  }
}

/**
 * Append live tool execution events to the activity stream in real time.
 */
function appendLiveToolEvent(ev) {
  const container = document.getElementById("agent-activity-stream");
  const counter = document.getElementById("activity-counter");
  if (!container) return;

  const placeholder = container.querySelector(".empty-activity-placeholder");
  if (placeholder) placeholder.remove();

  // If this is a tool_end event for a tool that had a tool_start, we can log the execution result
  totalActivityCount++;
  if (counter) counter.textContent = `${totalActivityCount} events`;

  const card = document.createElement("div");
  let cardClass = "activity-event-card";
  let stageTag = "";

  if (ev.type === "tool_error" || ev.stage === "facing_error_retrying") {
    cardClass += " status-error-retry";
    stageTag = `<span class="event-stage-tag tag-error">⚠️ ERROR / RETRYING</span>`;
  } else if (ev.type === "finding_drafted" || ev.stage === "drafting_action") {
    cardClass += " status-finding";
    stageTag = `<span class="event-stage-tag tag-finding">★ PM ACTION DRAFTED</span>`;
  } else if (ev.type === "tool_start" || ev.stage === "extracting_data") {
    cardClass += " status-extracting";
    stageTag = `<span class="event-stage-tag tag-extracting">EXTRACTING DATA</span>`;
  } else if (ev.type === "agent_thinking" || ev.status === "thinking") {
    cardClass += " status-thinking";
    stageTag = `<span class="event-stage-tag tag-thinking">THINKING</span>`;
  } else {
    cardClass += " status-success";
  }

  card.className = cardClass;

  const toolName = ev.tool || (ev.type === "agent_thinking" ? "chain_reasoning" : (ev.type || "agent_step"));
  const stepLabel = ev.step ? `Step ${ev.step}` : `Event ${totalActivityCount}`;
  const duration = ev.execution_time_ms ? `${ev.execution_time_ms}ms` : "";

  let detailContent = "";

  if (ev.action_text) {
    detailContent += `<div style="font-weight: 500; margin-bottom: 4px; color: var(--text-primary);">${escapeHtml(ev.action_text)}</div>`;
  }

  if (ev.inputs) {
    const inputsStr = ev.inputs.query ? ev.inputs.query : JSON.stringify(ev.inputs, null, 2);
    detailContent += `<pre class="event-code-block">${escapeHtml(inputsStr)}</pre>`;
  }

  if (ev.error) {
    detailContent += `
      <div style="background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 6px 10px; border-radius: 4px; margin-top: 6px; font-size: 0.76rem;">
        <strong>Error encountered:</strong> ${escapeHtml(ev.error)}<br/>
        <span style="font-style: italic; color: #b91c1c;">Agent is analyzing error and adapting next query...</span>
      </div>`;
  }

  if (ev.rows_returned !== undefined) {
    detailContent += `<div style="margin-top: 4px; font-size: 0.74rem; color: var(--text-muted);">Rows returned: <strong>${ev.rows_returned}</strong></div>`;
  }

  card.innerHTML = `
    <div class="event-header">
      <div style="display: flex; align-items: center;">
        <span class="event-step">${stepLabel}</span>
        ${stageTag}
      </div>
      <div style="display: flex; align-items: center; gap: 8px;">
        <span class="event-tool-name">${escapeHtml(toolName)}</span>
        ${duration ? `<span class="event-time">${duration}</span>` : ""}
      </div>
    </div>
    ${detailContent}
  `;

  container.appendChild(card);
  container.scrollTop = container.scrollHeight;

  if (ev.finding) {
    addSingleFinding(ev.finding);
  }
}

/**
 * Helper to stream Server-Sent Events from the backend.
 */
async function consumeEventStream(url, payload, handlers) {
  const { onEvent, onError, onComplete } = handlers;

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
      },
      body: JSON.stringify(payload),
    });

    const contentType = response.headers.get("content-type") || "";

    if (!contentType.includes("text/event-stream")) {
      const data = await response.json();
      if (!response.ok) {
        if (onError) onError(data.detail || data.message || "Request failed");
      } else {
        if (onComplete) onComplete(data);
      }
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith("data:")) {
          const jsonStr = trimmed.slice(5).trim();
          if (jsonStr) {
            try {
              const eventData = JSON.parse(jsonStr);
              if (onEvent) onEvent(eventData);
              if (eventData.type === "completed" && onComplete) {
                onComplete(eventData.payload || eventData);
              }
            } catch (err) {
              console.warn("Failed to parse SSE event JSON:", jsonStr, err);
            }
          }
        }
      }
    }
  } catch (err) {
    if (onError) onError(err.message || "Network stream error");
  }
}

/**
 * Render batch Agent Execution Activity events (fallback).
 */
function renderAgentActivity(events) {
  const container = document.getElementById("agent-activity-stream");
  const counter = document.getElementById("activity-counter");
  if (!container || !events) return;

  const placeholder = container.querySelector(".empty-activity-placeholder");
  if (placeholder) placeholder.remove();

  events.forEach((ev) => {
    appendLiveToolEvent(ev);
  });

  if (counter) counter.textContent = `${totalActivityCount} events`;
  container.scrollTop = container.scrollHeight;
}

/**
 * Update Findings tab with structured PM cards.
 */
function updateFindings(findingsList) {
  currentFindings = findingsList || [];
  renderFindings();
}

function addSingleFinding(finding) {
  if (!finding) return;
  const exists = currentFindings.some(f => f.problem === finding.problem && f.recommended_action === finding.recommended_action);
  if (!exists) {
    currentFindings.push(finding);
    renderFindings();
  }
}

function renderFindings() {
  const emptyState = document.getElementById("findings-empty-state");
  const container = document.getElementById("findings-container");
  if (!container) return;

  if (!currentFindings || currentFindings.length === 0) {
    if (emptyState) emptyState.style.display = "block";
    container.style.display = "none";
    container.innerHTML = "";
    return;
  }

  if (emptyState) emptyState.style.display = "none";
  container.style.display = "grid";
  container.innerHTML = "";

  currentFindings.forEach((item) => {
    const card = document.createElement("div");
    const prioClass = (item.priority || "P1").toLowerCase().includes("p0") ? "priority-p0" : "priority-p1";
    card.className = `finding-card ${prioClass}`;

    card.innerHTML = `
      <div class="finding-header">
        <h3 class="finding-title">${escapeHtml(item.problem || "Payment Decline Anomaly")}</h3>
        <span class="priority-badge">${escapeHtml(item.priority || "P1 - High")}</span>
      </div>

      <div class="finding-section">
        <div class="finding-section-label">Root Cause Classification</div>
        <span class="finding-cause-tag">${escapeHtml(item.cause_category || "Unknown")}</span>
        <p style="margin-top: 4px; color: var(--text-secondary);">${escapeHtml(item.likely_cause || "")}</p>
      </div>

      <div class="finding-section">
        <div class="finding-section-label">Evidence & Statistical Drivers</div>
        <p style="color: var(--text-secondary);">${escapeHtml(item.evidence || "Empirical deviation from baseline.")}</p>
      </div>

      <div class="finding-section">
        <div class="finding-section-label">Recommended PM Action</div>
        <p style="font-weight: 500; color: var(--text-primary);">${escapeHtml(item.recommended_action || "")}</p>
      </div>

      <div class="uplift-box">
        <span class="uplift-label">Estimated Approval Uplift</span>
        <span class="uplift-value">${escapeHtml(item.estimated_approval_rate_uplift || "+0.0% (Estimated)")}</span>
      </div>

      <div class="finding-footer">
        <span>Owner: <strong>${escapeHtml(item.suggested_owner || "Payments PM")}</strong></span>
        <span>Confidence: <strong>${escapeHtml(item.confidence || "High")}</strong></span>
      </div>
    `;

    container.appendChild(card);
  });
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatBytes(bytes) {
  if (bytes === 0) return "0 Bytes";
  const k = 1024;
  const sizes = ["Bytes", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
}

/**
 * Configure Zero-PII Shield simulation sandbox and live audit log controls.
 */
function initPrivacyShield() {
  const sampleTxnBtn = document.getElementById("privacy-sample-txn-btn");
  const sampleCardBtn = document.getElementById("privacy-sample-card-btn");
  const sampleDenyBtn = document.getElementById("privacy-sample-deny-btn");
  const testInput = document.getElementById("privacy-test-input");
  const runBtn = document.getElementById("privacy-test-run-btn");
  const refreshAuditBtn = document.getElementById("privacy-refresh-audit-btn");

  if (sampleTxnBtn && testInput) {
    sampleTxnBtn.addEventListener("click", () => {
      testInput.value = "Why was transaction bd3aa0d0-2cf7-46f0-a2b6-8a498a8eb49b declined by ICICI Bank?";
    });
  }

  if (sampleCardBtn && testInput) {
    sampleCardBtn.addEventListener("click", () => {
      testInput.value = "Can you check why customer john.doe@enterprise.com with card 4111 1111 1111 1111 had failure on txn bd3aa0d0-2cf7-46f0-a2b6-8a498a8eb49b?";
    });
  }

  if (sampleDenyBtn && testInput) {
    sampleDenyBtn.addEventListener("click", () => {
      testInput.value = "Run query: SELECT cvv, password, pin FROM transactions WHERE txn_id = 'bd3aa0d0-2cf7-46f0-a2b6-8a498a8eb49b'";
    });
  }

  if (runBtn && testInput) {
    runBtn.addEventListener("click", async () => {
      const textVal = testInput.value.trim();
      if (!textVal) return;

      runBtn.disabled = true;
      runBtn.innerHTML = "<span>Simulating Decoupling...</span>";

      try {
        const resp = await fetch("/api/privacy/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: textVal })
        });
        const data = await resp.json();

        if (resp.ok && data.success) {
          renderSimulationResults(data);
          fetchPrivacyData();
        } else {
          alert(`Privacy Simulation error: ${data.detail || "Failed to run simulation."}`);
        }
      } catch (err) {
        alert(`Error running privacy test: ${err.message}`);
      } finally {
        runBtn.disabled = false;
        runBtn.innerHTML = "<span>Run Decoupling Simulation</span>";
      }
    });
  }

  if (refreshAuditBtn) {
    refreshAuditBtn.addEventListener("click", () => {
      fetchPrivacyData();
    });
  }

  // Initial fetch of privacy statistics
  fetchPrivacyData();
}

function renderSimulationResults(data) {
  const container = document.getElementById("privacy-sim-results");
  const rawBox = document.getElementById("sim-raw-input");
  const sanitizedBox = document.getElementById("sim-sanitized-output");
  const rehydratedBox = document.getElementById("sim-rehydrated-output");
  const mappingTbody = document.getElementById("sim-mapping-tbody");

  if (!container || !rawBox || !sanitizedBox || !rehydratedBox) return;

  rawBox.textContent = data.original_text || "";
  sanitizedBox.textContent = data.sanitized_for_llm || "";
  rehydratedBox.textContent = data.rehydrated_for_user || "";

  if (mappingTbody && data.token_mapping) {
    mappingTbody.innerHTML = "";
    const entries = Object.entries(data.token_mapping);
    if (entries.length === 0) {
      mappingTbody.innerHTML = `<tr><td colspan="2" style="padding: 8px; color: var(--text-muted); text-align: center;">No PII detected in sample input.</td></tr>`;
    } else {
      entries.forEach(([tok, real]) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td style="padding: 6px 10px; font-weight: 700; color: #6d28d9;"><code>${escapeHtml(tok)}</code></td>
          <td style="padding: 6px 10px; color: var(--text-primary); font-family: monospace;">${escapeHtml(real)}</td>
        `;
        mappingTbody.appendChild(tr);
      });
    }
  }

  container.style.display = "block";
}

async function fetchPrivacyData() {
  try {
    const statusResp = await fetch("/api/privacy/status");
    if (statusResp.ok) {
      const s = await statusResp.json();
      const countEl = document.getElementById("privacy-total-scrubbed");
      if (countEl && s.total_global_scrub_events !== undefined) {
        countEl.textContent = s.total_global_scrub_events;
      }
      const denyEl = document.getElementById("privacy-deny-count");
      if (denyEl && s.forbidden_columns) {
        denyEl.textContent = `${s.forbidden_columns.length} Columns`;
      }
    }

    const auditResp = await fetch("/api/privacy/audit?limit=25");
    if (auditResp.ok) {
      const a = await auditResp.json();
      renderPrivacyAuditTable(a.audit_logs || []);
    }
  } catch (err) {
    console.debug("Failed to fetch privacy data:", err);
  }
}

function renderPrivacyAuditTable(logs) {
  const tbody = document.getElementById("privacy-audit-tbody");
  if (!tbody) return;

  tbody.innerHTML = "";

  if (!logs || logs.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="6" style="padding: 16px; text-align: center; color: var(--text-muted);">
          No PII entities scrubbed yet. Run an investigation or test the simulation sandbox above.
        </td>
      </tr>
    `;
    return;
  }

  // Render recent logs in reverse chronological order
  [...logs].reverse().forEach(log => {
    const tr = document.createElement("tr");
    const timeFormatted = log.timestamp ? log.timestamp.split("T")[1]?.slice(0, 8) : "";
    tr.innerHTML = `
      <td style="padding: 8px 10px; color: var(--text-muted);">${escapeHtml(timeFormatted)}</td>
      <td style="padding: 8px 10px; font-weight: 600;">${escapeHtml(log.entity_type || "")}</td>
      <td style="padding: 8px 10px; color: #7c3aed;"><code>${escapeHtml(log.surrogate_token || "")}</code></td>
      <td style="padding: 8px 10px; font-family: monospace;">${escapeHtml(log.masked_preview || "")}</td>
      <td style="padding: 8px 10px;"><span class="event-stage-tag tag-extracting">${escapeHtml(log.action || "SCRUBBED")}</span></td>
      <td style="padding: 8px 10px; color: var(--text-secondary);">${escapeHtml(log.source || "")}</td>
    `;
    tbody.appendChild(tr);
  });
}

/* =========================================================================
   Phase 5: Enterprise Integrations Dashboard Controller
   ========================================================================= */

let activitySearchDebounceTimer = null;

function initIntegrationsDashboard() {
  // 1. Refresh button
  const refreshBtn = document.getElementById("refresh-integrations-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      fetchIntegrationsOverview();
      fetchIntegrationActivities();
    });
  }

  // 2. Configure buttons on overview cards (Smooth scroll to form)
  const teamsConfigBtn = document.getElementById("teams-configure-btn");
  if (teamsConfigBtn) {
    teamsConfigBtn.addEventListener("click", () => {
      const pane = document.getElementById("pane-config-teams");
      if (pane) pane.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  const slackConfigBtn = document.getElementById("slack-configure-btn");
  if (slackConfigBtn) {
    slackConfigBtn.addEventListener("click", () => {
      const pane = document.getElementById("pane-config-slack");
      if (pane) pane.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  // 3. Disconnect buttons
  const teamsDisconnectBtn = document.getElementById("teams-disconnect-btn");
  if (teamsDisconnectBtn) {
    teamsDisconnectBtn.addEventListener("click", async () => {
      if (!confirm("Are you sure you want to disconnect Microsoft Teams? Felix will stop listening for Teams mentions.")) return;
      try {
        const resp = await fetch("/api/integrations/disconnect", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-User-Role": currentActiveRole,
          },
          body: JSON.stringify({ platform: "teams" }),
        });
        const data = await resp.json();
        if (resp.ok) {
          fetchIntegrationsOverview();
        } else {
          alert(`⚠️ ${data.detail || "Failed to disconnect Teams."}`);
        }
      } catch (err) {
        console.error("Failed to disconnect Teams:", err);
      }
    });
  }

  const slackDisconnectBtn = document.getElementById("slack-disconnect-btn");
  if (slackDisconnectBtn) {
    slackDisconnectBtn.addEventListener("click", async () => {
      if (!confirm("Are you sure you want to disconnect Slack? Felix will stop listening for Slack mentions.")) return;
      try {
        const resp = await fetch("/api/integrations/disconnect", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-User-Role": currentActiveRole,
          },
          body: JSON.stringify({ platform: "slack" }),
        });
        const data = await resp.json();
        if (resp.ok) {
          fetchIntegrationsOverview();
        } else {
          alert(`⚠️ ${data.detail || "Failed to disconnect Slack."}`);
        }
      } catch (err) {
        console.error("Failed to disconnect Slack:", err);
      }
    });
  }

  // 4. Form Submissions
  const teamsForm = document.getElementById("teams-config-form");
  if (teamsForm) {
    teamsForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await handleSaveTeamsConfig();
    });
  }

  const slackForm = document.getElementById("slack-config-form");
  if (slackForm) {
    slackForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await handleSaveSlackConfig();
    });
  }

  // 5. Test Connection Buttons
  const teamsTestBtn = document.getElementById("teams-test-conn-btn");
  if (teamsTestBtn) {
    teamsTestBtn.addEventListener("click", handleTestTeamsConnection);
  }

  const slackTestBtn = document.getElementById("slack-test-conn-btn");
  if (slackTestBtn) {
    slackTestBtn.addEventListener("click", handleTestSlackConnection);
  }

  // 6. Copy URL buttons
  document.querySelectorAll(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetId = btn.getAttribute("data-target");
      const input = document.getElementById(targetId);
      if (input) {
        navigator.clipboard.writeText(input.value).then(() => {
          const originalText = btn.textContent;
          btn.textContent = "Copied!";
          btn.classList.add("btn-success");
          setTimeout(() => {
            btn.textContent = originalText;
            btn.classList.remove("btn-success");
          }, 2000);
        });
      }
    });
  });

  // 7. Activity Filter Controls
  const filterPlatform = document.getElementById("activity-filter-platform");
  if (filterPlatform) {
    filterPlatform.addEventListener("change", () => fetchIntegrationActivities());
  }

  const filterStatus = document.getElementById("activity-filter-status");
  if (filterStatus) {
    filterStatus.addEventListener("change", () => fetchIntegrationActivities());
  }

  const searchInput = document.getElementById("activity-search-input");
  if (searchInput) {
    searchInput.addEventListener("input", () => {
      clearTimeout(activitySearchDebounceTimer);
      activitySearchDebounceTimer = setTimeout(() => {
        fetchIntegrationActivities();
      }, 300);
    });
  }

  const activityRefreshBtn = document.getElementById("activity-refresh-btn");
  if (activityRefreshBtn) {
    activityRefreshBtn.addEventListener("click", () => fetchIntegrationActivities());
  }

  // 8. Platform Listener Toggles
  const toggleSlack = document.getElementById("toggle-slack-enabled");
  if (toggleSlack) {
    toggleSlack.addEventListener("change", async () => {
      try {
        const resp = await fetch("/api/integrations/toggle", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-User-Role": currentActiveRole,
          },
          body: JSON.stringify({ platform: "slack", enabled: toggleSlack.checked }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          alert(`⚠️ ${data.detail || "Permission denied to toggle listener."}`);
          toggleSlack.checked = !toggleSlack.checked;
        } else {
          fetchIntegrationsOverview();
        }
      } catch (err) {
        console.error("Error toggling Slack:", err);
      }
    });
  }

  const toggleTeams = document.getElementById("toggle-teams-enabled");
  if (toggleTeams) {
    toggleTeams.addEventListener("change", async () => {
      try {
        const resp = await fetch("/api/integrations/toggle", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-User-Role": currentActiveRole,
          },
          body: JSON.stringify({ platform: "teams", enabled: toggleTeams.checked }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          alert(`⚠️ ${data.detail || "Permission denied to toggle listener."}`);
          toggleTeams.checked = !toggleTeams.checked;
        } else {
          fetchIntegrationsOverview();
        }
      } catch (err) {
        console.error("Error toggling Teams:", err);
      }
    });
  }
}

/**
 * Fetch overview data and update cards, forms, and system health status.
 */
async function fetchIntegrationsOverview() {
  try {
    const resp = await fetch("/api/integrations/overview");
    if (!resp.ok) return;
    const data = await resp.json();

    const origin = window.location.origin;

    // --- Microsoft Teams ---
    const teams = data.platforms?.teams || {};
    const teamsCardOrg = document.getElementById("teams-card-org");
    if (teamsCardOrg) teamsCardOrg.textContent = teams.organization_name || "Enterprise Microsoft 365 Tenant";

    const teamsCardLast = document.getElementById("teams-card-last-interaction");
    if (teamsCardLast) teamsCardLast.textContent = teams.last_successful_interaction || "Never";

    const teamsCardTotal = document.getElementById("teams-card-total-messages");
    if (teamsCardTotal) teamsCardTotal.textContent = (teams.total_messages || 0).toLocaleString();

    const teamsStatusPill = document.getElementById("teams-card-status");
    if (teamsStatusPill) {
      applyStatusPill(teamsStatusPill, teams.status, teams.status_label);
    }

    // Populate Teams Form
    const teamsAppIdInput = document.getElementById("teams-cfg-app-id");
    if (teamsAppIdInput && teams.app_id) teamsAppIdInput.value = teams.app_id;

    const teamsTenantIdInput = document.getElementById("teams-cfg-tenant-id");
    if (teamsTenantIdInput && teams.tenant_id) teamsTenantIdInput.value = teams.tenant_id;

    const teamsOrgInput = document.getElementById("teams-cfg-org-name");
    if (teamsOrgInput && teams.organization_name) teamsOrgInput.value = teams.organization_name;

    const teamsEndpointInput = document.getElementById("teams-cfg-endpoint");
    if (teamsEndpointInput) teamsEndpointInput.value = origin + (teams.messaging_endpoint || "/api/teams/messages");

    const teamsPwdInput = document.getElementById("teams-cfg-app-pwd");
    if (teamsPwdInput) {
      if (teams.has_app_password) {
        teamsPwdInput.placeholder = "•••••••••••• (Configured - leave blank to keep)";
      } else {
        teamsPwdInput.placeholder = "Enter Microsoft Client Secret";
      }
    }

    // --- Slack ---
    const slack = data.platforms?.slack || {};
    const slackCardWs = document.getElementById("slack-card-workspace");
    if (slackCardWs) slackCardWs.textContent = slack.workspace_name || "Enterprise Slack Workspace";

    const slackCardLast = document.getElementById("slack-card-last-interaction");
    if (slackCardLast) slackCardLast.textContent = slack.last_successful_interaction || "Never";

    const slackCardTotal = document.getElementById("slack-card-total-messages");
    if (slackCardTotal) slackCardTotal.textContent = (slack.total_messages || 0).toLocaleString();

    const slackStatusPill = document.getElementById("slack-card-status");
    if (slackStatusPill) {
      applyStatusPill(slackStatusPill, slack.status, slack.status_label);
    }

    // Populate Slack Form
    const slackAppIdInput = document.getElementById("slack-cfg-app-id");
    if (slackAppIdInput && slack.app_id) slackAppIdInput.value = slack.app_id;

    const slackWsIdInput = document.getElementById("slack-cfg-workspace-id");
    if (slackWsIdInput && slack.workspace_id) slackWsIdInput.value = slack.workspace_id;

    const slackWsNameInput = document.getElementById("slack-cfg-workspace-name");
    if (slackWsNameInput && slack.workspace_name) slackWsNameInput.value = slack.workspace_name;

    const slackBotHandleInput = document.getElementById("slack-cfg-bot-user-id");
    if (slackBotHandleInput && slack.bot_user_id) slackBotHandleInput.value = slack.bot_user_id;

    const slackWebhookInput = document.getElementById("slack-cfg-webhook");
    if (slackWebhookInput) slackWebhookInput.value = origin + (slack.event_subscription_url || "/api/slack/events");

    const slackTokenInput = document.getElementById("slack-cfg-bot-token");
    if (slackTokenInput) {
      if (slack.has_bot_token) {
        slackTokenInput.placeholder = "xoxb-•••••••••••• (Configured - leave blank to keep)";
      } else {
        slackTokenInput.placeholder = "xoxb-... (Enter Bot Token)";
      }
    }

    const slackSecretInput = document.getElementById("slack-cfg-signing-secret");
    if (slackSecretInput) {
      if (slack.has_signing_secret) {
        slackSecretInput.placeholder = "•••••••••••• (Configured - leave blank to keep)";
      } else {
        slackSecretInput.placeholder = "Enter Signing Secret";
      }
    }

    // --- Toggles & Health ---
    const toggleSlack = document.getElementById("toggle-slack-enabled");
    if (toggleSlack) toggleSlack.checked = !!slack.enabled;

    const toggleTeams = document.getElementById("toggle-teams-enabled");
    if (toggleTeams) toggleTeams.checked = !!teams.enabled;

    const healthVolume = document.getElementById("health-total-volume");
    if (healthVolume) healthVolume.textContent = (data.health?.total_messages_all || 0).toLocaleString();

  } catch (err) {
    console.debug("Error fetching integrations overview:", err);
  }
}

/**
 * Apply status pill styling and text dynamically.
 */
function applyStatusPill(element, status, label) {
  element.className = "integration-status-pill";
  if (status === "connected") {
    element.classList.add("status-connected");
  } else if (status === "disconnected") {
    element.classList.add("status-disconnected");
  } else if (status === "error") {
    element.classList.add("status-error");
  } else {
    element.classList.add("status-config-req");
  }
  element.innerHTML = `<span class="status-dot"></span> ${escapeHtml(label || "Configuration Required")}`;
}

/**
 * Save Microsoft Teams configuration to backend.
 */
async function handleSaveTeamsConfig() {
  const saveBtn = document.getElementById("teams-save-cfg-btn");
  const feedback = document.getElementById("teams-cfg-feedback");
  const appId = document.getElementById("teams-cfg-app-id")?.value?.trim();
  const tenantId = document.getElementById("teams-cfg-tenant-id")?.value?.trim();
  const appPwd = document.getElementById("teams-cfg-app-pwd")?.value?.trim();
  const orgName = document.getElementById("teams-cfg-org-name")?.value?.trim();

  if (saveBtn) saveBtn.textContent = "Saving...";

  try {
    const payload = {};
    if (appId !== undefined) payload.app_id = appId;
    if (tenantId !== undefined) payload.tenant_id = tenantId;
    if (appPwd) payload.app_password = appPwd;
    if (orgName !== undefined) payload.organization_name = orgName;

    const resp = await fetch("/api/integrations/teams/configure", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User-Role": currentActiveRole,
      },
      body: JSON.stringify(payload),
    });

    const data = await resp.json();
    if (feedback) {
      feedback.style.display = "block";
      if (resp.ok) {
        feedback.style.background = "#ecfdf5";
        feedback.style.color = "#065f46";
        feedback.style.border = "1px solid #a7f3d0";
        feedback.textContent = "✓ Teams configuration saved securely.";
        document.getElementById("teams-cfg-app-pwd").value = "";
      } else {
        feedback.style.background = "#fef2f2";
        feedback.style.color = "#991b1b";
        feedback.style.border = "1px solid #fecaca";
        feedback.textContent = `⚠️ ${data.detail || "Error saving configuration."}`;
      }
    }
    fetchIntegrationsOverview();
  } catch (err) {
    if (feedback) {
      feedback.style.display = "block";
      feedback.style.background = "#fef2f2";
      feedback.style.color = "#991b1b";
      feedback.textContent = `⚠️ Network error: ${err.message}`;
    }
  } finally {
    if (saveBtn) saveBtn.textContent = "Save Teams Configuration";
  }
}

/**
 * Save Slack configuration to backend.
 */
async function handleSaveSlackConfig() {
  const saveBtn = document.getElementById("slack-save-cfg-btn");
  const feedback = document.getElementById("slack-cfg-feedback");
  const appId = document.getElementById("slack-cfg-app-id")?.value?.trim();
  const wsId = document.getElementById("slack-cfg-workspace-id")?.value?.trim();
  const botToken = document.getElementById("slack-cfg-bot-token")?.value?.trim();
  const signingSecret = document.getElementById("slack-cfg-signing-secret")?.value?.trim();
  const wsName = document.getElementById("slack-cfg-workspace-name")?.value?.trim();
  const botHandle = document.getElementById("slack-cfg-bot-user-id")?.value?.trim();

  if (saveBtn) saveBtn.textContent = "Saving...";

  try {
    const payload = {};
    if (appId !== undefined) payload.app_id = appId;
    if (wsId !== undefined) payload.workspace_id = wsId;
    if (botToken) payload.bot_token = botToken;
    if (signingSecret) payload.signing_secret = signingSecret;
    if (wsName !== undefined) payload.workspace_name = wsName;
    if (botHandle !== undefined) payload.bot_user_id = botHandle;

    const resp = await fetch("/api/integrations/slack/configure", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User-Role": currentActiveRole,
      },
      body: JSON.stringify(payload),
    });

    const data = await resp.json();
    if (feedback) {
      feedback.style.display = "block";
      if (resp.ok) {
        feedback.style.background = "#ecfdf5";
        feedback.style.color = "#065f46";
        feedback.style.border = "1px solid #a7f3d0";
        feedback.textContent = "✓ Slack configuration saved securely.";
        document.getElementById("slack-cfg-bot-token").value = "";
        document.getElementById("slack-cfg-signing-secret").value = "";
      } else {
        feedback.style.background = "#fef2f2";
        feedback.style.color = "#991b1b";
        feedback.style.border = "1px solid #fecaca";
        feedback.textContent = `⚠️ ${data.detail || "Error saving configuration."}`;
      }
    }
    fetchIntegrationsOverview();
  } catch (err) {
    if (feedback) {
      feedback.style.display = "block";
      feedback.style.background = "#fef2f2";
      feedback.style.color = "#991b1b";
      feedback.textContent = `⚠️ Network error: ${err.message}`;
    }
  } finally {
    if (saveBtn) saveBtn.textContent = "Save Slack Configuration";
  }
}

/**
 * Test Microsoft Teams Connection.
 */
async function handleTestTeamsConnection() {
  const btn = document.getElementById("teams-test-conn-btn");
  const feedback = document.getElementById("teams-cfg-feedback");
  if (btn) btn.textContent = "Testing...";

  try {
    const resp = await fetch("/api/integrations/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform: "teams" }),
    });
    const result = await resp.json();
    if (feedback) {
      feedback.style.display = "block";
      if (result.success) {
        feedback.style.background = "#ecfdf5";
        feedback.style.color = "#065f46";
        feedback.style.border = "1px solid #a7f3d0";
        feedback.textContent = `✓ ${result.message}`;
      } else {
        feedback.style.background = "#fffbeb";
        feedback.style.color = "#92400e";
        feedback.style.border = "1px solid #fde68a";
        feedback.textContent = `⚠️ ${result.error || "Connection test failed."}`;
      }
    }
  } catch (err) {
    if (feedback) {
      feedback.style.display = "block";
      feedback.style.background = "#fef2f2";
      feedback.style.color = "#991b1b";
      feedback.textContent = `⚠️ Test request failed: ${err.message}`;
    }
  } finally {
    if (btn) btn.textContent = "Test Connection";
  }
}

/**
 * Test Slack Connection.
 */
async function handleTestSlackConnection() {
  const btn = document.getElementById("slack-test-conn-btn");
  const feedback = document.getElementById("slack-cfg-feedback");
  if (btn) btn.textContent = "Testing...";

  try {
    const resp = await fetch("/api/integrations/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform: "slack" }),
    });
    const result = await resp.json();
    if (feedback) {
      feedback.style.display = "block";
      if (result.success) {
        feedback.style.background = "#ecfdf5";
        feedback.style.color = "#065f46";
        feedback.style.border = "1px solid #a7f3d0";
        feedback.textContent = `✓ ${result.message}`;
      } else {
        feedback.style.background = "#fffbeb";
        feedback.style.color = "#92400e";
        feedback.style.border = "1px solid #fde68a";
        feedback.textContent = `⚠️ ${result.error || "Connection test failed."}`;
      }
    }
  } catch (err) {
    if (feedback) {
      feedback.style.display = "block";
      feedback.style.background = "#fef2f2";
      feedback.style.color = "#991b1b";
      feedback.textContent = `⚠️ Test request failed: ${err.message}`;
    }
  } finally {
    if (btn) btn.textContent = "Test Connection";
  }
}

/**
 * Query and render integration activities table with filters.
 */
async function fetchIntegrationActivities() {
  const tbody = document.getElementById("integrations-activity-tbody");
  if (!tbody) return;

  const platform = document.getElementById("activity-filter-platform")?.value || "all";
  const status = document.getElementById("activity-filter-status")?.value || "all";
  const search = document.getElementById("activity-search-input")?.value?.trim() || "";

  try {
    const params = new URLSearchParams({
      platform: platform,
      status: status,
      limit: "50",
      offset: "0",
    });
    if (search) params.set("search", search);

    const resp = await fetch(`/api/integrations/activity?${params.toString()}`);
    if (!resp.ok) return;

    const data = await resp.json();
    const activities = data.activities || [];

    tbody.innerHTML = "";

    if (activities.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="8" style="padding: 24px; text-align: center; color: var(--text-muted);">
            No integration activities recorded matching current filters. When users mention @Felix in Slack or Microsoft Teams, their inquiries and analytical tools will appear here in real-time.
          </td>
        </tr>
      `;
      return;
    }

    activities.forEach((act) => {
      const tr = document.createElement("tr");

      // Platform badge
      let platformBadge = `<span class="badge-web">Web</span>`;
      if (act.platform === "slack") {
        platformBadge = `<span class="badge-slack"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M6 10.5C6 9.67 6.67 9 7.5 9H9V10.5C9 11.33 8.33 12 7.5 12C6.67 12 6 11.33 6 10.5Z"/></svg> Slack</span>`;
      } else if (act.platform === "teams") {
        platformBadge = `<span class="badge-teams"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M14.5 9.5C14.5 8.12 15.62 7 17 7C18.38 7 19.5 8.12 19.5 9.5Z"/></svg> Teams</span>`;
      }

      // Status badge
      const isOk = act.execution_status === "completed";
      const statusBadge = isOk
        ? `<span class="status-badge badge-active">Completed</span>`
        : `<span class="status-badge badge-error">Error</span>`;

      // Tools called
      const tools = act.tools_called || [];
      const toolsSummary = tools.length > 0
        ? tools.map((t) => `<code style="font-size: 0.72rem; padding: 2px 4px; background: #f1f5f9; border-radius: 3px;">${escapeHtml(t)}</code>`).join(" ")
        : `<span style="color: var(--text-muted); font-size: 0.75rem;">direct</span>`;

      const findingsText = act.findings_count > 0
        ? `<span style="color: #16a34a; font-weight: 600; font-size: 0.75rem; margin-left: 4px;">(${act.findings_count} findings)</span>`
        : "";

      tr.innerHTML = `
        <td style="padding: 10px; color: var(--text-muted); white-space: nowrap;">${escapeHtml(act.ts || "")}</td>
        <td style="padding: 10px;">${platformBadge}</td>
        <td style="padding: 10px; font-weight: 600;">${escapeHtml(act.user_name || act.user_id || "User")}</td>
        <td style="padding: 10px; font-family: monospace; font-size: 0.76rem; color: var(--text-secondary);">${escapeHtml(act.channel ? act.channel.slice(0, 24) : "-")}</td>
        <td style="padding: 10px; max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(act.query || "")}">
          ${escapeHtml(act.query || "")}
        </td>
        <td style="padding: 10px;">${statusBadge}</td>
        <td style="padding: 10px; white-space: nowrap; color: var(--text-secondary);">${act.execution_duration_ms ? act.execution_duration_ms + "ms" : "-"}</td>
        <td style="padding: 10px;">${toolsSummary} ${findingsText}</td>
      `;

      tbody.appendChild(tr);
    });

  } catch (err) {
    console.debug("Error fetching integration activities:", err);
  }
}

/* =========================================================================
   Phase 6: Enterprise Security, Role-Based Access Control & Human Approval
   ========================================================================= */

function initRoleSwitcher() {
  const roleSelect = document.getElementById("user-role-selector");
  if (!roleSelect) return;

  roleSelect.addEventListener("change", () => {
    currentActiveRole = roleSelect.value;
    updateUIPermissions();
  });
  updateUIPermissions();
}

function updateUIPermissions() {
  const isAdm = currentActiveRole === "admin";
  const saveTeamsBtn = document.getElementById("teams-save-cfg-btn");
  const saveSlackBtn = document.getElementById("slack-save-cfg-btn");
  const teamsDisBtn = document.getElementById("teams-disconnect-btn");
  const slackDisBtn = document.getElementById("slack-disconnect-btn");

  if (!isAdm) {
    if (saveTeamsBtn) saveTeamsBtn.title = "Admin Role Required (Currently in Standard User mode)";
    if (saveSlackBtn) saveSlackBtn.title = "Admin Role Required (Currently in Standard User mode)";
    if (teamsDisBtn) teamsDisBtn.title = "Admin Role Required";
    if (slackDisBtn) slackDisBtn.title = "Admin Role Required";
  } else {
    if (saveTeamsBtn) saveTeamsBtn.title = "";
    if (saveSlackBtn) saveSlackBtn.title = "";
    if (teamsDisBtn) teamsDisBtn.title = "";
    if (slackDisBtn) slackDisBtn.title = "";
  }
}

function initActionApprovalFramework() {
  const refreshBtn = document.getElementById("refresh-proposals-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => fetchActionProposals());
  }
  fetchActionProposals();
}

async function fetchActionProposals() {
  const container = document.getElementById("action-proposals-list");
  if (!container) return;

  try {
    const resp = await fetch("/api/actions/proposals");
    if (!resp.ok) return;
    const data = await resp.json();
    const proposals = data.proposals || [];

    if (proposals.length === 0) {
      container.innerHTML = `
        <div style="padding: 16px; text-align: center; color: var(--text-muted); font-size: 0.85rem; border: 1px dashed var(--border-color); border-radius: var(--radius-sm);">
          No action proposals recorded yet. Run an investigation to generate actionable recommendations.
        </div>
      `;
      return;
    }

    container.innerHTML = "";
    proposals.forEach((prop) => {
      const card = document.createElement("div");
      card.className = "proposal-card";
      card.style.border = "1px solid var(--border-color)";
      card.style.borderRadius = "var(--radius-sm)";
      card.style.padding = "16px";
      card.style.background = "#ffffff";
      card.style.boxShadow = "var(--shadow-sm)";

      let statusBadge = "";
      if (prop.status === "PENDING_HUMAN_APPROVAL") {
        statusBadge = `<span class="integration-status-pill status-config-req"><span class="status-dot"></span> Pending Human Approval</span>`;
      } else if (prop.status === "APPROVED") {
        statusBadge = `<span class="integration-status-pill status-connected"><span class="status-dot"></span> Approved by Admin</span>`;
      } else {
        statusBadge = `<span class="integration-status-pill status-error"><span class="status-dot"></span> Rejected</span>`;
      }

      const isPending = prop.status === "PENDING_HUMAN_APPROVAL";

      card.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
          <div>
            <div style="font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase; font-weight: 700;">Action Proposal • ${escapeHtml(prop.proposal_id)}</div>
            <h4 style="font-size: 1rem; font-weight: 700; color: var(--text-primary); margin-top: 2px;">${escapeHtml(prop.title || "Remediation Action")}</h4>
          </div>
          <div>${statusBadge}</div>
        </div>

        <p style="font-size: 0.84rem; color: var(--text-secondary); margin-bottom: 10px;">
          <strong>Proposed Action:</strong> ${escapeHtml(prop.recommended_action || "")}
        </p>

        <div style="background: #f8fafc; border: 1px solid var(--border-color); border-radius: 4px; padding: 10px 12px; margin-bottom: 12px; font-size: 0.8rem; display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 8px;">
          <div><span style="color: var(--text-muted);">Expected Impact:</span> <strong style="color: #16a34a;">${escapeHtml(prop.expected_impact || "")}</strong></div>
          <div><span style="color: var(--text-muted);">Suggested Owner:</span> <strong>${escapeHtml(prop.suggested_owner || "Payments Engineering")}</strong></div>
          <div><span style="color: var(--text-muted);">Priority:</span> <strong>${escapeHtml(prop.priority || "P1")}</strong></div>
          <div><span style="color: var(--text-muted);">Created:</span> <strong>${escapeHtml(prop.created_at || "")}</strong></div>
        </div>

        <div style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 4px; padding: 8px 12px; margin-bottom: 12px; font-size: 0.75rem; color: #92400e; display: flex; align-items: center; gap: 8px;">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink: 0;"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>
          <span><strong>Production Execution Locked:</strong> Felix cannot directly modify infrastructure. Review and approval by an Organisation Administrator is required.</span>
        </div>

        ${isPending ? `
          <div style="display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px;">
            <button type="button" class="btn btn-sm btn-outline-danger btn-reject-proposal" data-id="${escapeHtml(prop.proposal_id)}">Reject Action</button>
            <button type="button" class="btn btn-sm btn-success btn-approve-proposal" data-id="${escapeHtml(prop.proposal_id)}" style="background: #16a34a; color: white; border: none; padding: 6px 14px; border-radius: 4px; font-weight: 600; cursor: pointer;">Approve Action</button>
          </div>
        ` : `
          <div style="font-size: 0.75rem; color: var(--text-secondary); text-align: right;">
            Reviewed by <strong>${escapeHtml(prop.reviewed_by || "Administrator")}</strong> at ${escapeHtml(prop.reviewed_at || "")}
          </div>
        `}
      `;

      container.appendChild(card);
    });

    // Attach approve/reject event listeners
    container.querySelectorAll(".btn-approve-proposal").forEach((btn) => {
      btn.addEventListener("click", () => reviewProposalAction(btn.getAttribute("data-id"), "approve"));
    });
    container.querySelectorAll(".btn-reject-proposal").forEach((btn) => {
      btn.addEventListener("click", () => reviewProposalAction(btn.getAttribute("data-id"), "reject"));
    });

  } catch (err) {
    console.debug("Error fetching action proposals:", err);
  }
}

async function reviewProposalAction(proposalId, decision) {
  try {
    const resp = await fetch(`/api/actions/proposals/${proposalId}/review`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User-Role": currentActiveRole,
        "X-User-Id": currentActiveRole === "admin" ? "admin" : "standard_user",
      },
      body: JSON.stringify({ decision: decision }),
    });

    const data = await resp.json();
    if (!resp.ok) {
      alert(`⚠️ ${data.detail || "Action review failed."}`);
      return;
    }

    fetchActionProposals();
  } catch (err) {
    alert(`⚠️ Error: ${err.message}`);
  }
}


