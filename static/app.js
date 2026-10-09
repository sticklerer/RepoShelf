const PAGE_SIZE = 100;
const FAILURE_PAGE_SIZE = 50;
const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "paused", "cancelling"]);
const MAX_AUTOMATION_SECONDS = 99 * 365 * 24 * 60 * 60;
const DURATION_UNITS = [
  { aliases: ["year", "years", "yr", "yrs", "y"], seconds: 365 * 24 * 60 * 60 },
  { aliases: ["day", "days", "d"], seconds: 24 * 60 * 60 },
  { aliases: ["hour", "hours", "hr", "hrs", "h"], seconds: 60 * 60 },
  { aliases: ["minute", "minutes", "min", "mins", "m"], seconds: 60 },
  { aliases: ["second", "seconds", "sec", "secs", "s"], seconds: 1 },
];
const state = { repos: [], groups: [], filter: "all", search: "", selectedRepo: null, selectedGroupIds: [], selectedQueueGroupIds: [], pendingRepoRemoval: null, selected: new Set(), page: 0, currentView: "library", settings: { logging_enabled: false, show_automation_banner: true, grouping_enabled: true }, generatedDeviceId: "", deleteAppData: false, jobTimer: null, queueTimer: null, queueRefreshPromise: null, queueLoaded: false, activeJobId: null, jobs: [], queuePages: {}, failurePages: {}, failureCache: {}, iconCache: new Map(), iconLoads: new Map(), iconObserver: null, jobMinimized: false, editingGroupId: null };
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const allPackages = () => state.repos.flatMap((repo) => repo.packages);
function parseDuration(value) {
  const input = value.trim().toLowerCase();
  if (!input) throw new Error("Enter a check interval.");
  let position = 0;
  let total = 0;
  let terms = 0;
  while (position < input.length) {
    while (/[,\s]/.test(input[position] || "")) position += 1;
    if (position >= input.length) break;
    const match = input.slice(position).match(/^(\d+(?:\.\d+)?)\s*(years?|yrs?|y|days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)/i);
    if (!match) throw new Error("Use a duration such as “220 seconds” or “3m 40s”.");
    const unit = DURATION_UNITS.find((item) => item.aliases.includes(match[2]));
    total += Number(match[1]) * unit.seconds;
    terms += 1;
    position += match[0].length;
  }
  const seconds = Math.round(total);
  if (!terms || seconds < 1) throw new Error("The interval must be at least 1 second.");
  if (seconds > MAX_AUTOMATION_SECONDS) throw new Error("The maximum interval is 99 years.");
  return seconds;
}
function formatDuration(value) {
  let seconds = Math.max(0, Math.floor(Number(value) || 0));
  const parts = [];
  for (const unit of DURATION_UNITS) {
    const count = Math.floor(seconds / unit.seconds);
    if (count) {
      const name = unit.aliases[0];
      parts.push(`${count} ${name}${count === 1 ? "" : "s"}`);
      seconds %= unit.seconds;
    }
  }
  return parts.join(" ") || "0 seconds";
}
function visiblePackages() {
  const selectedRepo = state.repos.find((repo) => repo.id === state.selectedRepo);
  let visible = selectedRepo ? selectedRepo.packages : allPackages();
  if (!selectedRepo && state.settings.grouping_enabled && state.selectedGroupIds.length) {
    const selectedRepos = new Set(state.repos
      .filter((repo) => groupAncestors(repo.group_id).some((id) => state.selectedGroupIds.includes(id)))
      .map((repo) => repo.id));
    visible = visible.filter((item) => selectedRepos.has(item.repo_id));
  }
  if (state.filter === "not-downloaded") visible = visible.filter((item) => !item.downloaded);
  if (state.filter === "downloaded") visible = visible.filter((item) => item.downloaded);
  if (state.search) {
    const search = state.search.toLowerCase();
    visible = visible.filter((item) => `${item.name} ${item.bundle_id || ""} ${item.description} ${item.repo_name} ${item.version}`.toLowerCase().includes(search));
  }
  return visible;
}

function groupAncestors(groupId) {
  const ancestors = [];
  const visited = new Set();
  while (groupId && !visited.has(groupId)) {
    visited.add(groupId);
    ancestors.push(groupId);
    groupId = state.groups.find((group) => group.id === groupId)?.parent_id;
  }
  return ancestors;
}

