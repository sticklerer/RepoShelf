const state = { repos: [], filter: "all", search: "", selectedRepo: null, selected: new Set(), jobTimer: null };
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const allPackages = () => state.repos.flatMap((repo) => repo.packages);
function visiblePackages() {
  const selectedRepo = state.repos.find((repo) => repo.id === state.selectedRepo);
  let visible = selectedRepo ? selectedRepo.packages : allPackages();
  if (state.filter === "not-downloaded") visible = visible.filter((item) => !item.downloaded);
  if (state.filter === "downloaded") visible = visible.filter((item) => item.downloaded);
  if (state.search) {
    const search = state.search.toLowerCase();
    visible = visible.filter((item) => `${item.name} ${item.description} ${item.repo_name} ${item.version}`.toLowerCase().includes(search));
  }
  return visible;
}

function toast(message, isError = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", isError);
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 3400);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}

function sizeLabel(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size <= 0) return "—";
  if (size < 1024 * 1024) return `${Math.ceil(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function relativeDate(timestamp) {
  if (!timestamp) return "Not refreshed";
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - timestamp) / 60));
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
}

function render() {
  const packages = allPackages();
  $("#stat-packages").textContent = packages.length.toLocaleString();
  $("#stat-repos").textContent = state.repos.length;
  $("#stat-downloaded").textContent = packages.filter((item) => item.downloaded).length;
  $("#nav-package-count").textContent = packages.length;
  $("#stat-automation").textContent = state.automation?.enabled ? `${state.automation.interval_hours}h` : "Off";
  $("#download-location").textContent = state.download_dir ? `Saved to ${state.download_dir}` : "";
  $("#repo-list").innerHTML = state.repos.map((repo) => `
    <div class="repo-line">
      <button class="repo-item ${state.selectedRepo === repo.id ? "selected" : ""}" data-repo="${esc(repo.id)}" title="${esc(repo.url)}">
        <span class="repo-icon">⌂</span><span class="repo-name">${esc(repo.name)}</span><span class="repo-item-count">${repo.packages.length}</span>
      </button>
      <button class="repo-remove" data-remove="${esc(repo.id)}" title="Remove source" aria-label="Remove ${esc(repo.name)}">×</button>
    </div>`).join("");

  const selectedRepo = state.repos.find((repo) => repo.id === state.selectedRepo);
  const visible = visiblePackages();
  visible.sort((a, b) => a.name.localeCompare(b.name));
  $("#current-location").textContent = selectedRepo ? selectedRepo.name : "All packages";
  $("#section-title").childNodes[0].textContent = selectedRepo ? selectedRepo.name : "All packages ";
  $("#section-count").textContent = visible.length;
  $("#visible-count").textContent = `Showing ${visible.length} package${visible.length === 1 ? "" : "s"}`;
  $("#package-rows").innerHTML = visible.map((item) => `
    <tr>
      <td><input class="row-check" type="checkbox" data-id="${item.id}" ${state.selected.has(item.id) ? "checked" : ""} aria-label="Select ${esc(item.name)}"></td>
      <td><div class="package-name-cell"><span class="package-icon">◇</span><span><span class="package-title">${esc(item.name)}</span><span class="package-description">${esc(item.description || "No description provided")}</span></span></div></td>
      <td><span class="source-cell"><span class="source-mini">⌂</span>${esc(item.repo_name)}</span></td>
      <td class="version-cell">${esc(item.version || "—")}</td>
      <td class="size-cell">${sizeLabel(item.size)}</td>
      <td><span class="status-pill ${item.downloaded ? "" : "pending"}"><span>${item.downloaded ? "✓" : "·"}</span>${item.downloaded ? "Saved" : "Available"}</span></td>
      <td>${item.downloaded ? "" : `<button class="row-download" data-download="${item.id}">↓ Get</button>`}</td>
    </tr>`).join("");
  $("#empty-state").style.display = visible.length ? "none" : "flex";
  $("#package-rows").closest("table").style.display = visible.length ? "table" : "none";
  $("#empty-state").querySelector("strong").textContent = packages.length
    ? (state.search ? "No matching packages" : "No packages match this filter")
    : "No packages here yet";
  $("#empty-state").querySelector("p").textContent = packages.length
    ? "Try a different search or filter, or refresh your repository sources."
    : "Add a jailbreak repository to browse its packages and start your local library.";
  $("#empty-add-button").style.display = packages.length ? "none" : "inline-flex";
  updateSelectionControls();
  $("#get-all-button").disabled = visible.every((item) => item.downloaded);
}

function updateSelectionControls() {
  const packages = allPackages();
  const packageIds = new Set(packages.map((item) => item.id));
  for (const id of state.selected) {
    if (!packageIds.has(id)) state.selected.delete(id);
  }
  const selectedPending = packages.filter((item) => state.selected.has(item.id) && !item.downloaded);
  $("#get-selected-button").hidden = state.selected.size === 0;
  $("#get-selected-button").disabled = selectedPending.length === 0;
  const visibleIds = [...document.querySelectorAll(".row-check")].map((checkbox) => checkbox.dataset.id);
  const selectedVisible = visibleIds.filter((id) => state.selected.has(id)).length;
  $("#select-visible").checked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
  $("#select-visible").indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.length;
}

async function loadState() {
  const result = await api("/api/state");
  state.repos = result.repos;
  state.automation = result.automation;
  state.download_dir = result.download_dir;
  if (state.selectedRepo && !state.repos.some((repo) => repo.id === state.selectedRepo)) state.selectedRepo = null;
  render();
}

function openDialog(dialog) {
  dialog.showModal();
}

async function startJob(ids, title) {
  if (!ids.length) return toast("No packages to download.");
  const result = await api("/api/downloads", { method: "POST", body: JSON.stringify({ package_ids: ids }) });
  watchJob(result.job_id, title);
}

function watchJob(jobId, title) {
  const banner = $("#job-banner");
  banner.hidden = false;
  $("#job-title").textContent = title;
  $("#job-status").textContent = "Starting…";
  $("#job-progress-fill").style.width = "0%";
  clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      const percent = job.total ? Math.round(job.completed / job.total * 100) : 100;
      $("#job-progress-fill").style.width = `${percent}%`;
      $("#job-status").textContent = job.total ? `${job.completed} of ${job.total} packages` : "All packages are already saved";
      if (job.status !== "running") {
        clearInterval(state.jobTimer);
        await loadState();
        if (job.errors.length) toast(`${job.errors.length} package${job.errors.length === 1 ? "" : "s"} could not be downloaded. See the job panel details.`, true);
        else toast(job.total ? "Downloads finished." : "Your library is already up to date.");
        $("#job-title").textContent = job.errors.length ? "Download finished with errors" : "Downloads complete";
        $("#job-status").textContent = job.errors.length ? job.errors.join(" · ") : "Your local library is up to date.";
        $(".job-spinner").style.animationPlayState = "paused";
        setTimeout(() => { banner.hidden = true; $(".job-spinner").style.animationPlayState = ""; }, 6000);
      }
    } catch (error) {
      clearInterval(state.jobTimer);
      banner.hidden = true;
      toast(error.message, true);
    }
  }, 700);
}

function showSourceDialog() {
  state.deviceIdRequest = (state.deviceIdRequest || 0) + 1;
  const requestId = state.deviceIdRequest;
  $("#source-error").textContent = "";
  $("#repo-url").value = "";
  $("#repo-auth-method").value = "none";
  $("#repo-username").value = "";
  $("#repo-password").value = "";
  $("#repo-access-token").value = "";
  $("#device-profile-enabled").checked = true;
  $("#device-model").value = "iPhone15,2";
  $("#device-os-version").value = "17.0";
  $("#device-architecture").value = "iphoneos-arm64";
  $("#device-client-version").value = "2.4.4";
  $("#device-id-value").value = "";
  $("#device-id-value").readOnly = true;
  $("#manual-device-id").checked = false;
  $("#manual-device-id").disabled = false;
  updateAuthFields();
  updatePaidRepositoryMode();
  updateDeviceProfileFields();
  openDialog($("#source-dialog"));
  if (!$("#manual-device-id").checked) {
    api("/api/device-profile").then((result) => {
      if ($("#source-dialog").open && requestId === state.deviceIdRequest && !$("#manual-device-id").checked) {
        $("#device-id-value").value = result.generated_device_id;
      }
    }).catch((error) => {
      if ($("#source-dialog").open && requestId === state.deviceIdRequest) {
        $("#device-id-value").value = "";
        $("#device-id-value").placeholder = error.message;
      }
    });
  }
  $("#repo-url").focus();
}

function updateAuthFields() {
  const method = $("#repo-auth-method").value;
  $("#repo-basic-fields").hidden = method !== "basic";
  $("#repo-token-fields").hidden = method !== "bearer";
  $("#repo-username").required = method === "basic";
  $("#repo-password").required = method === "basic";
  $("#repo-access-token").required = method === "bearer";
}

function updateDeviceProfileFields() {
  const enabled = $("#device-profile-enabled").checked;
  const paid = requiresManualDeviceId($("#repo-url").value);
  $("#device-profile-fields").hidden = !enabled;
  $("#device-model").required = enabled;
  $("#device-os-version").required = enabled;
  $("#device-client-version").required = enabled;
  $("#device-profile-enabled").disabled = paid;
  $("#manual-device-id").disabled = paid;
  $("#device-id-value").readOnly = !$("#manual-device-id").checked;
  $("#device-id-value").required = enabled && $("#manual-device-id").checked;
  $("#paid-repo-notice").hidden = !requiresManualDeviceId($("#repo-url").value);
  $("#device-id-hint").textContent = $("#manual-device-id").checked
    ? "Enter the device ID already authorized with this paid repository. RepoShelf stores it in your OS keyring."
    : "A random 40-character archive ID is generated once and stored in your OS keyring. It is not read from or tied to a physical phone.";
}

function requiresManualDeviceId(url) {
  try {
    const host = new URL(url).hostname.toLowerCase().replace(/\.$/, "");
    return ["havoc.app", "chariz.com", "yourepo.com"].some(
      (domain) => host === domain || host.endsWith(`.${domain}`),
    );
  } catch {
    return false;
  }
}

function updatePaidRepositoryMode() {
  if (requiresManualDeviceId($("#repo-url").value)) {
    if (!$("#manual-device-id").checked) $("#device-id-value").value = "";
    $("#manual-device-id").checked = true;
    $("#device-profile-enabled").checked = true;
    $("#manual-device-id").disabled = true;
    $("#device-profile-enabled").disabled = true;
  } else {
    $("#manual-device-id").disabled = false;
    $("#device-profile-enabled").disabled = false;
  }
  updateDeviceProfileFields();
}

$("#source-dialog").addEventListener("close", () => {
  state.deviceIdRequest = (state.deviceIdRequest || 0) + 1;
  $("#repo-username").value = "";
  $("#repo-password").value = "";
  $("#repo-access-token").value = "";
  $("#device-id-value").value = "";
});

$("#repo-auth-method").addEventListener("change", updateAuthFields);
$("#device-profile-enabled").addEventListener("change", () => {
  if ($("#device-profile-enabled").checked) updatePaidRepositoryMode();
  else updateDeviceProfileFields();
});
$("#manual-device-id").addEventListener("change", () => {
  state.deviceIdRequest = (state.deviceIdRequest || 0) + 1;
  const requestId = state.deviceIdRequest;
  if ($("#manual-device-id").checked) $("#device-id-value").value = "";
  else if (!requiresManualDeviceId($("#repo-url").value)) {
    api("/api/device-profile").then((result) => {
      if ($("#source-dialog").open && requestId === state.deviceIdRequest && !$("#manual-device-id").checked) {
        $("#device-id-value").value = result.generated_device_id;
      }
    })
      .catch((error) => toast(error.message, true));
  }
  updateDeviceProfileFields();
});
$("#repo-url").addEventListener("input", updatePaidRepositoryMode);
$("#add-source-button").addEventListener("click", showSourceDialog);
$("#sidebar-add").addEventListener("click", showSourceDialog);
$("#empty-add-button").addEventListener("click", showSourceDialog);
$("#help-button").addEventListener("click", () => openDialog($("#help-dialog")));
$("#automation-button").addEventListener("click", () => {
  $("#automation-enabled").checked = Boolean(state.automation?.enabled);
  $("#automation-interval").value = String(state.automation?.interval_hours || 24);
  $("#automation-error").textContent = "";
  openDialog($("#automation-dialog"));
});
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));

$("#source-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#source-submit");
  button.disabled = true;
  button.textContent = "Checking source…";
  $("#source-error").textContent = "";
  try {
    const method = $("#repo-auth-method").value;
    const auth = { method };
    if (method === "basic") {
      auth.username = $("#repo-username").value;
      auth.secret = $("#repo-password").value;
    } else if (method === "bearer") {
      auth.secret = $("#repo-access-token").value;
    }
    const deviceProfile = {
      enabled: $("#device-profile-enabled").checked,
      client: "sileo",
      manual_device_id: $("#manual-device-id").checked,
    };
    if (deviceProfile.enabled) {
      deviceProfile.model = $("#device-model").value.trim();
      deviceProfile.os_version = $("#device-os-version").value.trim();
      deviceProfile.architecture = $("#device-architecture").value;
      deviceProfile.client_version = $("#device-client-version").value.trim();
      if (deviceProfile.manual_device_id) {
        deviceProfile.device_id = $("#device-id-value").value.trim();
      }
    }
    const result = await api("/api/repos", {
      method: "POST",
      body: JSON.stringify({
        url: $("#repo-url").value.trim(),
        auth,
        device_profile: deviceProfile,
      }),
    });
    state.repos = result.state.repos;
    state.selectedRepo = null;
    render();
    $("#source-dialog").close();
    $("#repo-url").value = "";
    $("#repo-username").value = "";
    $("#repo-password").value = "";
    $("#repo-access-token").value = "";
    $("#device-profile-enabled").checked = false;
    updateDeviceProfileFields();
    toast(`Added source with ${result.package_count} packages.`);
  } catch (error) {
    $("#source-error").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Add source";
  }
});

$("#automation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#automation-error").textContent = "";
  try {
    const result = await api("/api/automation", {
      method: "POST",
      body: JSON.stringify({
        enabled: $("#automation-enabled").checked,
        interval_hours: Number($("#automation-interval").value),
      }),
    });
    state.automation = result.automation;
    render();
    $("#automation-dialog").close();
    toast(result.automation.enabled ? `Automation enabled. Sources will be checked every ${result.automation.interval_hours} hour(s).` : "Automation turned off.");
  } catch (error) {
    $("#automation-error").textContent = error.message;
  }
});

$("#run-now-button").addEventListener("click", async () => {
  $("#automation-error").textContent = "";
  try {
    const result = await api("/api/automation/run", { method: "POST", body: "{}" });
    $("#automation-dialog").close();
    if (result.errors.length) toast(result.errors.join(" · "), true);
    watchJob(result.job_id, "Checking sources and downloading updates");
  } catch (error) {
    $("#automation-error").textContent = error.message;
  }
});

$("#refresh-button").addEventListener("click", async () => {
  if (!state.repos.length) return toast("Add a repository source first.");
  const button = $("#refresh-button");
  button.disabled = true;
  button.querySelector("span").textContent = "Refreshing…";
  const errors = [];
  try {
    for (const repo of state.repos) {
      try { await api(`/api/repos/${repo.id}/refresh`, { method: "POST", body: "{}" }); }
      catch (error) { errors.push(`${repo.name}: ${error.message}`); }
    }
    await loadState();
    toast(errors.length ? errors.join(" · ") : "Sources refreshed.", errors.length > 0);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "Refresh";
  }
});

$("#get-all-button").addEventListener("click", () => {
  const packages = visiblePackages();
  const pending = packages.filter((item) => !item.downloaded);
  startJob(pending.map((item) => item.id), `Downloading ${pending.length} packages`);
});
$("#get-selected-button").addEventListener("click", () => {
  const ids = allPackages().filter((item) => state.selected.has(item.id) && !item.downloaded).map((item) => item.id);
  startJob(ids, `Downloading ${ids.length} selected packages`);
});

$("#search-input").addEventListener("input", (event) => { state.search = event.target.value.trim(); render(); });
$("#all-packages-nav").addEventListener("click", () => { state.selectedRepo = null; render(); });
document.querySelectorAll(".filter-tab").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".filter-tab").forEach((tab) => tab.classList.toggle("selected", tab === button));
  state.filter = button.dataset.filter;
  render();
}));

$("#repo-list").addEventListener("click", async (event) => {
  const removeButton = event.target.closest("[data-remove]");
  if (removeButton) {
    const repo = state.repos.find((item) => item.id === removeButton.dataset.remove);
    if (!repo || !confirm(`Remove ${repo.name} from RepoShelf? Downloaded files will stay on disk.`)) return;
    try {
      const result = await api(`/api/repos/${repo.id}`, { method: "DELETE" });
      state.repos = result.state.repos;
      state.selectedRepo = null;
      render();
      toast("Source removed. Downloaded files were kept.");
    } catch (error) { toast(error.message, true); }
    return;
  }
  const button = event.target.closest("[data-repo]");
  if (!button) return;
  const repo = state.repos.find((item) => item.id === button.dataset.repo);
  if (!repo) return;
  state.selectedRepo = state.selectedRepo === repo.id ? null : repo.id;
  render();
});

$("#package-rows").addEventListener("change", (event) => {
  if (!event.target.matches(".row-check")) return;
  if (event.target.checked) state.selected.add(event.target.dataset.id);
  else state.selected.delete(event.target.dataset.id);
  updateSelectionControls();
});
$("#package-rows").addEventListener("click", (event) => {
  const button = event.target.closest("[data-download]");
  if (button) startJob([button.dataset.download], "Downloading package");
});
$("#select-visible").addEventListener("change", (event) => {
  const ids = [...document.querySelectorAll(".row-check")].map((checkbox) => checkbox.dataset.id);
  ids.forEach((id) => event.target.checked ? state.selected.add(id) : state.selected.delete(id));
  render();
});

loadState().catch((error) => toast(`Couldn't load RepoShelf: ${error.message}`, true));
setInterval(() => loadState().catch((error) => toast(`Couldn't refresh the library: ${error.message}`, true)), 30000);