function groupOptions(excludedId = "") {
  const descendants = new Set();
  const addChildren = (id) => {
    descendants.add(id);
    state.groups.filter((group) => group.parent_id === id).forEach((group) => addChildren(group.id));
  };
  if (excludedId) addChildren(excludedId);
  return `<option value="">No group</option>${state.groups
    .filter((group) => !descendants.has(group.id))
    .map((group) => `<option value="${esc(group.id)}">${esc(groupAncestors(group.id).reverse().map((id) => state.groups.find((item) => item.id === id)?.name || "").filter(Boolean).join(" / "))}</option>`)
    .join("")}`;
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

function byteLabel(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "0 B";
  if (bytes < 1024) return `${Math.floor(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function activateIcons(container, selector) {
  if (!state.iconObserver && "IntersectionObserver" in window) {
    state.iconObserver = new IntersectionObserver((entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        loadIcon(entry.target);
      });
    }, { rootMargin: "180px" });
  }
  container.querySelectorAll(selector).forEach((image) => {
    const source = image.dataset.src;
    image.addEventListener("load", () => image.parentElement.classList.add("has-image"), { once: true });
    image.addEventListener("error", () => image.remove(), { once: true });
    if (state.iconCache.has(source)) {
      image.src = state.iconCache.get(source);
    } else if (state.iconObserver) {
      state.iconObserver.observe(image);
    } else {
      loadIcon(image);
    }
  });
}

async function loadIcon(image) {
  if (!image.isConnected) return;
  const source = image.dataset.src;
  try {
    let objectUrl = state.iconCache.get(source);
    if (!objectUrl) {
      let request = state.iconLoads.get(source);
      if (!request) {
        request = fetch(source, { credentials: "same-origin" }).then((response) => {
          if (!response.ok) throw new Error(`Icon request failed (${response.status})`);
          return response.blob();
        }).then((blob) => URL.createObjectURL(blob)).finally(() => state.iconLoads.delete(source));
        state.iconLoads.set(source, request);
      }
      objectUrl = await request;
      state.iconCache.set(source, objectUrl);
      if (state.iconCache.size > 300) {
        const oldest = state.iconCache.keys().next().value;
        URL.revokeObjectURL(state.iconCache.get(oldest));
        state.iconCache.delete(oldest);
      }
    }
    if (image.isConnected) image.src = objectUrl;
  } catch {
    image.remove();
  }
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
  const settingsVisible = state.currentView === "settings";
  const queueVisible = state.currentView === "queue";
  const automationVisible = state.currentView === "automation";
  const libraryVisible = !settingsVisible && !queueVisible && !automationVisible;
  document.querySelectorAll("[data-library-view]").forEach((element) => { element.hidden = !libraryVisible; });
  $("#settings-view").hidden = !settingsVisible;
  $("#queue-view").hidden = !queueVisible;
  $("#automation-view").hidden = !automationVisible;
  $("#settings-nav").classList.toggle("active", settingsVisible);
  $("#queue-nav").classList.toggle("active", queueVisible);
  $("#automation-nav").classList.toggle("active", automationVisible);
  $("#all-packages-nav").classList.toggle("active", libraryVisible);
  $("#automation-banner").hidden = !libraryVisible || Boolean(state.automation?.enabled) || state.settings.show_automation_banner === false;
  $("#collection-kicker").hidden = Boolean(state.automation?.enabled);
  if (settingsVisible) {
    $("#current-location").textContent = "Settings";
    $("#show-automation-banner").checked = state.settings.show_automation_banner !== false;
    $("#keep-download-status").checked = Boolean(state.settings.keep_download_status_until_done);
    $("#show-device-info-on-add").checked = state.settings.show_device_info_on_add !== false;
    $("#grouping-enabled").checked = state.settings.grouping_enabled !== false;
  }
  else if (queueVisible) {
    $("#current-location").textContent = "Download queue";
    renderQueue();
  }
  else if (automationVisible) {
    $("#current-location").textContent = "Automation";
    $("#automation-enabled").checked = Boolean(state.automation?.enabled);
    $("#automation-interval").value = formatDuration(state.automation?.interval_seconds || 24 * 60 * 60);
    renderAutomationRepoIntervals();
  } else {
  const packages = allPackages();
  $("#stat-packages").textContent = packages.length.toLocaleString();
  $("#stat-repos").textContent = state.repos.length;
  $("#stat-downloaded").textContent = packages.filter((item) => item.downloaded).length;
  $("#nav-package-count").textContent = packages.length;
  $("#stat-automation").textContent = state.automation?.enabled
    ? (Object.keys(state.automation.per_repo_intervals_seconds || {}).length
      || Object.keys(state.automation.per_group_intervals_seconds || {}).length
      ? "Custom"
      : formatDuration(state.automation.interval_seconds || 24 * 60 * 60))
    : "Off";
  $("#download-location").textContent = state.download_dir ? `Saved to ${state.download_dir}` : "";
  $("#repo-list").innerHTML = state.settings.grouping_enabled
    ? renderSourceTree(null)
    : state.repos.map((repo) => renderRepoLine(repo)).join("");
  $("#group-manager-button").hidden = state.settings.grouping_enabled === false;
  renderGroupManager();
  renderQueueGroupFilter();

  const selectedRepo = state.repos.find((repo) => repo.id === state.selectedRepo);
  const visible = visiblePackages();
  visible.sort((a, b) => a.name.localeCompare(b.name));
  const pageCount = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  state.page = Math.min(state.page, pageCount - 1);
  const pageStart = state.page * PAGE_SIZE;
  const pagePackages = visible.slice(pageStart, pageStart + PAGE_SIZE);
  const selectedGroups = state.selectedGroupIds.map((id) => state.groups.find((group) => group.id === id)?.name).filter(Boolean);
  const locationName = selectedRepo?.name || (selectedGroups.length ? selectedGroups.join(", ") : "All packages");
  $("#current-location").textContent = locationName;
  $("#section-title").childNodes[0].textContent = `${locationName}${selectedRepo || selectedGroups.length ? " " : " "}`;
  $("#section-count").textContent = visible.length;
  $("#visible-count").textContent = visible.length
    ? `Showing ${pageStart + 1}–${Math.min(pageStart + PAGE_SIZE, visible.length)} of ${visible.length} packages`
    : "Showing 0 packages";
  $("#page-count").textContent = `Page ${state.page + 1} of ${pageCount}`;
  $("#previous-page").disabled = state.page === 0;
  $("#next-page").disabled = state.page >= pageCount - 1;
  $("#package-rows").innerHTML = pagePackages.map((item) => `
    <tr>
      <td><input class="row-check" type="checkbox" data-id="${item.id}" ${state.selected.has(item.id) ? "checked" : ""} aria-label="Select ${esc(item.name)}"></td>
      <td><div class="package-name-cell"><span class="package-icon"><span class="package-icon-fallback">◇</span><img class="package-icon-image" data-src="/api/packages/${esc(item.id)}/icon" alt=""></span><span class="package-copy"><span class="package-title">${esc(item.name)}</span><span class="package-bundle-id">${esc(item.bundle_id || item.name)}</span><span class="package-description">${esc(item.description || "No description provided")}</span></span></div></td>
      <td><span class="source-cell"><span class="source-mini"><span class="source-mini-fallback">⌂</span><img class="source-mini-image" data-src="/api/repos/${esc(item.repo_id)}/icon" alt=""></span>${esc(item.repo_name)}</span></td>
      <td class="version-cell">${esc(item.version || "—")}</td>
      <td class="size-cell">${sizeLabel(item.size)}</td>
      <td><span class="status-pill ${item.downloaded ? "" : "pending"}"><span>${item.downloaded ? "✓" : "·"}</span>${item.downloaded ? "Saved" : "Available"}</span></td>
      <td>${item.downloaded ? "" : `<button class="row-download" data-download="${item.id}">↓ Get</button>`}</td>
    </tr>`).join("");
  activateIcons($("#repo-list"), ".repo-icon-image");
  activateIcons($("#package-rows"), ".package-icon-image, .source-mini-image");
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
  $("#logging-enabled").checked = Boolean(state.settings.logging_enabled);
  $("#settings-log-path").textContent = `${state.download_dir.replace(/\/downloads\/?$/, "")}/reposhelf.log`;
  $("#generated-device-id").textContent = state.generatedDeviceId || "Not loaded";
}

function renderRepoLine(repo, depth = 0) {
  return `<div class="repo-line" style="--tree-depth:${depth}">
    <button class="repo-item ${state.selectedRepo === repo.id ? "selected" : ""}" data-repo="${esc(repo.id)}" title="${esc(repo.url)}">
      <span class="repo-icon"><span class="repo-icon-fallback">⌂</span><img class="repo-icon-image" data-src="/api/repos/${esc(repo.id)}/icon" alt=""></span><span class="repo-name">${esc(repo.name)}</span><span class="repo-item-count">${repo.packages.length}</span>
    </button>
    <button class="repo-remove" data-remove="${esc(repo.id)}" title="Remove source" aria-label="Remove ${esc(repo.name)}">×</button>
  </div>`;
}

function renderSourceTree(parentId, depth = 0) {
  const childGroups = state.groups.filter((group) => (group.parent_id || null) === parentId);
  const groupedRepos = state.repos.filter((repo) => (repo.group_id || null) === parentId);
  return childGroups.map((group) => {
    const groupIds = new Set([group.id, ...state.groups.filter((candidate) => groupAncestors(candidate.id).includes(group.id)).map((candidate) => candidate.id)]);
    const repoCount = state.repos.filter((repo) => groupAncestors(repo.group_id).some((id) => groupIds.has(id)))
      .reduce((count, repo) => count + repo.packages.length, 0);
    const selected = state.selectedGroupIds.includes(group.id);
    return `<div class="source-tree-group">
      <div class="group-line" style="--tree-depth:${depth}">
        <input class="group-select" type="checkbox" data-group-toggle="${esc(group.id)}" ${selected ? "checked" : ""} aria-label="Include ${esc(group.name)} and nested groups">
        <button class="group-item ${selected ? "selected" : ""}" data-group="${esc(group.id)}" title="Show packages in ${esc(group.name)} and nested groups"><svg class="group-folder-icon" viewBox="0 0 20 18" aria-hidden="true"><path d="M2.5 5.25A1.75 1.75 0 0 1 4.25 3.5h4l1.8 1.8h5.7A1.75 1.75 0 0 1 17.5 7v7.25A1.75 1.75 0 0 1 15.75 16h-11A2.25 2.25 0 0 1 2.5 13.75z"/></svg><span class="repo-name">${esc(group.name)}</span><span class="repo-item-count">${repoCount}</span></button>
        <button class="group-edit" data-edit-group="${esc(group.id)}" title="Edit group" aria-label="Edit ${esc(group.name)}">⋯</button>
      </div>
      ${renderSourceTree(group.id, depth + 1)}
      ${groupedRepos.filter((repo) => repo.group_id === group.id).map((repo) => renderRepoLine(repo, depth + 1)).join("")}
    </div>`;
  }).join("") + groupedRepos.filter((repo) => !repo.group_id).map((repo) => renderRepoLine(repo, depth)).join("");
}

function renderGroupManager() {
  const parent = $("#group-parent");
  if (!parent) return;
  const selectedParent = parent.value;
  parent.innerHTML = groupOptions(state.editingGroupId || "");
  if ([...parent.options].some((option) => option.value === selectedParent)) parent.value = selectedParent;
  $("#group-form-title").textContent = state.editingGroupId ? "Edit group" : "Create a group";
  $("#group-name").value = state.editingGroupId
    ? state.groups.find((group) => group.id === state.editingGroupId)?.name || ""
    : $("#group-name").value;
  $("#group-save").textContent = state.editingGroupId ? "Save group" : "Create group";
  $("#group-cancel-edit").hidden = !state.editingGroupId;
  $("#group-list").innerHTML = state.groups.length
    ? state.groups.map((group) => `<div class="group-manager-row"><span>${"— ".repeat(groupAncestors(group.id).length - 1)}${esc(group.name)}</span><span><button class="secondary-button" data-edit-group="${esc(group.id)}">Edit</button><button class="danger-button" data-delete-group="${esc(group.id)}">Delete</button></span></div>`).join("")
    : '<p class="settings-help">No groups yet. Create one above; you can nest groups by choosing a parent.</p>';
  $("#repo-group-list").innerHTML = state.repos.length
    ? state.repos.map((repo) => `<label class="repo-group-row"><span>${esc(repo.name)}</span><select class="text-field" data-repo-group="${esc(repo.id)}">${groupOptions()}</select></label>`).join("")
    : '<p class="settings-help">Add a source before organizing it into a group.</p>';
  state.repos.forEach((repo) => {
    const select = document.querySelector(`[data-repo-group="${CSS.escape(repo.id)}"]`);
    if (select) select.value = repo.group_id || "";
  });
}

function renderAutomationRepoIntervals() {
  const overrides = state.automation?.per_repo_intervals_seconds || {};
  const groupOverrides = state.automation?.per_group_intervals_seconds || {};
  const sourceRow = (repo) => `
    <label class="automation-repo-row" style="--tree-depth:${groupAncestors(repo.group_id).length}">
      <span>${esc(repo.name)}</span>
      <span class="automation-repo-input"><input class="text-field automation-duration" type="text" data-repo-interval="${esc(repo.id)}" value="${overrides[repo.id] ? esc(formatDuration(overrides[repo.id])) : ""}" placeholder="Use inherited interval" autocomplete="off" aria-label="Check interval for ${esc(repo.name)}"></span>
    </label>`;
  const groupRows = (parentId, depth = 0) => state.groups
    .filter((group) => (group.parent_id || null) === parentId)
    .map((group) => `<div class="automation-group-block">
      <label class="automation-group-row" style="--tree-depth:${depth}">
        <strong>▰ ${esc(group.name)}</strong>
        <span class="automation-repo-input"><input class="text-field automation-duration" type="text" data-group-interval="${esc(group.id)}" value="${groupOverrides[group.id] ? esc(formatDuration(groupOverrides[group.id])) : ""}" placeholder="Use inherited interval" autocomplete="off" aria-label="Check interval for ${esc(group.name)} and its sources"></span>
      </label>
      ${groupRows(group.id, depth + 1)}
      ${state.repos.filter((repo) => repo.group_id === group.id).map(sourceRow).join("")}
    </div>`).join("");
  const ungrouped = state.repos.filter((repo) => !state.settings.grouping_enabled || !repo.group_id).map(sourceRow).join("");
  $("#automation-repo-intervals").innerHTML = state.repos.length
    ? `${state.settings.grouping_enabled ? groupRows(null) : ""}${ungrouped}`
    : '<p class="settings-help">Add a source to set an individual check interval.</p>';
}

function renderQueueGroupFilter() {
  const container = $("#queue-group-filter-options");
  if (!container) return;
  container.innerHTML = state.settings.grouping_enabled && state.groups.length
    ? state.groups.map((group) => `<label style="--tree-depth:${groupAncestors(group.id).length - 1}"><input type="checkbox" data-queue-group="${esc(group.id)}" ${state.selectedQueueGroupIds.includes(group.id) ? "checked" : ""}>${esc(group.name)}</label>`).join("")
    : "";
  $("#queue-group-filter").hidden = !state.settings.grouping_enabled || !state.groups.length;
}

function renderQueue() {
  const activeJobs = state.jobs.filter((job) => ACTIVE_JOB_STATUSES.has(job.status));
  $("#nav-queue-count").textContent = activeJobs.reduce(
    (count, job) => count + Math.max(0, job.total - job.completed),
    0,
  ).toLocaleString();
  $("#queue-loading").hidden = state.queueLoaded || Boolean(state.queueError);
  const visibleJobs = state.jobs.filter((job) => !state.selectedQueueGroupIds.length || (
    job.source_ids || []).some((repoId) => {
      const repo = state.repos.find((item) => item.id === repoId);
      return repo && groupAncestors(repo.group_id).some((groupId) => state.selectedQueueGroupIds.includes(groupId));
    }));
  $("#queue-empty").hidden = !state.queueLoaded || visibleJobs.length > 0;
  $("#queue-empty").querySelector("strong").textContent = state.jobs.length
    ? "No queues match these groups"
    : "No downloads yet";
  $("#queue-empty").querySelector("p").textContent = state.jobs.length
    ? "Choose different groups or clear the group filter to see other queues."
    : "Packages you start downloading will appear here.";
  $("#queue-error").hidden = !state.queueError;
  $("#queue-error").innerHTML = state.queueError
    ? `${esc(state.queueError)} <button class="secondary-button" id="queue-retry">Retry</button>`
    : "";
  $("#queue-jobs").innerHTML = visibleJobs.slice(0, 30).map((job) => {
    const canControl = ACTIVE_JOB_STATUSES.has(job.status);
    const items = job.items.filter((item) => item.status !== "removed");
    const page = state.queuePages[job.id] || 0;
    const pageCount = Math.max(1, Math.ceil(job.item_count / PAGE_SIZE));
    const pageOffset = job.item_offset || 0;
    const failures = job.failed_items || [];
    const failurePage = state.failurePages[job.id] || 0;
    const failurePageCount = Math.max(1, Math.ceil((job.failed_count || 0) / FAILURE_PAGE_SIZE));
    return `<article class="queue-job">
      <header><div><strong>${job.automatic ? "Automatic downloads" : "Package downloads"}</strong><span>${esc(job.status)} · ${job.completed.toLocaleString()} of ${job.total.toLocaleString()} processed</span></div>
      <div class="queue-job-actions">${canControl ? `<button class="secondary-button" data-job-action="${job.status === "paused" ? "resume" : "pause"}" data-job="${job.id}">${job.status === "paused" ? "Resume" : "Pause"}</button><button class="danger-button" data-job-action="cancel" data-job="${job.id}">Cancel</button>` : ""}</div></header>
      <div class="queue-items">${items.map((item) => `<div class="queue-item"><span class="queue-item-status ${esc(item.status)}">${esc(item.status)}</span><strong title="${esc(item.name)}">${esc(item.name)}</strong>${item.error ? `<small>${esc(item.error)}</small>` : ""}${item.status === "pending" ? `<button class="queue-remove" data-job-action="remove" data-job="${job.id}" data-item="${item.id}" aria-label="Remove ${esc(item.name)} from queue">Remove</button>` : ""}</div>`).join("") || '<div class="queue-empty-note">No packages remain in this queue.</div>'}</div>
      ${job.item_count > PAGE_SIZE ? `<footer class="queue-pagination"><span>Showing ${pageOffset + 1}–${Math.min(pageOffset + items.length, job.item_count)} of ${job.item_count.toLocaleString()}</span><div><button class="secondary-button" data-job-page="-1" data-job="${job.id}" ${page <= 0 ? "disabled" : ""}>Previous</button><span>Page ${page + 1} of ${pageCount}</span><button class="secondary-button" data-job-page="1" data-job="${job.id}" ${page >= pageCount - 1 ? "disabled" : ""}>Next</button></div></footer>` : ""}
      ${!canControl && job.error_count ? `<section class="queue-failures"><header><div><strong>${job.error_count.toLocaleString()} package${job.error_count === 1 ? "" : "s"} failed</strong><span>Review each reason and retry individually, or retry all failures.</span></div><button class="secondary-button" data-retry-job="${job.id}">Retry all</button></header>
        <div class="queue-failure-list">${failures.map((item) => `<article class="queue-failure"><div><strong>${esc(item.name)}</strong><small>${esc(item.error)}</small></div><button class="secondary-button" data-retry-job="${job.id}" data-retry-item="${item.id}">Retry</button></article>`).join("") || '<div class="queue-empty-note">Loading failed packages…</div>'}</div>
        ${(job.failed_count || 0) > FAILURE_PAGE_SIZE ? `<footer class="queue-pagination"><span>Showing ${(failurePage * FAILURE_PAGE_SIZE + 1).toLocaleString()}–${Math.min((failurePage + 1) * FAILURE_PAGE_SIZE, job.failed_count).toLocaleString()} of ${job.failed_count.toLocaleString()} failures</span><div><button class="secondary-button" data-failure-page="-1" data-job="${job.id}" ${failurePage <= 0 ? "disabled" : ""}>Previous</button><span>Page ${failurePage + 1} of ${failurePageCount}</span><button class="secondary-button" data-failure-page="1" data-job="${job.id}" ${failurePage >= failurePageCount - 1 ? "disabled" : ""}>Next</button></div></footer>` : ""}
      </section>` : ""}
    </article>`;
  }).join("");
}

async function refreshQueue() {
  if (state.queueRefreshPromise) return state.queueRefreshPromise;
  state.queueRefreshPromise = (async () => {
    try {
      const result = await api(`/api/jobs?limit=${PAGE_SIZE}`);
      state.jobs = await Promise.all(result.jobs.map(async (job) => {
        const pageCount = Math.max(1, Math.ceil(job.item_count / PAGE_SIZE));
        const page = Math.min(state.queuePages[job.id] || 0, pageCount - 1);
        state.queuePages[job.id] = page;
        const queueJob = (page === 0 && job.items.length) || !job.item_count
          ? job
          : await api(`/api/jobs/${job.id}?offset=${page * PAGE_SIZE}&limit=${PAGE_SIZE}`);
        if (!ACTIVE_JOB_STATUSES.has(job.status) && job.error_count) {
          const failurePage = state.failurePages[job.id] || 0;
          const offset = failurePage * FAILURE_PAGE_SIZE;
          let failures = state.failureCache[job.id];
          if (!failures || failures.count !== job.error_count || failures.offset !== offset) {
            const result = await api(`/api/jobs/${job.id}/failures?offset=${offset}&limit=${FAILURE_PAGE_SIZE}`);
            failures = { count: job.error_count, offset, total: result.total, items: result.items };
            state.failureCache[job.id] = failures;
          }
          return { ...queueJob, failed_items: failures.items, failed_count: failures.total };
        }
        return queueJob;
      }));
      state.queueError = "";
      state.queueLoaded = true;
      renderQueue();
    } catch (error) {
      state.queueError = `Could not load the download queue: ${error.message}`;
      state.queueLoaded = false;
      renderQueue();
      throw error;
    } finally {
      state.queueRefreshPromise = null;
    }
  })();
  return state.queueRefreshPromise;
}

async function controlJob(jobId, action, itemId = "") {
  if (!jobId) return;
  try {
    await api(`/api/jobs/${jobId}/control`, {
      method: "POST",
      body: JSON.stringify({ action, item_id: itemId }),
    });
    await refreshQueue();
  } catch (error) {
    toast(error.message, true);
  }
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
  const [result, queue] = await Promise.all([api("/api/state"), api("/api/jobs?limit=0")]);
  state.repos = result.repos;
  state.groups = result.groups || [];
  state.jobs = queue.jobs;
  state.automation = result.automation;
  state.settings = { logging_enabled: false, show_automation_banner: true, grouping_enabled: true, ...(result.settings || {}) };
  state.download_dir = result.download_dir;
  $("#uninstall-card").hidden = Boolean(result.packaged_install);
  if (state.selectedRepo && !state.repos.some((repo) => repo.id === state.selectedRepo)) state.selectedRepo = null;
  if (state.selectedGroupIds.some((id) => !state.groups.some((group) => group.id === id))) state.selectedGroupIds = [];
  render();
  renderQueue();
  if (state.settings.keep_download_status_until_done && !state.activeJobId) {
    const activeJob = state.jobs.find((job) => ACTIVE_JOB_STATUSES.has(job.status));
    if (activeJob) {
      const title = activeJob.automatic ? "Checking sources and downloading updates" : `Downloading ${activeJob.total.toLocaleString()} packages`;
      watchJob(activeJob.id, title);
    }
  }
}

function openDialog(dialog) {
  dialog.showModal();
}

async function startJob(ids, title) {
  if (!ids.length) return toast("No packages to download.");
  const result = await api("/api/downloads", { method: "POST", body: JSON.stringify({ package_ids: ids }) });
  watchJob(result.job_id, title);
  await refreshQueue();
}

function watchJob(jobId, title) {
  state.activeJobId = jobId;
  const banner = $("#job-banner");
  banner.hidden = false;
  $("#job-title").textContent = title;
  $("#job-status").textContent = "Starting…";
  $("#job-progress-fill").style.width = "0%";
  $("#job-percent").textContent = "0%";
  $("#job-current-name").textContent = "Preparing download…";
  $("#job-current-size").textContent = "";
  $("#job-count").textContent = "0 packages";
  $("#job-rate").textContent = "";
  $("#job-errors").hidden = true;
  clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    try {
      const queueOffset = state.currentView === "queue" ? (state.queuePages[jobId] || 0) * PAGE_SIZE : 0;
      const job = await api(`/api/jobs/${jobId}?offset=${queueOffset}&limit=${PAGE_SIZE}`);
      const fileProgress = job.current_size > 0
        ? Math.min(1, job.current_bytes / job.current_size)
        : 0;
      const percent = job.total
        ? Math.min(100, ((job.completed + fileProgress) / job.total) * 100)
        : 100;
      $("#job-progress-fill").style.width = `${percent}%`;
      $(".job-progress").setAttribute("aria-valuenow", String(Math.round(percent)));
      $("#job-percent").textContent = percent > 0 && percent < 1
        ? `${percent.toFixed(2)}%`
        : `${percent.toFixed(percent < 10 ? 1 : 0)}%`;
      $("#job-count").textContent = `${job.completed.toLocaleString()} of ${job.total.toLocaleString()} packages`;
      $("#job-current-name").textContent = job.current_package
        ? `Downloading ${job.current_package}`
        : job.status === "queued" ? "Waiting in the download queue…"
          : job.status === "paused" ? "Download paused"
            : job.status === "cancelling" ? "Stopping the current download…"
              : "Download queue complete";
      $("#job-current-size").textContent = job.current_package
        ? job.current_size > 0
          ? `${byteLabel(job.current_bytes)} / ${byteLabel(job.current_size)}`
          : `${byteLabel(job.downloaded_bytes)} downloaded`
        : job.downloaded_bytes > 0 ? `${byteLabel(job.downloaded_bytes)} downloaded` : "";
      if (job.downloaded_bytes > 0 && job.transfer_started_at > 0) {
        const elapsedSeconds = Math.max(0.25, Date.now() / 1000 - job.transfer_started_at);
        $("#job-rate").textContent = `${byteLabel(job.downloaded_bytes / elapsedSeconds)}/s avg`;
      } else {
        $("#job-rate").textContent = "";
      }
      $("#job-size-progress").textContent = job.size_known_count === job.item_count && job.total_bytes > 0
        ? `${byteLabel(job.downloaded_bytes)} / ${byteLabel(job.total_bytes)}`
        : job.downloaded_bytes > 0 ? `${byteLabel(job.downloaded_bytes)} downloaded` : "";
      $("#job-status").textContent = job.total
        ? job.status === "paused" ? "Paused"
          : job.status === "queued" ? "Waiting for another download"
            : job.status === "cancelling" ? "Cancelling after the current read…"
              : `${job.completed.toLocaleString()} complete · ${Math.max(0, job.total - job.completed).toLocaleString()} remaining`
        : "All packages are already saved";
      $("#job-pause").textContent = job.status === "paused" ? "Resume" : "Pause";
      $("#job-pause").disabled = !["queued", "running", "paused"].includes(job.status);
      $("#job-cancel").disabled = !["queued", "running", "paused"].includes(job.status);
      $(".job-spinner").style.animationPlayState = job.status === "paused" ? "paused" : "";
      state.jobs = [job, ...state.jobs.filter((entry) => entry.id !== job.id)];
      if (state.currentView === "queue") renderQueue();
      if (job.status !== "running") {
        if (["completed", "cancelled", "failed"].includes(job.status)) {
          clearInterval(state.jobTimer);
          await loadState();
          if (job.errors.length) toast(`${job.errors.length} package${job.errors.length === 1 ? "" : "s"} could not be downloaded. See the job panel details.`, true);
          else toast(job.status === "cancelled" ? "Download queue cancelled." : job.total ? "Downloads finished." : "Your library is already up to date.");
        $("#job-title").textContent = job.errors.length ? "Download finished with errors" : "Downloads complete";
        $("#job-status").textContent = job.errors.length
          ? `${job.completed} of ${job.total} packages processed`
          : job.status === "cancelled" ? "Queue cancelled." : "Your local library is up to date.";
        $("#job-current-name").textContent = job.errors.length
          ? `${job.errors.length} package${job.errors.length === 1 ? "" : "s"} failed`
          : job.status === "cancelled" ? "Queue cancelled."
            : "All requested packages are saved.";
        if (job.errors.length) {
          $("#job-errors").textContent = job.errors.join(" · ");
          $("#job-errors").hidden = false;
        }
          $(".job-spinner").style.animationPlayState = "paused";
          setTimeout(() => { banner.hidden = true; $(".job-spinner").style.animationPlayState = ""; }, 6000);
        }
      }
      if (state.currentView === "queue") await refreshQueue();
    } catch (error) {
      if (state.settings.keep_download_status_until_done) {
        $("#job-status").textContent = "Connection interrupted — retrying…";
      } else {
        clearInterval(state.jobTimer);
        banner.hidden = true;
        toast(error.message, true);
      }
    }
  }, 700);
}

async function retryFailedPackages(jobId, itemId = "") {
  try {
    const result = await api(`/api/jobs/${jobId}/retry`, {
      method: "POST",
      body: JSON.stringify(itemId ? { item_id: itemId } : {}),
    });
    watchJob(result.job_id, `Retrying ${result.retry_count.toLocaleString()} failed package${result.retry_count === 1 ? "" : "s"}`);
    await refreshQueue();
  } catch (error) {
    toast(error.message, true);
  }
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
  $("#device-info-section").hidden = state.settings.show_device_info_on_add === false && !paid;
  const fieldsVisible = !$("#device-info-section").hidden;
  $("#device-profile-fields").hidden = !enabled;
  $("#device-model").required = enabled && fieldsVisible;
  $("#device-os-version").required = enabled && fieldsVisible;
  $("#device-client-version").required = enabled && fieldsVisible;
  $("#device-profile-enabled").disabled = paid;
  $("#generate-add-profile").disabled = paid || !enabled;
  $("#manual-device-id").disabled = paid;
  $("#device-id-value").readOnly = !$("#manual-device-id").checked;
  $("#device-id-value").required = enabled && fieldsVisible && $("#manual-device-id").checked;
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
$("#generate-add-profile").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (requiresManualDeviceId($("#repo-url").value)) {
    toast("Paid repositories require the device ID authorized by their provider.", true);
    return;
  }
  button.disabled = true;
  button.textContent = "Generating details…";
  try {
    const result = await api("/api/device-profile/generate");
    $("#device-profile-enabled").checked = true;
    $("#device-model").value = result.model;
    $("#device-os-version").value = result.os_version;
    $("#device-architecture").value = result.architecture;
    $("#device-client-version").value = result.client_version;
    $("#manual-device-id").checked = false;
    $("#device-id-value").value = result.generated_device_id;
    state.deviceIdRequest = (state.deviceIdRequest || 0) + 1;
    updateDeviceProfileFields();
    toast("Generated a compatible device profile for this source.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.textContent = "Generate compatible device details";
    updateDeviceProfileFields();
  }
});
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
  state.currentView = "automation";
  clearInterval(state.queueTimer);
  render();
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
    const perRepoIntervals = {};
    document.querySelectorAll("[data-repo-interval]").forEach((input) => {
      if (input.value.trim() !== "") {
        perRepoIntervals[input.dataset.repoInterval] = parseDuration(input.value);
      }
    });
    const perGroupIntervals = {};
    document.querySelectorAll("[data-group-interval]").forEach((input) => {
        if (input.value.trim() !== "") {
          perGroupIntervals[input.dataset.groupInterval] = parseDuration(input.value);
        }
    });
    const intervalSeconds = parseDuration($("#automation-interval").value);
    const result = await api("/api/automation", {
      method: "POST",
      body: JSON.stringify({
        enabled: $("#automation-enabled").checked,
        interval_seconds: intervalSeconds,
        per_repo_intervals_seconds: perRepoIntervals,
        per_group_intervals_seconds: state.settings.grouping_enabled
          ? perGroupIntervals
          : state.automation?.per_group_intervals_seconds || {},
      }),
    });
    state.automation = result.automation;
    render();
    toast(result.automation.enabled ? "Automation settings saved." : "Automation turned off.");
  } catch (error) {
    $("#automation-error").textContent = error.message;
  }
});
$("#automation-form").addEventListener("focusout", (event) => {
  const input = event.target.closest(".automation-duration");
  if (input && input.value.trim()) {
    try {
      input.value = formatDuration(parseDuration(input.value));
    } catch {
      return;
    }
  }
});

$("#run-now-button").addEventListener("click", async () => {
  $("#automation-error").textContent = "";
  try {
    const result = await api("/api/automation/run", { method: "POST", body: "{}" });
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

$("#get-all-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const label = button.querySelector("span");
  button.disabled = true;
  label.textContent = "Preparing queue…";
  try {
    const result = await api("/api/downloads", {
      method: "POST",
      body: JSON.stringify({
        selection: {
          mode: "visible",
          repo_id: state.selectedRepo,
          group_ids: state.settings.grouping_enabled ? state.selectedGroupIds : [],
          filter: state.filter,
          search: state.search,
        },
      }),
    });
    watchJob(result.job_id, `Downloading ${result.total.toLocaleString()} packages`);
    toast(result.total ? `Queued ${result.total.toLocaleString()} packages.` : "All matching packages are already downloaded.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    label.textContent = "Get all";
  }
});
$("#get-selected-button").addEventListener("click", () => {
  const ids = allPackages().filter((item) => state.selected.has(item.id) && !item.downloaded).map((item) => item.id);
  startJob(ids, `Downloading ${ids.length} selected packages`);
});

$("#search-input").addEventListener("input", (event) => {
  state.search = event.target.value.trim();
  state.page = 0;
  render();
});
$("#all-packages-nav").addEventListener("click", () => {
  state.currentView = "library";
  state.selectedRepo = null;
  state.selectedGroupIds = [];
  state.page = 0;
  clearInterval(state.queueTimer);
  render();
});
$("#queue-nav").addEventListener("click", async () => {
  state.currentView = "queue";
  state.queueLoaded = false;
  state.queueError = "";
  render();
  try { await refreshQueue(); } catch (error) { toast(error.message, true); }
  clearInterval(state.queueTimer);
  state.queueTimer = setInterval(() => refreshQueue().catch(() => {}), 3000);
});
$("#automation-nav").addEventListener("click", () => {
  state.currentView = "automation";
  clearInterval(state.queueTimer);
  render();
});
$("#settings-nav").addEventListener("click", () => {
  state.currentView = "settings";
  clearInterval(state.queueTimer);
  render();
  api("/api/device-profile")
    .then((result) => {
      state.generatedDeviceId = result.generated_device_id;
      $("#generated-device-id").textContent = state.generatedDeviceId;
    })
    .catch((error) => {
      $("#generated-device-id").textContent = error.message;
    });
});
function openGroupManager(groupId = null) {
  state.editingGroupId = groupId;
  $("#group-error").textContent = "";
  $("#group-name").value = state.groups.find((group) => group.id === groupId)?.name || "";
  renderGroupManager();
  $("#group-parent").value = state.groups.find((group) => group.id === groupId)?.parent_id || "";
  openDialog($("#group-manager-dialog"));
  if (!groupId) $("#group-name").focus();
}
$("#group-manager-button").addEventListener("click", () => openGroupManager());
$("#group-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#group-save");
  button.disabled = true;
  $("#group-error").textContent = "";
  const payload = {
    action: state.editingGroupId ? "update" : "create",
    name: $("#group-name").value.trim(),
    parent_id: $("#group-parent").value || null,
  };
  if (state.editingGroupId) payload.group_id = state.editingGroupId;
  try {
    await api("/api/groups", { method: "POST", body: JSON.stringify(payload) });
    state.editingGroupId = null;
    $("#group-name").value = "";
    await loadState();
    $("#group-parent").value = "";
    toast(payload.action === "create" ? "Group created." : "Group updated.");
  } catch (error) {
    $("#group-error").textContent = error.message;
  } finally {
    button.disabled = false;
    renderGroupManager();
  }
});
$("#group-cancel-edit").addEventListener("click", () => openGroupManager());
$("#group-list").addEventListener("click", async (event) => {
  const editButton = event.target.closest("[data-edit-group]");
  if (editButton) {
    openGroupManager(editButton.dataset.editGroup);
    return;
  }
  const deleteButton = event.target.closest("[data-delete-group]");
  if (!deleteButton) return;
  const group = state.groups.find((item) => item.id === deleteButton.dataset.deleteGroup);
  if (!group || !confirm(`Delete the group “${group.name}”? Its child groups and sources will move to its parent.`)) return;
  try {
    await api(`/api/groups/${encodeURIComponent(group.id)}`, { method: "DELETE", body: "{}" });
    state.selectedGroupIds = state.selectedGroupIds.filter((id) => id !== group.id);
    await loadState();
    toast("Group deleted; its contents were kept in the parent group.");
  } catch (error) {
    $("#group-error").textContent = error.message;
  }
});
$("#repo-group-list").addEventListener("change", async (event) => {
  const select = event.target.closest("[data-repo-group]");
  if (!select) return;
  select.disabled = true;
  try {
    await api(`/api/repos/${encodeURIComponent(select.dataset.repoGroup)}/group`, {
      method: "POST",
      body: JSON.stringify({ group_id: select.value || null }),
    });
    await loadState();
    toast("Source group updated.");
  } catch (error) {
    toast(error.message, true);
    renderGroupManager();
  } finally {
    select.disabled = false;
  }
});
$("#repo-list").addEventListener("click", async (event) => {
  const editGroup = event.target.closest("[data-edit-group]");
  if (editGroup) {
    openGroupManager(editGroup.dataset.editGroup);
    return;
  }
  const removeButton = event.target.closest("[data-remove]");
  if (removeButton) {
    const repo = state.repos.find((item) => item.id === removeButton.dataset.remove);
    if (!repo) return;
    state.pendingRepoRemoval = repo;
    $("#remove-repo-title").textContent = `Remove ${repo.name}?`;
    const downloadedCount = repo.packages.filter((item) => item.downloaded).length;
    $("#remove-repo-detail").textContent = downloadedCount
      ? `This removes the source and its ${downloadedCount.toLocaleString()} saved package${downloadedCount === 1 ? "" : "s"} from the library. Choose below whether to delete those downloaded files or keep them on disk.`
      : "This removes the source from RepoShelf. No downloaded packages from this source are currently in the library.";
    $("#delete-repo-downloads").checked = false;
    $("#remove-repo-error").textContent = "";
    $("#remove-repo-confirm").disabled = false;
    openDialog($("#remove-repo-dialog"));
    return;
  }
  const groupButton = event.target.closest("[data-group]");
  if (groupButton) {
    state.currentView = "library";
    state.selectedRepo = null;
    state.selectedGroupIds = [groupButton.dataset.group];
    state.page = 0;
    clearInterval(state.queueTimer);
    render();
    return;
  }
  const button = event.target.closest("[data-repo]");
  if (!button) return;
  const repo = state.repos.find((item) => item.id === button.dataset.repo);
  if (!repo) return;
  state.selectedRepo = state.selectedRepo === repo.id ? null : repo.id;
  state.selectedGroupIds = [];
  state.page = 0;
  render();
});
$("#repo-list").addEventListener("change", (event) => {
  const toggle = event.target.closest("[data-group-toggle]");
  if (!toggle) return;
  state.selectedRepo = null;
  state.selectedGroupIds = toggle.checked
    ? [...new Set([...state.selectedGroupIds, toggle.dataset.groupToggle])]
    : state.selectedGroupIds.filter((id) => id !== toggle.dataset.groupToggle);
  state.page = 0;
  render();
});
$("#queue-jobs").addEventListener("click", (event) => {
  const retryButton = event.target.closest("[data-retry-job]");
  if (retryButton) {
    retryFailedPackages(retryButton.dataset.retryJob, retryButton.dataset.retryItem || "");
    return;
  }
  const button = event.target.closest("[data-job-action]");
  if (button) {
    controlJob(button.dataset.job, button.dataset.jobAction, button.dataset.item || "");
    return;
  }
  const pageButton = event.target.closest("[data-job-page]");
  if (pageButton && !pageButton.disabled) {
    state.queuePages[pageButton.dataset.job] = (state.queuePages[pageButton.dataset.job] || 0) + Number(pageButton.dataset.jobPage);
    state.activeJobId = pageButton.dataset.job;
    refreshQueue().catch((error) => toast(error.message, true));
    return;
  }
  const failurePageButton = event.target.closest("[data-failure-page]");
  if (failurePageButton && !failurePageButton.disabled) {
    state.failurePages[failurePageButton.dataset.job] = (state.failurePages[failurePageButton.dataset.job] || 0) + Number(failurePageButton.dataset.failurePage);
    refreshQueue().catch((error) => toast(error.message, true));
  }
});
$("#queue-group-filter-options").addEventListener("change", (event) => {
  const checkbox = event.target.closest("[data-queue-group]");
  if (!checkbox) return;
  state.selectedQueueGroupIds = checkbox.checked
    ? [...new Set([...state.selectedQueueGroupIds, checkbox.dataset.queueGroup])]
    : state.selectedQueueGroupIds.filter((id) => id !== checkbox.dataset.queueGroup);
  renderQueue();
});
$("#queue-error").addEventListener("click", (event) => {
  if (event.target.closest("#queue-retry")) refreshQueue().catch(() => {});
});
$("#job-pause").addEventListener("click", () => {
  const job = state.jobs.find((item) => item.id === state.activeJobId);
  controlJob(state.activeJobId, job?.status === "paused" ? "resume" : "pause");
});
$("#job-cancel").addEventListener("click", () => controlJob(state.activeJobId, "cancel"));
$("#job-minimize").addEventListener("click", () => {
  state.jobMinimized = !state.jobMinimized;
  $("#job-banner").classList.toggle("minimized", state.jobMinimized);
  $("#job-minimize").textContent = state.jobMinimized ? "+" : "−";
  $("#job-minimize").setAttribute("aria-label", state.jobMinimized ? "Restore download status" : "Minimize download status");
  $("#job-minimize").setAttribute("aria-expanded", String(!state.jobMinimized));
});
const jobBannerHandle = $("#job-banner-handle");
jobBannerHandle.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || event.target.closest("button")) return;
  const banner = $("#job-banner");
  const rect = banner.getBoundingClientRect();
  const offsetX = event.clientX - rect.left;
  const offsetY = event.clientY - rect.top;
  jobBannerHandle.setPointerCapture(event.pointerId);
  const move = (moveEvent) => {
    const left = Math.max(0, Math.min(window.innerWidth - rect.width, moveEvent.clientX - offsetX));
    const top = Math.max(0, Math.min(window.innerHeight - rect.height, moveEvent.clientY - offsetY));
    banner.style.left = `${left}px`;
    banner.style.top = `${top}px`;
    banner.style.right = "auto";
    banner.style.bottom = "auto";
  };
  const stop = () => {
    jobBannerHandle.removeEventListener("pointermove", move);
    jobBannerHandle.removeEventListener("pointerup", stop);
    jobBannerHandle.removeEventListener("pointercancel", stop);
  };
  jobBannerHandle.addEventListener("pointermove", move);
  jobBannerHandle.addEventListener("pointerup", stop, { once: true });
  jobBannerHandle.addEventListener("pointercancel", stop, { once: true });
});
$("#generate-device-details").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!confirm("Generate a new archive device ID? Sources using the generated profile will use it on their next request. Manually authorized paid-repository IDs will not change.")) return;
  button.disabled = true;
  try {
    const result = await api("/api/device-profile/regenerate", {
      method: "POST",
      body: "{}",
    });
    state.generatedDeviceId = result.generated_device_id;
    $("#generated-device-id").textContent = state.generatedDeviceId;
    toast("New archive device details generated.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});
$("#logging-enabled").addEventListener("change", async (event) => {
  const toggle = event.currentTarget;
  toggle.disabled = true;
  try {
    const result = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ logging_enabled: toggle.checked }),
    });
    state.settings = { ...state.settings, ...result.settings };
    toast(toggle.checked ? "Application logging enabled." : "Application logging disabled.");
  } catch (error) {
    toggle.checked = Boolean(state.settings.logging_enabled);
    toast(error.message, true);
  } finally {
    toggle.disabled = false;
  }
});
$("#show-automation-banner").addEventListener("change", async (event) => {
  const toggle = event.currentTarget;
  toggle.disabled = true;
  try {
    const result = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ show_automation_banner: toggle.checked }),
    });
    state.settings = { ...state.settings, ...result.settings };
    render();
    toast(toggle.checked ? "Automation setup banner enabled." : "Automation setup banner hidden.");
  } catch (error) {
    toggle.checked = state.settings.show_automation_banner !== false;
    toast(error.message, true);
  } finally {
    toggle.disabled = false;
  }
});
$("#keep-download-status").addEventListener("change", async (event) => {
  const toggle = event.currentTarget;
  toggle.disabled = true;
  try {
    const result = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ keep_download_status_until_done: toggle.checked }),
    });
    state.settings = { ...state.settings, ...result.settings };
    if (toggle.checked && !state.activeJobId) {
      const activeJob = state.jobs.find((job) => ACTIVE_JOB_STATUSES.has(job.status));
      if (activeJob) watchJob(activeJob.id, activeJob.automatic ? "Checking sources and downloading updates" : `Downloading ${activeJob.total.toLocaleString()} packages`);
    }
    toast(toggle.checked ? "Download status will stay visible until active downloads finish." : "Download status visibility setting updated.");
  } catch (error) {
    toggle.checked = Boolean(state.settings.keep_download_status_until_done);
    toast(error.message, true);
  } finally {
    toggle.disabled = false;
  }
});
$("#show-device-info-on-add").addEventListener("change", async (event) => {
  const toggle = event.currentTarget;
  toggle.disabled = true;
  try {
    const result = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ show_device_info_on_add: toggle.checked }),
    });
    state.settings = { ...state.settings, ...result.settings };
    if ($("#source-dialog").open) updateDeviceProfileFields();
    toast(toggle.checked ? "Device info will appear when adding a source." : "Device info will be hidden for sources that do not require manual authorization.");
  } catch (error) {
    toggle.checked = state.settings.show_device_info_on_add !== false;
    toast(error.message, true);
  } finally {
    toggle.disabled = false;
  }
});
$("#grouping-enabled").addEventListener("change", async (event) => {
  const toggle = event.currentTarget;
  toggle.disabled = true;
  try {
    const result = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ grouping_enabled: toggle.checked }),
    });
    state.settings = { ...state.settings, ...result.settings };
    if (!toggle.checked) {
      state.selectedGroupIds = [];
      state.selectedQueueGroupIds = [];
      state.selectedRepo = null;
    }
    render();
    toast(toggle.checked ? "Source grouping enabled." : "Source grouping hidden; saved group assignments are kept.");
  } catch (error) {
    toggle.checked = state.settings.grouping_enabled !== false;
    toast(error.message, true);
  } finally {
    toggle.disabled = false;
  }
});

$("#uninstall-button").addEventListener("click", () => {
  $("#delete-app-data").checked = false;
  openDialog($("#uninstall-choice-dialog"));
});
$("#uninstall-choice-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.deleteAppData = $("#delete-app-data").checked;
  $("#uninstall-confirm-detail").textContent = state.deleteAppData
    ? "This removes RepoShelf and permanently deletes downloaded tweaks, repository configuration, logs, and other local app data."
    : "This removes RepoShelf. Downloaded packages and saved repository configuration will be kept.";
  $("#uninstall-confirm-button").textContent = state.deleteAppData
    ? "Uninstall and delete data"
    : "Uninstall, keep data";
  $("#uninstall-error").textContent = "";
  $("#uninstall-choice-dialog").close();
  openDialog($("#uninstall-confirm-dialog"));
});
$("#uninstall-confirm-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#uninstall-confirm-button");
  button.disabled = true;
  $("#uninstall-error").textContent = "";
  try {
    await api("/api/uninstall", {
      method: "POST",
      body: JSON.stringify({ delete_data: state.deleteAppData }),
    });
    $("#uninstall-confirm-dialog").close();
    toast("Uninstall started.");
  } catch (error) {
    $("#uninstall-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
document.querySelectorAll(".filter-tab").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".filter-tab").forEach((tab) => tab.classList.toggle("selected", tab === button));
  state.filter = button.dataset.filter;
  state.page = 0;
  render();
}));
$("#previous-page").addEventListener("click", () => {
  if (state.page > 0) {
    state.page -= 1;
    render();
  }
});
$("#next-page").addEventListener("click", () => {
  state.page += 1;
  render();
});

$("#remove-repo-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const repo = state.pendingRepoRemoval;
  if (!repo) return;
  const button = $("#remove-repo-confirm");
  button.disabled = true;
  $("#remove-repo-error").textContent = "";
  try {
    const result = await api(`/api/repos/${encodeURIComponent(repo.id)}`, {
      method: "DELETE",
      body: JSON.stringify({ delete_downloads: $("#delete-repo-downloads").checked }),
    });
    state.repos = result.state.repos;
    state.groups = result.state.groups || state.groups;
    state.selectedRepo = null;
    state.selectedGroupIds = [];
    state.pendingRepoRemoval = null;
    $("#remove-repo-dialog").close();
    render();
    toast(result.deleted_downloads
      ? `Source removed and ${result.deleted_downloads} downloaded package file${result.deleted_downloads === 1 ? "" : "s"} deleted.`
      : "Source removed. Downloaded files were kept.");
  } catch (error) {
    $("#remove-repo-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
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
