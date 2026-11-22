const state = {
  user: null,
  permissions: [],
  project: null,
  projects: [],
  users: [],
  view: "dashboard",
  folders: [],
  cases: [],
  selectedFolder: "",
  collapsedFolders: new Set(),
  repoSidebarCollapsed: false,
  selectedCycle: "",
  executionSidebarCollapsed: false,
  repoSettingsOpen: false,
  importing: false,
  importingFile: "",
  importFeedback: "",
  highlightedCaseIds: new Set(),
  selectedCaseIds: new Set(),
  repoFilters: { q: "", status: "", priority: "", type: "", last: "", includeSub: true },
  cycleTab: "cases",
  runSelection: new Set(),
  runFilter: { q: "", assignee: "", module: "", status: "" },
  boardFilter: { q: "", module: "" },
  boardSelection: new Set(),
  ws: { assignee: "me", cycle: "", status: "", q: "", includeCompleted: false },
  wsData: null,
  wsList: [],
  cmp: { ids: [], previous: "", current: "", category: "", flag: "", module: "", q: "", status: "", assignee: "" },
  cmpData: null,
  dash: { cycle: "", module: "", member: "", from: "", to: "" },
  dashData: null,
  audit: { entity: "", action: "", actor: "", date_from: "", date_to: "", q: "", page: "" },
  planId: "",
  defectFilter: { status: "__open", severity: "", q: "" },
  defectMeta: null,
  coverage: null,
};

let renderSequence = 0;
let importFeedbackTimer = null;
let draggedFolderId = "";
let pendingDeleteFolder = { id: "", name: "" };
let pendingDeleteCases = [];
const $ = (selector) => document.querySelector(selector);
const api = async (path, options = {}) => {
  const response = await fetch(`/api${path}`, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: response.statusText }));
    if (error.code === "auth_required" && !path.startsWith("/auth/")) { showLogin("Your session has ended. Please sign in again."); }
    if (error.code === "password_change_required") { showChangePassword(true); }
    const failure = new Error(error.error || response.statusText);
    failure.code = error.code || "";
    failure.duplicates = error.duplicates || [];
    throw failure;
  }
  return response.status === 204 ? {} : response.json();
};
// Fetch every page of a list endpoint (the server returns at most 200 records per page).
const apiAll = async (path) => {
  const items = [];
  for (let page = 1; page < 1000; page += 1) {
    const result = await api(`${path}${path.includes("?") ? "&" : "?"}page=${page}&page_size=200`);
    items.push(...result.items);
    if (items.length >= result.total || !result.items.length) break;
  }
  return items;
};
const can = (permission) => state.permissions.includes("*") || state.permissions.includes(permission);
const isAdmin = () => state.user?.role === "Admin";
const caseLabel = (testCase) => testCase.case_key || String(testCase.id || "").slice(0, 8);
const isMac = /Mac|iPhone|iPad/.test(navigator.platform || "");
// Inline handler arguments: JSON-encoded, then HTML-escaped, so quotes in user text can never break out.
const jsArg = (value) => esc(JSON.stringify(String(value ?? "")));
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));
const status = (value) => `<span class="badge ${esc(String(value).toLowerCase().replaceAll(" ", "-"))}">${esc(statusLabel(value) || "Not set")}</span>`;

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  window.setTimeout(() => element.classList.remove("show"), 3500);
}

function associateFormLabels(container = $("#app")) {
  container.querySelectorAll(".field").forEach((field, index) => {
    const label = field.querySelector(":scope > label");
    const control = field.querySelector(":scope > input, :scope > select, :scope > textarea");
    if (!label || !control || label.contains(control)) return;
    control.id ||= `field-${index}-${control.name || "input"}`;
    label.htmlFor = control.id;
  });
}

function heading(title, subtitle = "", action = "") {
  return `<div class="page-heading"><div><div class="eyebrow">${esc(state.project?.key || "TMExpert")} workspace</div><h1>${esc(title)}</h1>${subtitle ? `<p class="subtitle">${esc(subtitle)}</p>` : ""}</div>${action}</div>`;
}

function updateProjectSelect() {
  const select = $("#projectSelect");
  select.innerHTML = state.projects.length
    ? state.projects.map((project) => `<option value="${esc(project.id)}" ${project.id === state.project?.id ? "selected" : ""}>${esc(project.key)} · ${esc(project.name)}</option>`).join("")
    : '<option value="">No projects</option>';
}

function initials(name) {
  return String(name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0].toUpperCase()).join("") || "?";
}

function renderUserMenu() {
  const slot = $("#userMenu");
  if (!slot) return;
  if (!state.user) { slot.innerHTML = ""; return; }
  slot.innerHTML = `<button type="button" class="avatar-button" aria-label="Account menu for ${esc(state.user.name)}" aria-haspopup="menu" aria-expanded="false" onclick="toggleUserMenu(event)"><span class="avatar">${esc(initials(state.user.name))}</span><span class="user-meta"><strong>${esc(state.user.name)}</strong><small>${esc(state.user.role)}</small></span></button>
    <div id="userMenuList" class="user-menu-list" role="menu" hidden><div class="user-menu-head"><strong>${esc(state.user.name)}</strong><small>${esc(state.user.email)}</small></div><button type="button" class="folder-menu-item" onclick="showChangePassword(false)">Change password</button><button type="button" class="folder-menu-item" onclick="signOut()">Sign out</button></div>`;
}

function toggleUserMenu(event) {
  event.stopPropagation();
  const list = $("#userMenuList");
  if (!list) return;
  list.hidden = !list.hidden;
  event.currentTarget.setAttribute("aria-expanded", String(!list.hidden));
}

function applyRoleVisibility() {
  document.querySelectorAll("[data-requires]").forEach((element) => { element.hidden = !can(element.dataset.requires); });
}

function setAuthMode(on) {
  document.body.classList.toggle("auth-mode", on);
}

function showLogin(message = "") {
  state.user = null;
  state.permissions = [];
  // Nothing from the previous person's session (page, filters, selections) carries over.
  Object.assign(state, {
    view: "dashboard", selectedFolder: "", selectedCycle: "", cycleTab: "cases", importFeedback: "",
    selectedCaseIds: new Set(), runSelection: new Set(), boardSelection: new Set(), highlightedCaseIds: new Set(),
    repoFilters: { q: "", status: "", priority: "", type: "", last: "", includeSub: true },
    runFilter: { q: "", assignee: "", module: "", status: "" }, boardFilter: { q: "", module: "" },
    ws: { assignee: "me", cycle: "", status: "", q: "", includeCompleted: false }, wsData: null, wsList: [],
    cmp: { ids: [], previous: "", current: "", category: "", flag: "", module: "", q: "", status: "", assignee: "" }, cmpData: null,
    dash: { cycle: "", module: "", member: "", from: "", to: "" }, dashData: null,
    audit: { entity: "", action: "", actor: "", date_from: "", date_to: "", q: "", page: "" },
  });
  setAuthMode(true);
  renderUserMenu();
  $("#app").innerHTML = `<section class="auth-card panel"><div class="brand auth-brand"><span class="brand-mark">T</span><span>TMExpert</span></div><h1>Sign in</h1><p class="subtitle">Use the email and password your TMExpert admin gave you.</p>
    ${message ? `<div class="import-errors auth-message">${esc(message)}</div>` : ""}
    <form id="loginForm" class="auth-form"><div class="field"><label>Email</label><input name="email" type="email" autocomplete="username" required></div><div class="field"><label>Password</label><input name="password" type="password" autocomplete="current-password" required></div><p id="loginError" class="form-error" role="alert"></p><button class="button auth-submit">Sign in</button></form></section>`;
  associateFormLabels();
  $("#loginForm").onsubmit = async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    try {
      const session = await api("/auth/login", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))) });
      applySession(session);
      if (session.must_change_password) showChangePassword(true);
      else await loadWorkspace();
    } catch (error) {
      $("#loginError").textContent = error.message;
      button.disabled = false;
    }
  };
  $("#loginForm input[name=email]").focus();
}

function showChangePassword(required) {
  setAuthMode(required);
  const list = $("#userMenuList");
  if (list) list.hidden = true;
  $("#app").innerHTML = `<section class="auth-card panel"><h1>${required ? "Choose a new password" : "Change password"}</h1><p class="subtitle">${required ? "Your account is using a temporary password. Set your own password to continue." : "Other signed-in sessions for your account will be signed out."}</p>
    <form id="passwordForm" class="auth-form"><div class="field"><label>Current password</label><input name="current_password" type="password" autocomplete="current-password" required></div><div class="field"><label>New password (at least 8 characters)</label><input name="new_password" type="password" autocomplete="new-password" minlength="8" required></div><div class="field"><label>Confirm new password</label><input name="confirm_password" type="password" autocomplete="new-password" minlength="8" required></div><p id="passwordError" class="form-error" role="alert"></p>
    <div class="form-actions">${required ? '<button type="button" class="button secondary" onclick="signOut()">Sign out</button>' : '<button type="button" class="button secondary" onclick="render()">Cancel</button>'}<button class="button">Save password</button></div></form></section>`;
  associateFormLabels();
  $("#passwordForm").onsubmit = async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    if (values.new_password !== values.confirm_password) { $("#passwordError").textContent = "The new passwords do not match."; return; }
    try {
      const session = await api("/auth/change-password", { method: "POST", body: JSON.stringify({ current_password: values.current_password, new_password: values.new_password }) });
      applySession(session);
      toast("Password updated");
      if (required) await loadWorkspace(); else await render();
    } catch (error) { $("#passwordError").textContent = error.message; }
  };
}

function applySession(session) {
  state.user = session.user;
  state.permissions = session.permissions || [];
}

async function signOut() {
  try { await api("/auth/logout", { method: "POST" }); } catch (ignored) {}
  showLogin("You have signed out.");
}

async function init() {
  let session;
  try {
    session = await api("/auth/session");
    applySession(session);
  } catch (error) {
    if (error.code === "auth_required" || error.code === "auth_failed") return showLogin();
    $("#app").innerHTML = `<section class="panel empty error-state"><h2>TMExpert could not load</h2><p>${esc(error.message)}</p><button class="button" onclick="init()">Retry</button></section>`;
    return;
  }
  if (session.must_change_password) return showChangePassword(true);
  await loadWorkspace();
}

async function loadWorkspace() {
  setAuthMode(false);
  renderUserMenu();
  applyRoleVisibility();
  try {
    [state.projects, state.users] = await Promise.all([
      apiAll("/projects"),
      api("/users").then((result) => result.items),
    ]);
    if (!isAdmin() && ["admin"].includes(state.view)) state.view = "dashboard";
    if (!can("execution.update_any")) {
      state.runFilter.assignee = "__me";  // team members start with their own work
      if (state.view === "dashboard") state.view = "workspace";
    }
    state.project = state.projects[0] || null;
    updateProjectSelect();
    $("#projectSelect").onchange = async (event) => {
      state.project = state.projects.find((project) => project.id === event.target.value) || null;
      state.selectedFolder = "";
      state.selectedCycle = "";
      state.collapsedFolders = new Set();
      await render();
    };
    await render();
  } catch (error) {
    $("#app").innerHTML = `<section class="panel empty error-state"><h2>TMExpert could not load</h2><p>${esc(error.message)}</p><button class="button" onclick="init()">Retry</button></section>`;
  }
}

function projectTable() {
  const rows = state.projects.map((project) => `<tr><td><strong>${esc(project.name)}</strong><small>${esc(project.description || "No description")}</small></td><td><span class="project-key">${esc(project.key)}</span></td><td>${esc(project.application || "—")}</td><td>${esc(project.product_version || "—")}</td><td>${status(project.status)}</td><td><button class="link" onclick="openProject(${jsArg(project.id)})">Open</button></td></tr>`).join("");
  return heading("Projects", "Manage workspaces and switch between independent test repositories.", can("projects.write") ? '<button class="button" onclick="newProject()">+ New project</button>' : "") +
    `<section class="panel"><div class="panel-head"><h2>All projects</h2><span class="muted">${state.projects.length} workspace${state.projects.length === 1 ? "" : "s"}</span></div><div class="table-wrap"><table><thead><tr><th>Project</th><th>Key</th><th>Application</th><th>Version</th><th>Status</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="6"><div class="empty">No projects yet. Create a project to start organizing test work.</div></td></tr>'}</tbody></table></div></section>`;
}

function genericList(title, entity, description, records) {
  const action = entity === "test_plans" ? "New test plan" : "Create";
  const rows = records.map((record) => `<tr>${Object.entries(record).filter(([key]) => !["id", "archived", "password_hash"].includes(key)).slice(0, 7).map(([key, value]) => `<td>${key === "status" ? status(value) : esc(Array.isArray(value) ? value.join(", ") : value && typeof value === "object" ? JSON.stringify(value) : value) || "—"}</td>`).join("")}</tr>`).join("");
  const columns = records.length ? Object.keys(records[0]).filter((key) => !["id", "archived", "password_hash"].includes(key)).slice(0, 7) : [];
  const writePermission = entity === "test_plans" ? "plans.write" : "requirements.write";
  return heading(title, description, can(writePermission) ? `<button class="button" onclick="newEntity('${entity}')">+ ${action}</button>` : "") +
    `<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2><span class="muted">${records.length} record${records.length === 1 ? "" : "s"}</span></div>${records.length ? `<div class="table-wrap"><table><thead><tr>${columns.map((key) => `<th>${esc(key.replaceAll("_", " "))}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="empty">No ${esc(title.toLowerCase())} in this project yet.</div>`}</section>`;
}

async function render() {
  const sequence = ++renderSequence;
  try {
    if (!state.project && !["projects", "admin"].includes(state.view)) state.view = "projects";
    document.querySelectorAll(".nav").forEach((item) => item.classList.toggle("active", item.dataset.view === state.view));
    let content = "";
    if (state.view === "dashboard") content = await dashboard();
    else if (state.view === "workspace") content = await workspace();
    else if (state.view === "compare") content = await compareView();
    else if (state.view === "repository") content = await repository();
    else if (state.view === "projects") content = projectTable();
    else if (state.view === "plans") content = await plansPage();
    else if (state.view === "suites") content = await suitesPage();
    else if (state.view === "defects") content = await defectsPage(); else if (state.view === "execution") content = await execution();
    else if (state.view === "reports") content = await reportsPage();
    else if (state.view === "audit") content = await auditPage();
    else if (state.view === "admin" && isAdmin()) content = await administration();
    else { state.view = "dashboard"; content = await dashboard(); }
    if (sequence !== renderSequence) return;
    $("#app").innerHTML = content;
    associateFormLabels();
    const importInput = $("#caseImport");
    if (importInput) importInput.onchange = importCases;
    document.querySelectorAll(".tree-node[data-folder]").forEach((item) => {
      item.onclick = async () => { hideFolderMenu(); state.selectedFolder = item.dataset.folder || ""; state.selectedCaseIds.clear(); state.highlightedCaseIds.clear(); state.importFeedback = ""; if (importFeedbackTimer) { clearTimeout(importFeedbackTimer); importFeedbackTimer = null; } await render(); };
      if (!can("repository.write")) return;
      item.oncontextmenu = (event) => {
        event.preventDefault();
        hideFolderMenu();
        showFolderMenu(event.clientX, event.clientY, item.dataset.folder || "", item.dataset.folderName || "");
      };
      if (item.dataset.folder) {
        item.ondragstart = (event) => {
          draggedFolderId = item.dataset.folder;
          event.dataTransfer.setData("text/plain", draggedFolderId);
          event.dataTransfer.effectAllowed = "move";
          item.classList.add("dragging");
        };
        item.ondragend = () => {
          item.classList.remove("dragging");
          document.querySelectorAll(".tree-node.drop-target").forEach((target) => target.classList.remove("drop-target"));
        };
      }
      item.ondragover = (event) => {
        if (!draggedFolderId) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        item.classList.add("drop-target");
      };
      item.ondragleave = () => item.classList.remove("drop-target");
      item.ondrop = async (event) => {
        event.preventDefault();
        item.classList.remove("drop-target");
        const sourceId = event.dataTransfer.getData("text/plain") || draggedFolderId;
        const targetId = item.dataset.folder || "";
        draggedFolderId = "";
        if (!sourceId) return;
        if (sourceId === targetId) { toast("Already in that module"); return; }
        await moveFolder(sourceId, targetId);
      };
    });
    document.querySelectorAll(".tree-toggle[data-toggle-folder]").forEach((item) => {
      item.onclick = async () => {
        const folderId = item.dataset.toggleFolder || "";
        if (state.collapsedFolders.has(folderId)) state.collapsedFolders.delete(folderId);
        else state.collapsedFolders.add(folderId);
        await render();
      };
    });
    bindCaseRows();
    bindRepositoryToolbar();
    bindExecutionView();
    bindWorkspace();
    bindCompare();
    if (state.view === "dashboard") bindDashboard();
    if (state.view === "defects") bindDefects();
    if (state.view === "audit") { bindAudit(); const search = $("#auditSearch"); if (search && state.audit.q) { search.focus(); search.setSelectionRange(search.value.length, search.value.length); } }
    const queueAll = $("#queueAll");
    if (queueAll) queueAll.onchange = () => document.querySelectorAll(".queue-check").forEach((item) => { item.checked = queueAll.checked; });
    document.querySelectorAll(".tree-node[data-cycle], .cycle-item[data-cycle]").forEach((item) => {
      item.onclick = async () => { state.selectedCycle = item.dataset.cycle || ""; state.runSelection.clear(); state.boardSelection.clear(); state.runFilter.module = ""; await render(); };
    });
    document.querySelectorAll(".last-status-select[data-run-status]").forEach((item) => {
      item.onchange = () => updateRunStatus(item);
    });
  } catch (error) {
    if (sequence !== renderSequence) return;
    $("#app").innerHTML = `<section class="panel empty error-state"><h2>Unable to load this view</h2><p>${esc(error.message)}</p><button class="button secondary" onclick="render()">Retry</button></section>`;
  }
}

async function go(view) {
  if (view === "plans" && state.view !== "plans" && !state.keepPlan) state.planId = "";
  state.keepPlan = false;
  if (view !== state.view) {
    state.highlightedCaseIds.clear();
    state.importFeedback = "";
    if (importFeedbackTimer) { clearTimeout(importFeedbackTimer); importFeedbackTimer = null; }
  }
  state.view = view;
  await render();
}

async function toggleRepoSidebar() {
  state.repoSidebarCollapsed = !state.repoSidebarCollapsed;
  await render();
}

async function toggleExecutionSidebar() {
  state.executionSidebarCollapsed = !state.executionSidebarCollapsed;
  await render();
}

function toggleRepoSettings(event) {
  if (event) event.stopPropagation();
  const open = !state.repoSettingsOpen;
  state.repoSettingsOpen = open;
  const menu = $("#repoSettingsMenu");
  const button = document.querySelector(".repo-settings-btn");
  if (menu) menu.hidden = !open;
  if (button) {
    button.classList.toggle("open", open);
    button.setAttribute("aria-expanded", String(open));
  }
}

function collapseRepoSettings() {
  state.repoSettingsOpen = false;
  const menu = $("#repoSettingsMenu");
  const button = document.querySelector(".repo-settings-btn");
  if (menu) menu.hidden = true;
  if (button) {
    button.classList.remove("open");
    button.setAttribute("aria-expanded", "false");
  }
}

function triggerCaseImport() {
  collapseRepoSettings();
  const input = $("#caseImport");
  if (input) input.click();
}

document.addEventListener("click", (event) => {
  if (!state.repoSettingsOpen) return;
  const wrapper = document.querySelector(".repo-settings");
  if (wrapper && !wrapper.contains(event.target)) collapseRepoSettings();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.repoSettingsOpen) collapseRepoSettings();
});

function hideFolderMenu() {
  const menu = $("#folderContextMenu");
  if (menu) menu.hidden = true;
}

function requestDeleteFolder(folderId) {
  const folder = state.folders.find((item) => item.id === folderId);
  if (!folder) return;
  pendingDeleteCases = [];
  pendingDeleteFolder = { id: folderId, name: folder.name };
  $("#confirmTitle").textContent = "Archive module?";
  $("#confirmMessage").textContent = `Archive “${folder.name}”? The module is archived and kept in the workbook history. Only empty modules can be archived.`;
  const progress = $("#deleteProgress");
  if (progress) progress.hidden = true;
  const yes = $("#confirmYes");
  const no = $("#confirmNo");
  if (yes) { yes.disabled = false; yes.textContent = "Yes, archive"; }
  if (no) { no.disabled = false; no.textContent = "No"; }
  $("#confirmModal").hidden = false;
  $("#confirmYes").onclick = confirmDeleteFolder;
  $("#confirmNo").onclick = hideConfirmModal;
  hideFolderMenu();
}

function requestDeleteSelectedCases() {
  const ids = getCheckedCaseIds();
  if (!ids.length) { toast("Select test cases first"); return; }
  requestDeleteCases(ids);
}

function requestDeleteCases(ids) {
  if (!ids.length) return;
  pendingDeleteFolder = { id: "", name: "" };
  pendingDeleteCases = [...ids];
  const names = ids.map((id) => state.cases.find((item) => item.id === id)?.title).filter(Boolean);
  const preview = names.slice(0, 3).map((title) => `“${title}”`).join(", ");
  const extra = names.length > 3 ? ` and ${names.length - 3} more` : "";
  $("#confirmTitle").textContent = ids.length === 1 ? "Archive test case?" : `Archive ${ids.length} test cases?`;
  $("#confirmMessage").textContent = ids.length === 1 ? `Archive ${preview}? It will be hidden from the repository; its history, versions and execution results are kept.` : `Archive ${preview}${extra}? They will be hidden from the repository; their history, versions and execution results are kept.`;
  const progress = $("#deleteProgress");
  if (progress) progress.hidden = true;
  const yes = $("#confirmYes");
  const no = $("#confirmNo");
  if (yes) { yes.disabled = false; yes.textContent = "Yes, archive"; }
  if (no) { no.disabled = false; no.textContent = "No"; }
  $("#confirmModal").hidden = false;
  $("#confirmYes").onclick = confirmDeleteCases;
  $("#confirmNo").onclick = hideConfirmModal;
  collapseRepoSettings();
  hideFolderMenu();
}

function hideConfirmModal() {
  const yes = $("#confirmYes");
  const no = $("#confirmNo");
  if (yes) { yes.disabled = false; yes.textContent = "Yes, archive"; }
  if (no) { no.disabled = false; no.textContent = "No"; }
  const progress = $("#deleteProgress");
  if (progress) progress.hidden = true;
  $("#confirmModal").hidden = true;
  pendingDeleteFolder = { id: "", name: "" };
  pendingDeleteCases = [];
}

function showDeleteProgress(done, total, label) {
  const progress = $("#deleteProgress");
  const fill = $("#deleteProgressFill");
  const text = $("#deleteProgressText");
  if (progress) progress.hidden = false;
  const percent = total ? Math.round((done / total) * 100) : 0;
  if (fill) fill.style.width = `${percent}%`;
  if (text) text.textContent = label || `Deleting… ${done} of ${total}`;
  const yes = $("#confirmYes");
  const no = $("#confirmNo");
  if (yes) { yes.disabled = true; yes.textContent = "Archiving…"; }
  if (no) { no.disabled = true; }
}

async function confirmDeleteCases() {
  const ids = [...pendingDeleteCases];
  if (!ids.length) return hideConfirmModal();
  const total = ids.length;
  showDeleteProgress(0, total, `Archiving ${total} test case${total === 1 ? "" : "s"}…`);
  let deleted = 0;
  let lastError = "";
  const deletedIds = new Set();
  try {
    const result = await api("/test-cases/bulk-delete", { method: "POST", body: JSON.stringify({ ids }) });
    deleted = Number(result.deleted || 0);
    (result.ids || ids.slice(0, deleted)).forEach((id) => deletedIds.add(id));
    showDeleteProgress(deleted, total, `Archiving… ${deleted} of ${total} archived`);
  } catch (error) {
    if (String(error.message).includes("Not found") || String(error.message).includes("404")) {
      for (let index = 0; index < ids.length; index += 5) {
        const chunk = ids.slice(index, index + 5);
        const outcomes = await Promise.allSettled(chunk.map((id) => api(`/test_cases/${encodeURIComponent(id)}`, { method: "DELETE" })));
        outcomes.forEach((outcome, chunkIndex) => {
          if (outcome.status === "fulfilled") { deleted += 1; deletedIds.add(chunk[chunkIndex]); }
          else lastError = outcome.reason?.message || lastError;
        });
        showDeleteProgress(deleted, total, `Archiving… ${deleted} of ${total} archived`);
      }
    } else {
      lastError = error.message;
    }
  }
  if (deletedIds.size) {
    state.cases = state.cases.filter((item) => !deletedIds.has(item.id));
  }
  if (deleted) {
    const text = $("#deleteProgressText");
    if (text) text.textContent = `${deleted} archived. Refreshing table…`;
    await render();
  }
  hideConfirmModal();
  toast(deleted === total ? `${deleted} test case${deleted === 1 ? "" : "s"} archived` : deleted ? `${deleted} of ${total} archived${lastError ? ` — ${lastError}` : ""}` : lastError || "Archive failed");
}

async function confirmDeleteFolder() {
  const { id, name } = pendingDeleteFolder;
  if (!id) return hideConfirmModal();
  showDeleteProgress(0, 1, `Archiving module “${name}”…`);
  try {
    await api(`/folders/${encodeURIComponent(id)}`, { method: "DELETE" });
    showDeleteProgress(1, 1, "Module archived. Refreshing…");
    if (state.selectedFolder === id) state.selectedFolder = "";
    await render();
    hideConfirmModal();
    toast(`Module “${name}” archived`);
  } catch (error) {
    hideConfirmModal();
    toast(error.message);
  }
}

async function moveFolder(folderId, newParentId) {
  try {
    await api(`/folders/${encodeURIComponent(folderId)}/move`, { method: "PUT", body: JSON.stringify({ parent_id: newParentId }) });
    toast("Module moved");
    state.selectedFolder = folderId;
    await render();
  } catch (error) {
    toast(error.message);
  }
}

function openProject(projectId) {
  state.project = state.projects.find((project) => project.id === projectId) || null;
  state.selectedFolder = "";
  state.selectedCycle = "";
  state.collapsedFolders = new Set();
  updateProjectSelect();
  go("dashboard");
}

function newProject() {
  $("#app").innerHTML = heading("New project", "Create a workspace with its own repository and test execution history.") +
    `<section class="panel form-panel"><form id="projectForm"><div class="form-grid">
      <div class="field"><label>Project name *</label><input name="name" required maxlength="120" placeholder="Payments platform"></div>
      <div class="field"><label>Project key *</label><input name="key" required maxlength="12" pattern="[A-Za-z0-9_\\-]+" placeholder="PAY"></div>
      <div class="field"><label>Application</label><input name="application" maxlength="120"></div>
      <div class="field"><label>Product version</label><input name="product_version" maxlength="40"></div>
      <div class="field full"><label>Description</label><textarea name="description" maxlength="1000"></textarea></div>
    </div><div class="form-actions"><button type="button" class="button secondary" onclick="go('projects')">Cancel</button><button class="button">Create project</button></div></form></section>`;
  associateFormLabels();
  $("#projectForm").onsubmit = async (event) => {
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(event.currentTarget));
    try {
      const created = await api("/projects", { method: "POST", body: JSON.stringify({ ...fields, owner_id: state.user.id }) });
      state.projects = await apiAll("/projects");
      state.project = created;
      state.selectedFolder = "";
      updateProjectSelect();
      toast(`Project ${created.key} created`);
      await go("projects");
    } catch (error) { toast(error.message); }
  };
}

function removeCaseStep(button) {
  button.closest(".step-input").remove();
  updateStepNumbers();
}

function exportCases() {
  if (state.project) window.location.href = `/api/export/test-cases.xlsx?project_id=${encodeURIComponent(state.project.id)}`;
}

function downloadTemplate() {
  if (state.project) window.location.href = `/api/test-case-import-template.xlsx?project_id=${encodeURIComponent(state.project.id)}`;
}

async function updateCaseLastStatus(select) {
  const testCaseId = select.dataset.lastStatusCase;
  const previousStatus = select.dataset.previousStatus || "Not Run";
  select.disabled = true;
  try {
    const updated = await api(`/test-cases/${encodeURIComponent(testCaseId)}/last-status`, {
      method: "PUT",
      body: JSON.stringify({ last_status: select.value }),
    });
    select.dataset.previousStatus = updated.last_status;
    select.className = `last-status-select status-${updated.last_status.toLowerCase().replaceAll(" ", "-")}`;
    toast(`Last status updated to ${updated.last_status}`);
  } catch (error) {
    select.value = previousStatus;
    toast(error.message);
  } finally {
    select.disabled = false;
  }
}

async function newEntity(entity) {
  if (entity === "projects") return newProject();
  if (entity === "test_runs") return cycleWizard(state.selectedCycle);

  let title = "New record";
  let description = "";
  let fields = "";
  if (entity === "requirements") {
    title = "New requirement";
    description = "Capture a requirement and trace it to verified test coverage.";
    fields = `<div class="field"><label>Requirement key *</label><input name="key" required placeholder="PAY-101"></div><div class="field"><label>Priority</label><select name="priority"><option>High</option><option selected>Medium</option><option>Low</option></select></div><div class="field full"><label>Title *</label><input name="title" required></div><div class="field"><label>Type</label><select name="type"><option>Functional</option><option>Non-functional</option><option>Business</option></select></div><div class="field"><label>Status</label><select name="status"><option>Draft</option><option>Approved</option><option>Deprecated</option></select></div><div class="field full"><label>Description</label><textarea name="description"></textarea></div>`;
  } else if (entity === "test_plans") {
    title = "New test plan";
    description = "Define the release or regression testing scope.";
    fields = `<div class="field full"><label>Name *</label><input name="name" required placeholder="Release 2.5 regression"></div><div class="field"><label>Release</label><input name="release"></div><div class="field"><label>Build</label><input name="build"></div><div class="field"><label>Start date</label><input name="start_date" type="date"></div><div class="field"><label>End date</label><input name="end_date" type="date"></div><div class="field full"><label>Scope</label><textarea name="scope"></textarea></div>`;
  } else return;

  $("#app").innerHTML = heading(title, description) +
    `<section class="panel form-panel"><form id="entityForm"><div class="form-grid">${fields}</div><div class="form-actions"><button type="button" class="button secondary" onclick="go('${entity === "test_plans" ? "plans" : "requirements"}')">Cancel</button><button class="button">Create</button></div></form></section>`;
  associateFormLabels();
  $("#entityForm").onsubmit = async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    const ownerId = state.user.id;
    try {
      const payload = entity === "requirements"
        ? { ...values, project_id: state.project.id, parent_id: "", version: "1", owner_id: ownerId, source_reference: "" }
        : { ...values, project_id: state.project.id, description: values.scope || "", owner_id: ownerId, status: "Draft" };
      await api(`/${entity}`, { method: "POST", body: JSON.stringify(payload) });
      toast(`${title} created`);
      await go(entity === "test_plans" ? "plans" : "requirements");
    } catch (error) { toast(error.message); }
  };
}

async function updateRunStatus(select) {
  const runId = select.dataset.runStatus;
  const previousStatus = select.dataset.previousStatus || "Not Run";
  select.disabled = true;
  try {
    const updated = await api(`/test-runs/${encodeURIComponent(runId)}/status`, {
      method: "PUT",
      body: JSON.stringify({ status: select.value }),
    });
    select.dataset.previousStatus = updated.status;
    select.className = `last-status-select status-${updated.status.toLowerCase().replaceAll(" ", "-")}`;
    toast(`Status saved · ${updated.status}`);
    if (state.view === "execution" && $(".cycle-overview")) await render();  // keep progress and counts current
  } catch (error) {
    select.value = previousStatus;
    toast(error.message);
  } finally {
    select.disabled = false;
  }
}

async function administration() {
  const users = (await api("/users?archived=true")).items.sort((a, b) => String(a.name).localeCompare(String(b.name)));
  state.users = users.filter((user) => user.status === "Active");
  const rows = users.map((user) => `<tr><td><strong>${esc(user.name)}</strong><small>${esc(user.email)}</small></td><td>${esc(user.role)}</td><td>${status(user.status === "Active" ? "Active" : "Inactive")}</td><td class="row-actions"><button class="link" onclick="editUser(${jsArg(user.id)})">Edit</button><button class="link" onclick="resetUserPassword(${jsArg(user.id)})">Reset password</button></td></tr>`).join("");
  return heading("Administration", "Team members, roles, and Excel storage.", '<button class="button" onclick="editUser(\'\')">+ Add team member</button>') +
    `<section class="panel"><div class="panel-head"><h2>Team members</h2><span class="muted">${users.filter((user) => user.status === "Active").length} active of ${users.length}</span></div>
      <div class="table-wrap"><table><thead><tr><th>Member</th><th>Role</th><th>Status</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="muted panel-note"><strong>Admin</strong> can manage the repository, cycles and users. <strong>Team Member</strong> sees the test cases and records execution results. Permissions are enforced by the server.</p></section>
    <section class="panel admin-storage"><div class="toolbar"><strong>Excel storage</strong><button class="button secondary small" onclick="rebuildIndex()">Rebuild search index</button></div><div class="coverage"><div class="legend"><span>Persistence engine</span><strong>Managed .xlsx workbooks</strong></div><p class="muted">Every change is validated, written atomically, and recorded in the audit history with previous and new values. Workbooks are backed up before bulk changes and at most every 5 minutes otherwise.</p></div></section>`;
}

function editUser(userId) {
  const user = state.users.find((item) => item.id === userId) || null;
  const creating = !userId;
  const roleOptions = ["Team Member", "Admin"].map((role) => `<option ${role === (user?.role || "Team Member") ? "selected" : ""}>${role}</option>`).join("");
  $("#app").innerHTML = heading(creating ? "Add team member" : `Edit ${user?.name || "member"}`, creating ? "The member signs in with this temporary password and must choose their own on first sign-in." : "Deactivated members can no longer sign in; their history is kept.") +
    `<section class="panel form-panel"><form id="userForm"><div class="form-grid">
      <div class="field"><label>Full name *</label><input name="name" required maxlength="120" value="${esc(user?.name || "")}"></div>
      <div class="field"><label>Email *</label><input name="email" type="email" required maxlength="160" value="${esc(user?.email || "")}"></div>
      <div class="field"><label>Role *</label><select name="role">${roleOptions}</select></div>
      ${creating ? '<div class="field"><label>Temporary password * (8+ characters)</label><input name="password" type="password" minlength="8" required autocomplete="new-password"></div>' : `<div class="field"><label>Status</label><select name="status"><option ${user?.status === "Active" ? "selected" : ""}>Active</option><option ${user?.status !== "Active" ? "selected" : ""}>Inactive</option></select></div>`}
    </div><p id="userFormError" class="form-error" role="alert"></p><div class="form-actions"><button type="button" class="button secondary" onclick="go('admin')">Cancel</button><button class="button">${creating ? "Add member" : "Save changes"}</button></div></form></section>`;
  associateFormLabels();
  $("#userForm").onsubmit = async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    try {
      if (creating) await api("/users", { method: "POST", body: JSON.stringify(values) });
      else await api(`/users/${encodeURIComponent(userId)}`, { method: "PUT", body: JSON.stringify(values) });
      toast(creating ? `${values.name} added` : "Member updated");
      await go("admin");
    } catch (error) { $("#userFormError").textContent = error.message; }
  };
}

function resetUserPassword(userId) {
  const user = state.users.find((item) => item.id === userId);
  $("#app").innerHTML = heading(`Reset password for ${user?.name || "member"}`, "They will be signed out and must choose a new password at next sign-in.") +
    `<section class="panel form-panel"><form id="resetForm"><div class="form-grid"><div class="field"><label>Temporary password * (8+ characters)</label><input name="password" type="password" minlength="8" required autocomplete="new-password"></div></div><p id="resetError" class="form-error" role="alert"></p>
    <div class="form-actions"><button type="button" class="button secondary" onclick="go('admin')">Cancel</button><button class="button">Reset password</button></div></form></section>`;
  associateFormLabels();
  $("#resetForm").onsubmit = async (event) => {
    event.preventDefault();
    try {
      await api(`/users/${encodeURIComponent(userId)}/reset-password`, { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))) });
      toast("Password reset");
      await go("admin");
    } catch (error) { $("#resetError").textContent = error.message; }
  };
}

// ---------------------------------------------------------------- repository
const APPROVAL_STATES = ["Draft", "In Review", "Approved", "Rejected", "Deprecated"];
const PRIORITIES = ["Critical", "High", "Medium", "Low"];
const RUN_STATES = ["Not Run", "In Progress", "Passed", "Failed", "Blocked", "Not Applicable"];
const STATUS_LABELS = { "In Review": "Pending Approval" };
const statusLabel = (value) => STATUS_LABELS[value] || value;
const TRANSITION_LABELS = { "In Review": "Submit for approval", Approved: "Approve", Rejected: "Reject", Draft: "Return to draft", Deprecated: "Deprecate" };
const fmtDate = (value) => (value ? new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—");

function folderChildren(folders) {
  const children = new Map();
  const ids = new Set(folders.map((folder) => folder.id));
  for (const folder of folders) {
    // A module whose parent was archived is shown at the top level so it stays reachable.
    const parentId = ids.has(folder.parent_id) ? folder.parent_id : "";
    if (!children.has(parentId)) children.set(parentId, []);
    children.get(parentId).push(folder);
  }
  for (const list of children.values()) list.sort((a, b) => a.name.localeCompare(b.name));
  return children;
}

function folderSubtree(folderId) {
  const children = folderChildren(state.folders);
  const ids = new Set();
  const stack = [folderId];
  while (stack.length) {
    const id = stack.pop();
    if (ids.has(id)) continue;
    ids.add(id);
    (children.get(id) || []).forEach((child) => stack.push(child.id));
  }
  return ids;
}

function moduleChip() {
  return ""; // approvals are switched off: modules have no approval state
}

const ICON_FOLDER = '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M1.5 3.5a1 1 0 0 1 1-1h3.6l1.4 1.6h6a1 1 0 0 1 1 1v7.4a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z" fill="currentColor"/></svg>';
const ICON_CHEVRON = '<svg class="chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M5.5 3.5 10 8l-4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_ALL = '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 3h12v2H2zm0 4h12v2H2zm0 4h12v2H2z" fill="currentColor"/></svg>';
const ICON_CYCLE = '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2a6 6 0 1 1-5.6 3.8l1.8.7A4 4 0 1 0 8 4v2L4.5 3 8 0z" fill="currentColor"/></svg>';

function folderTree(folders, cases) {
  const children = folderChildren(folders);
  const direct = new Map();
  cases.forEach((testCase) => direct.set(testCase.folder_id, (direct.get(testCase.folder_id) || 0) + 1));
  const totals = new Map();
  const total = (id) => {
    if (totals.has(id)) return totals.get(id);
    const value = (direct.get(id) || 0) + (children.get(id) || []).reduce((sum, child) => sum + total(child.id), 0);
    totals.set(id, value);
    return value;
  };
  const editable = can("repository.write");
  const rows = [];
  const walk = (parentId, depth) => (children.get(parentId) || []).forEach((folder) => {
    const kids = children.get(folder.id) || [];
    const collapsed = state.collapsedFolders.has(folder.id);
    const own = direct.get(folder.id) || 0, all = total(folder.id);
    const countText = kids.length && all !== own ? `${own} here · ${all} including submodules` : `${own} test case${own === 1 ? "" : "s"}`;
    const badge = "";
    rows.push(`<div class="tree-branch mtree-row" style="--depth:${Math.min(depth, 10)}">
      ${kids.length ? `<button type="button" class="tree-toggle mtree-toggle" data-toggle-folder="${esc(folder.id)}" aria-label="${collapsed ? "Expand" : "Collapse"} ${esc(folder.name)}" aria-expanded="${!collapsed}">${ICON_CHEVRON}</button>` : '<span class="mtree-toggle" aria-hidden="true"></span>'}
      <button type="button" class="tree-node mtree-node ${state.selectedFolder === folder.id ? "selected" : ""}" data-folder="${esc(folder.id)}" data-folder-name="${esc(folder.name)}" aria-label="${esc(folder.name)}, ${countText}" title="${esc(folder.path || folder.name)} — ${countText}" ${editable ? 'draggable="true"' : ""}>
        <span class="mtree-icon">${ICON_FOLDER}</span><span class="tree-label mtree-label">${esc(folder.name)}</span>${badge}<span class="mtree-count ${own ? "" : "zero"}">${own || (kids.length ? "" : "0")}</span>
      </button></div>`);
    if (kids.length && !collapsed) walk(folder.id, depth + 1);
  });
  const rootCollapsed = state.collapsedFolders.has("");
  if (!rootCollapsed) walk("", 1);
  const hasRootKids = (children.get("") || []).length > 0;
  const root = `<div class="mtree-row root" style="--depth:0">
      ${hasRootKids ? `<button type="button" class="tree-toggle mtree-toggle" data-toggle-folder="" aria-label="${rootCollapsed ? "Expand" : "Collapse"} all modules" aria-expanded="${!rootCollapsed}">${ICON_CHEVRON}</button>` : '<span class="mtree-toggle" aria-hidden="true"></span>'}
      <button type="button" class="tree-node mtree-node ${state.selectedFolder ? "" : "selected"}" data-folder="" data-folder-name="${esc(state.project.name)}" aria-label="All test cases in ${esc(state.project.name)}, ${cases.length}" title="Every test case in ${esc(state.project.name)}">
        <span class="mtree-icon">${ICON_ALL}</span><span class="tree-label mtree-label">All test cases</span><span class="mtree-count">${cases.length}</span></button></div>`;
  const empty = !rows.length && !rootCollapsed ? `<div class="tree-empty">${editable ? "No modules yet. Add one to organize your test cases." : "No modules yet."}</div>` : "";
  return `<div class="mtree" role="tree">${root}${rows.join("")}${empty}</div>`;
}

function filteredCases() {
  const f = state.repoFilters;
  const inFolder = state.selectedFolder ? (f.includeSub ? folderSubtree(state.selectedFolder) : new Set([state.selectedFolder])) : null;
  const needle = f.q.trim().toLowerCase();
  return state.cases.filter((testCase) => {
    if (inFolder && !inFolder.has(testCase.folder_id)) return false;
    if (f.status && (testCase.status || "Draft") !== f.status) return false;
    if (f.priority && testCase.priority !== f.priority) return false;
    if (f.type && testCase.test_type !== f.type) return false;
    if (f.last && (testCase.last_status || "Not Run") !== f.last) return false;
    if (!needle) return true;
    const tags = Array.isArray(testCase.tags) ? testCase.tags.join(" ") : String(testCase.tags || "");
    return `${testCase.case_key} ${testCase.title} ${tags}`.toLowerCase().includes(needle);
  }).sort((a, b) => String(a.case_key || "").localeCompare(String(b.case_key || ""), undefined, { numeric: true }));
}

function filterSelect(id, label, options, value) {
  return `<label class="filter"><span class="visually-hidden">${esc(label)}</span><select id="${id}" aria-label="${esc(label)}"><option value="">${esc(label)}: All</option>${options.map((option) => `<option value="${esc(option)}" ${option === value ? "selected" : ""}>${esc(statusLabel(option))}</option>`).join("")}</select></label>`;
}

function bindRepositoryToolbar() {
  const search = $("#repoSearch");
  if (!search) return;
  let timer = null;
  search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { state.repoFilters.q = search.value; refreshCaseTable(); }, 150); };
  [["repoPriority", "priority"], ["repoType", "type"], ["repoLast", "last"]].forEach(([id, key]) => {
    $(`#${id}`).onchange = (event) => { state.repoFilters[key] = event.target.value; refreshCaseTable(); };
  });
  $("#repoIncludeSub").onchange = (event) => { state.repoFilters.includeSub = event.target.checked; refreshCaseTable(); };
}

function clearRepoFilters() {
  state.repoFilters = { ...state.repoFilters, q: "", status: "", priority: "", type: "", last: "" };
  render();
}

function refreshCaseTable() {
  const body = $("#caseRows");
  if (!body) return;
  const visible = filteredCases();
  body.innerHTML = caseRowsHtml(visible);
  const count = $("#repoCount");
  if (count) count.textContent = String(visible.length);
  bindCaseRows();
}

function bindCaseRows() {
  bindRepositoryExtras();
  document.querySelectorAll(".last-status-select[data-last-status-case]").forEach((item) => { item.onchange = () => updateCaseLastStatus(item); });
  document.querySelectorAll(".last-status-select[data-approval-case]").forEach((item) => { item.onchange = () => updateCaseApprovalStatus(item); });
  document.querySelectorAll(".case-check").forEach((item) => {
    item.onchange = () => {
      if (item.checked) state.selectedCaseIds.add(item.dataset.checkCase); else state.selectedCaseIds.delete(item.dataset.checkCase);
      item.closest("tr")?.classList.toggle("row-selected", item.checked);
      updateSelectedNote();
    };
  });
  const selectAll = $("#selectAllCases");
  if (selectAll) {
    selectAll.onchange = () => {
      const ids = filteredCases().map((testCase) => testCase.id);
      ids.forEach((id) => (selectAll.checked ? state.selectedCaseIds.add(id) : state.selectedCaseIds.delete(id)));
      refreshCaseTable();
    };
  }
  document.querySelectorAll("tr[data-case-row]").forEach((row) => {
    if (!can("repository.write")) return;
    row.oncontextmenu = (event) => {
      event.preventDefault();
      hideFolderMenu();
      showCaseMenu(event.clientX, event.clientY, row.dataset.caseRow);
    };
  });
  updateSelectedNote();
}

function getCheckedCaseIds() {
  return [...state.selectedCaseIds];
}

function updateSelectedNote() {
  const bar = $("#bulkBar");
  const ids = getCheckedCaseIds();
  const selectAll = $("#selectAllCases");
  if (selectAll) {
    const visibleIds = filteredCases().map((testCase) => testCase.id);
    selectAll.checked = visibleIds.length > 0 && visibleIds.every((id) => state.selectedCaseIds.has(id));
  }
  if (!bar) return;
  bar.hidden = ids.length === 0;
  if (!ids.length) { bar.innerHTML = ""; return; }
  bar.innerHTML = `<strong>${ids.length} selected</strong>
    <button class="button secondary small" onclick="bulkAction('move')">Move to module</button>
    <button class="button secondary small" onclick="bulkAction('set_priority')">Set priority</button>
    <button class="button secondary small" onclick="bulkAction('add_tags')">Add tags</button>
    <button class="button secondary small" onclick="bulkAction('remove_tags')">Remove tags</button>
    <button class="button secondary small danger-text" onclick="requestDeleteSelectedCases()">Archive</button>
    <button class="link" onclick="clearSelection()">Clear selection</button>`;
}

function clearSelection() {
  state.selectedCaseIds.clear();
  refreshCaseTable();
}

function folderOptions(selectedId = "", { includeRoot = false } = {}) {
  const sorted = [...state.folders].sort((a, b) => String(a.path).localeCompare(String(b.path)));
  return (includeRoot ? '<option value="">Project root (top-level module)</option>' : "") +
    sorted.map((folder) => `<option value="${esc(folder.id)}" ${folder.id === selectedId ? "selected" : ""}>${esc(folder.path || folder.name)}</option>`).join("");
}

// A small form dialog. Resolves with the submitted values, or null when cancelled.
function openDialog({ title, body, confirmLabel = "Save", danger = false }) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="dialogTitle"><form class="dialog-form"><h2 id="dialogTitle">${esc(title)}</h2>${body}<p class="form-error" role="alert"></p><div class="modal-actions"><button type="button" class="button secondary" data-cancel>Cancel</button><button class="button ${danger ? "danger" : ""}">${esc(confirmLabel)}</button></div></form></div>`;
    document.body.append(overlay);
    associateFormLabels(overlay);
    const form = overlay.querySelector("form");
    const close = (value) => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(value); };
    const onKey = (event) => { if (event.key === "Escape") close(null); };
    document.addEventListener("keydown", onKey);
    overlay.querySelector("[data-cancel]").onclick = () => close(null);
    form.onsubmit = (event) => { event.preventDefault(); close(Object.fromEntries(new FormData(form))); };
    (form.querySelector("input, select, textarea") || form.querySelector("button")).focus();
  });
}

const reasonDialog = (title, confirmLabel) => openDialog({ title, confirmLabel, danger: true, body: '<div class="field"><label>Reason (visible in the approval history) *</label><textarea name="comment" required minlength="3" maxlength="1000"></textarea></div>' });

function showSkipped(result, verb) {
  const skipped = result.skipped || [];
  toast(`${result.updated} test case${result.updated === 1 ? "" : "s"} ${verb}${skipped.length ? ` · ${skipped.length} skipped` : ""}`);
  if (skipped.length) {
    state.importFeedback = `<div class="import-errors"><strong>${skipped.length} test case${skipped.length === 1 ? " was" : "s were"} not changed:</strong><ul>${skipped.map((item) => `<li>${esc(item.case_key || item.id)}: ${esc(item.reason)}</li>`).join("")}</ul></div>`;
  }
}

async function bulkAction(action) {
  const ids = getCheckedCaseIds();
  if (!ids.length) return;
  const params = { action, ids };
  if (action === "reject") {
    const values = await reasonDialog(`Reject ${ids.length} test case${ids.length === 1 ? "" : "s"}`, "Reject");
    if (!values) return;
    params.comment = values.comment;
  } else if (action === "move") {
    const values = await openDialog({ title: `Move ${ids.length} test case${ids.length === 1 ? "" : "s"}`, confirmLabel: "Move", body: `<div class="field"><label>Destination module</label><select name="folder_id">${folderOptions(state.selectedFolder)}</select></div><p class="muted">Moving keeps the version and approval status.</p>` });
    if (!values) return;
    params.folder_id = values.folder_id;
  } else if (action === "set_priority") {
    const values = await openDialog({ title: "Set priority", confirmLabel: "Apply", body: `<div class="field"><label>Priority</label><select name="priority">${PRIORITIES.map((item) => `<option>${item}</option>`).join("")}</select></div>` });
    if (!values) return;
    params.priority = values.priority;
  } else if (action === "add_tags" || action === "remove_tags") {
    const values = await openDialog({ title: action === "add_tags" ? "Add tags" : "Remove tags", confirmLabel: "Apply", body: '<div class="field"><label>Tags, separated by commas</label><input name="tags" required placeholder="regression, smoke"></div>' });
    if (!values) return;
    params.tags = values.tags;
  } else if (action === "deprecate") {
    const values = await openDialog({ title: `Deprecate ${ids.length} test case${ids.length === 1 ? "" : "s"}?`, confirmLabel: "Deprecate", danger: true, body: '<p>Deprecated test cases stay in the repository and history but cannot be added to new cycles.</p><div class="field"><label>Note (optional)</label><input name="comment" maxlength="500"></div>' });
    if (!values) return;
    params.comment = values.comment;
  }
  try {
    const result = await api("/test-cases/bulk", { method: "POST", body: JSON.stringify(params) });
    state.importFeedback = "";
    showSkipped(result, { submit: "submitted", approve: "approved", reject: "rejected", move: "moved", set_priority: "updated", add_tags: "updated", remove_tags: "updated", deprecate: "deprecated" }[action] || "updated");
    if (!(result.skipped || []).length) state.selectedCaseIds.clear();
    await render();
  } catch (error) { toast(error.message); }
}

function showFolderMenu(x, y, folderId) {
  const menu = $("#folderContextMenu");
  if (!menu) return;
  const folder = state.folders.find((item) => item.id === folderId);
  const items = [["new-sub", folder ? "New submodule" : "New module"], ["new-case", "New test case"]];
  if (folder) {
    items.push(["rename", "Rename module"]);
    items.push(["archive", "Archive module"]);
  }
  menu.innerHTML = items.map(([action, text]) => `<button type="button" class="folder-menu-item ${action === "archive" || action === "reject" ? "danger" : ""}" data-action="${action}">${esc(text)}</button>`).join("");
  menu.hidden = false;
  menu.style.left = `${Math.min(x, window.innerWidth - 230)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - 40 * items.length - 20)}px`;
  menu.querySelectorAll(".folder-menu-item").forEach((item) => {
    item.onclick = (event) => {
      event.stopPropagation();
      hideFolderMenu();
      const action = item.dataset.action;
      if (action === "new-sub") newFolder(folderId);
      else if (action === "new-case") { state.selectedFolder = folderId; newCase(); }
      else if (action === "rename") renameFolder(folderId);
      else if (action === "archive") requestDeleteFolder(folderId);
    };
  });
}

async function folderTransition(folderId, target) {
  const folder = state.folders.find((item) => item.id === folderId);
  let comment = "";
  if (target === "Rejected") {
    const values = await reasonDialog(`Reject module “${folder?.name || ""}”`, "Reject module");
    if (!values) return;
    comment = values.comment;
  }
  try {
    await api(`/folders/${encodeURIComponent(folderId)}/transition`, { method: "POST", body: JSON.stringify({ status: target, comment }) });
    toast(`Module ${statusLabel(target).toLowerCase()}`);
    await render();
  } catch (error) { toast(error.message); }
}

async function renameFolder(folderId) {
  const folder = state.folders.find((item) => item.id === folderId);
  if (!folder) return;
  const values = await openDialog({ title: "Rename module", confirmLabel: "Rename", body: `<div class="field"><label>Module name</label><input name="name" required maxlength="120" value="${esc(folder.name)}"></div>` });
  if (!values) return;
  try {
    await api(`/folders/${encodeURIComponent(folderId)}/rename`, { method: "PUT", body: JSON.stringify({ name: values.name }) });
    toast("Module renamed");
    await render();
  } catch (error) { toast(error.message); }
}

function showCaseMenu(x, y, caseId) {
  const menu = $("#folderContextMenu");
  if (!menu) return;
  if (!state.selectedCaseIds.has(caseId)) { state.selectedCaseIds = new Set([caseId]); refreshCaseTable(); }
  const count = state.selectedCaseIds.size;
  menu.innerHTML = `<button type="button" class="folder-menu-item" data-action="open">Open</button><button type="button" class="folder-menu-item" data-action="edit">Edit</button><button type="button" class="folder-menu-item danger" data-action="archive">Archive ${count === 1 ? "test case" : `${count} selected`}</button>`;
  menu.hidden = false;
  menu.style.left = `${Math.min(x, window.innerWidth - 210)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - 130)}px`;
  menu.querySelectorAll(".folder-menu-item").forEach((item) => {
    item.onclick = (event) => {
      event.stopPropagation();
      hideFolderMenu();
      if (item.dataset.action === "open") viewCase(caseId);
      else if (item.dataset.action === "edit") editCase(caseId);
      else requestDeleteCases(getCheckedCaseIds());
    };
  });
}

function newFolder(parentId = state.selectedFolder || "") {
  if (!state.project) return;
  openDialog({ title: parentId ? "New submodule" : "New module", confirmLabel: "Create", body: `<div class="field"><label>Name *</label><input name="name" required maxlength="120" placeholder="e.g. Enrollment"></div><div class="field"><label>Inside</label><select name="parent_id">${folderOptions(parentId, { includeRoot: true })}</select></div><p class="muted">New modules start as Draft. Team members see a module's test cases once the module is approved.</p>` })
    .then(async (values) => {
      if (!values) return;
      try {
        const created = await api("/folders", { method: "POST", body: JSON.stringify({ project_id: state.project.id, parent_id: values.parent_id, name: values.name }) });
        state.selectedFolder = created.id;
        if (values.parent_id) state.collapsedFolders.delete(values.parent_id);
        toast(`Module “${created.name}” created as Draft`);
        await go("repository");
      } catch (error) { toast(error.message); }
    });
}

async function updateCaseApprovalStatus(select) {
  const testCaseId = select.dataset.approvalCase;
  const previousStatus = select.dataset.previousApproval || "Draft";
  const target = select.value;
  let comment = "";
  if (target === "Rejected") {
    const values = await reasonDialog("Reject test case", "Reject");
    if (!values) { select.value = previousStatus; return; }
    comment = values.comment;
  }
  select.disabled = true;
  try {
    const updated = await api(`/test-cases/${encodeURIComponent(testCaseId)}/transition`, { method: "POST", body: JSON.stringify({ status: target, comment }) });
    select.dataset.previousApproval = updated.status;
    select.className = `last-status-select approval-select status-${updated.status.toLowerCase().replaceAll(" ", "-")}`;
    const local = state.cases.find((item) => item.id === testCaseId);
    if (local) Object.assign(local, updated);
    toast(`${caseLabel(updated)} is now ${statusLabel(updated.status)}`);
  } catch (error) {
    select.value = previousStatus;
    toast(error.message);
  } finally {
    select.disabled = false;
  }
}

// ------------------------------------------------------- test case form
function stepRow(step = {}, index = 1) {
  return `<div class="step-input"><span class="step-number">${index}</span><div class="step-fields"><label>Action<input name="action" required placeholder="Action performed by the tester" value="${esc(step.action || "")}"></label><label>Expected result<input name="expected_result" required placeholder="Expected outcome" value="${esc(step.expected_result || "")}"></label><label>Test data<input name="test_data" placeholder="Optional test data" value="${esc(step.test_data || "")}"></label></div><button type="button" class="remove-step" onclick="removeCaseStep(this)" aria-label="Remove step">×</button></div>`;
}

async function caseForm(existing = null, steps = []) {
  if (!state.folders.length) state.folders = await apiAll(`/folders?project_id=${encodeURIComponent(state.project.id)}`);
  if (!state.folders.length) {
    $("#app").innerHTML = heading("Create a module first", "Every test case belongs to a module.", '<button class="button" onclick="newFolder()">+ Create module</button>') +
      '<section class="panel"><div class="empty">Create a module, then add your test case or import the Excel template.</div></section>';
    return;
  }
  const editing = Boolean(existing);
  const automation = existing?.automation_status || "Not Automated";
  const value = (key) => esc(existing?.[key] ?? "");
  const types = [...new Set(["Manual", "Automated", existing?.test_type].filter(Boolean))];
  const tags = Array.isArray(existing?.tags) ? existing.tags.join(", ") : existing?.tags || "";
  const approvedNote = editing ? `<div class="notice">Saving content changes creates version ${Number(existing.version) + 1}. Cycles that already use version ${esc(existing.version)} are not affected.</div>` : "";
  $("#app").innerHTML = heading(editing ? `Edit ${caseLabel(existing)}` : "New test case", editing ? existing.title : "Create a structured, versioned test case.", `<button class="button secondary" onclick="${editing ? `viewCase(${jsArg(existing.id)})` : "go('repository')"}">Cancel</button>`) +
    `<section class="panel form-panel">${approvedNote}<form id="caseForm"><div class="form-grid">
      <div class="field full"><label>Title *</label><input name="title" required maxlength="200" value="${value("title")}" placeholder="Describe the behavior being verified"></div>
      <div class="field"><label>Module *</label><select name="folder_id" required>${folderOptions(existing?.folder_id || state.selectedFolder || "")}</select></div>
      <div class="field"><label>Priority *</label><select name="priority">${PRIORITIES.map((item) => `<option ${item === (existing?.priority || "Medium") ? "selected" : ""}>${item}</option>`).join("")}</select></div>
      <div class="field"><label>Test type</label><select name="test_type">${types.map((item) => `<option ${item === (existing?.test_type || "Manual") ? "selected" : ""}>${esc(item)}</option>`).join("")}</select></div>
      <div class="field"><label>Tags</label><input name="tags" value="${esc(tags)}" placeholder="smoke, regression"></div>
      <div class="field"><label>Automation status</label><select name="automation_status">${["Not Automated", "In Progress", "Automated", "Not Applicable"].map((item) => `<option ${item === automation ? "selected" : ""}>${item}</option>`).join("")}</select></div>
      <div class="field"><label>Automation reference</label><input name="automation_reference" maxlength="300" value="${value("automation_reference")}" placeholder="e.g. tests/login.spec.ts"></div>
      <div class="field full"><label>Objective</label><textarea name="objective" placeholder="What does this test demonstrate?">${value("objective")}</textarea></div>
      <div class="field full"><label>Description</label><textarea name="description" placeholder="Additional context for testers">${value("description")}</textarea></div>
      <div class="field full"><label>Preconditions</label><textarea name="preconditions" placeholder="Required setup before execution">${value("preconditions")}</textarea></div>
      <div class="field full"><label>Expected result</label><textarea name="expected_result" placeholder="What should happen when this scenario is successful?">${value("expected_result")}</textarea></div>
      <div id="caseSteps" class="field full step-editor"><div class="step-editor-heading"><label>Test steps${editing && !steps.length ? "" : " *"}</label><button type="button" class="button secondary small" onclick="addCaseStep()">+ Add step</button></div>
        ${(steps.length ? steps : editing ? [] : [{}]).map((step, index) => stepRow(step, index + 1)).join("")}
      </div>
      ${editing ? '<div class="field full"><label>Change note (saved in version history)</label><input name="version_comment" maxlength="300" placeholder="What changed and why?"></div>' : ""}
    </div><p id="caseFormError" class="form-error" role="alert"></p><div class="form-actions"><button type="button" class="button secondary" onclick="${editing ? `viewCase(${jsArg(existing.id)})` : "go('repository')"}">Cancel</button><button class="button">${editing ? "Save changes" : "Create test case"}</button></div></form></section>`;
  associateFormLabels();
  updateStepNumbers();
  $("#caseForm").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = Object.fromEntries(new FormData(form));
    const formSteps = [...form.querySelectorAll(".step-input")].map((step) => ({
      action: step.querySelector('[name="action"]').value.trim(),
      expected_result: step.querySelector('[name="expected_result"]').value.trim(),
      test_data: step.querySelector('[name="test_data"]').value.trim(),
    }));
    const payload = {
      title: fields.title, folder_id: fields.folder_id, priority: fields.priority, test_type: fields.test_type,
      tags: fields.tags.split(",").map((tag) => tag.trim()).filter(Boolean),
      objective: fields.objective, description: fields.description, preconditions: fields.preconditions,
      automation_status: fields.automation_status, automation_reference: fields.automation_reference,
      // Step inputs share the name "expected_result", so read the case-level field directly.
      expected_result: form.querySelector('textarea[name="expected_result"]').value,
      steps: formSteps,
    };
    if (editing) payload.version_comment = fields.version_comment || "Updated";
    else payload.project_id = state.project.id;
    const submit = async (allowDuplicate) => {
      const path = editing ? `/test_cases/${encodeURIComponent(existing.id)}` : "/test-cases";
      return api(path, { method: editing ? "PUT" : "POST", body: JSON.stringify({ ...payload, allow_duplicate: allowDuplicate }) });
    };
    try {
      let saved;
      try {
        saved = await submit(false);
      } catch (error) {
        if (error.code !== "duplicate") throw error;
        const list = (error.duplicates || []).map((item) => `<li><strong>${esc(item.case_key)}</strong> ${esc(item.title)} <span class="muted">${esc(item.folder_path)}</span></li>`).join("");
        const confirmDup = await openDialog({ title: "Possible duplicate", confirmLabel: "Save anyway", body: `<p>This module already has a test case with the same title:</p><ul class="dup-list">${list}</ul><p class="muted">Save anyway only if this is intentionally a separate test case.</p>` });
        if (!confirmDup) return;
        saved = await submit(true);
      }
      toast(editing ? (saved.version !== existing.version ? `Saved as version ${saved.version}` : "No changes to save") : `${caseLabel(saved)} created`);
      await viewCase(saved.id);
    } catch (error) { $("#caseFormError").textContent = error.message; }
  };
}

function newCase() { return caseForm(null, []); }

async function editCase(id) {
  const details = await api(`/test-cases/${encodeURIComponent(id)}/details`);
  return caseForm(details.case, details.steps);
}

function addCaseStep() {
  const container = $("#caseSteps");
  const index = container.querySelectorAll(".step-input").length + 1;
  container.insertAdjacentHTML("beforeend", stepRow({}, index));
  updateStepNumbers();
}

function updateStepNumbers() {
  document.querySelectorAll("#caseSteps .step-input").forEach((step, index) => {
    step.querySelector(".step-number").textContent = String(index + 1);
  });
}

// ------------------------------------------------------- test case detail
async function viewCase(id, tab = "details") {
  let details;
  try { details = await api(`/test-cases/${encodeURIComponent(id)}/details`); }
  catch (error) { toast(error.message); return; }
  const testCase = details.case;
  const canEdit = can("repository.write") && testCase.archived !== true;
  // Approvals are switched off: the only lifecycle state shown is archived / deprecated.
  const transitionButtons = "";
  let banner = "";
  if (testCase.archived === true) banner = '<div class="notice warn">This test case is archived.</div>';
  else if (testCase.status === "Deprecated") banner = '<div class="notice warn">This test case is deprecated and is not added to new cycles.</div>';
  const tags = Array.isArray(testCase.tags) ? testCase.tags.join(", ") : testCase.tags || "";
  const tabs = [["details", "Details"], ["versions", `Version history (${details.versions.length})`], ["files", "Attachments"]];
  const stepRows = details.steps.map((step) => `<tr><td>${esc(step.step_number)}</td><td>${esc(step.action)}</td><td>${esc(step.expected_result)}</td><td>${esc(step.test_data || "—")}</td></tr>`).join("");
  const section = (title, text, fallback) => `<div class="detail-section"><h2>${title}</h2><p class="prewrap">${esc(text || fallback)}</p></div>`;
  let body = "";
  if (tab === "details") {
    body = `<div class="detail-grid">
        <div><span class="detail-label">Last status</span>${status(testCase.last_status || "Not Run")}</div>
        <div><span class="detail-label">Priority</span><strong>${esc(testCase.priority || "—")}</strong></div>
        <div><span class="detail-label">Type</span><strong>${esc(testCase.test_type || "—")}</strong></div>
        <div><span class="detail-label">Module</span><strong>${esc(details.folder_path || "—")}</strong></div>
        <div><span class="detail-label">Tags</span><strong>${esc(tags || "—")}</strong></div>
        <div><span class="detail-label">Automation</span><strong>${esc(testCase.automation_status || "Not Automated")}</strong>${testCase.automation_reference ? `<small>${esc(testCase.automation_reference)}</small>` : ""}</div>
        <div><span class="detail-label">Created</span><strong>${esc(details.created_by_name)}</strong><small>${esc(fmtDate(testCase.created_at))}</small></div>
        <div><span class="detail-label">Last updated</span><strong>${esc(details.updated_by_name)}</strong><small>${esc(fmtDate(testCase.updated_at))}</small></div>
      </div>
      ${section("Objective", testCase.objective, "No objective provided.")}
      ${section("Description", testCase.description, "No description provided.")}
      ${section("Preconditions", testCase.preconditions, "No preconditions provided.")}
      ${section("Expected result", testCase.expected_result, "No scenario-level expected result provided.")}
      <div class="detail-section"><h2>Test steps</h2><div class="table-wrap"><table><thead><tr><th>#</th><th>Action</th><th>Expected result</th><th>Test data</th></tr></thead><tbody>${stepRows || '<tr><td colspan="4" class="muted">This version has no steps.</td></tr>'}</tbody></table></div></div>`;
  } else if (tab === "versions") {
    body = `<div class="table-wrap"><table><thead><tr><th>Version</th><th>Saved</th><th>By</th><th>Change note</th><th></th></tr></thead><tbody>${details.versions.map((version) => `<tr><td><strong>v${esc(version.version)}</strong>${String(version.version) === String(testCase.version) ? ' <span class="badge approved">Current</span>' : ""}</td><td>${esc(fmtDate(version.created_at))}</td><td>${esc(version.changed_by_name)}</td><td>${esc(version.comment || "—")}</td><td><button class="link" onclick="showVersion(${jsArg(testCase.id)}, ${jsArg(version.version)})">View</button></td></tr>`).join("")}</tbody></table></div><div id="versionDetail"></div>`;
  } else {
    body = '<div id="caseFiles"><p class="muted">Loading attachments…</p></div>';
  }
  $("#app").innerHTML = heading(testCase.title, `${caseLabel(testCase)} · version ${testCase.version} · ${details.folder_path || "No module"}`,
    `<div class="heading-actions"><button class="button secondary" onclick="go('repository')">Back</button>${canEdit ? `<button class="button secondary" onclick="editCase(${jsArg(testCase.id)})">Edit</button>` : ""}${testCase.archived === true ? "" : transitionButtons}</div>`) +
    `${banner}<section class="panel detail-panel"><div class="tabs" role="tablist">${tabs.map(([key, text]) => `<button role="tab" aria-selected="${key === tab}" class="tab ${key === tab ? "active" : ""}" onclick="viewCase(${jsArg(testCase.id)}, '${key}')">${esc(text)}</button>`).join("")}</div>${body}</section>`;
  if (tab !== "details" && tab !== "versions") caseAttachmentsTab(testCase.id);
}

async function showVersion(caseId, version) {
  const target = $("#versionDetail");
  try {
    const detail = await api(`/test-cases/${encodeURIComponent(caseId)}/versions/${encodeURIComponent(version)}`);
    const changes = detail.previous_version
      ? (detail.changes.length || detail.steps_changed
        ? `<table><thead><tr><th>Field</th><th>v${esc(detail.previous_version)}</th><th>v${esc(detail.version)}</th></tr></thead><tbody>${detail.changes.map((change) => `<tr><td>${esc(change.field)}</td><td class="prewrap diff-old">${esc(change.before || "—")}</td><td class="prewrap diff-new">${esc(change.after || "—")}</td></tr>`).join("")}${detail.steps_changed ? `<tr><td>Test steps</td><td colspan="2">Steps changed (shown below)</td></tr>` : ""}</tbody></table>`
        : '<p class="muted">No content changes from the previous version.</p>')
      : '<p class="muted">First version.</p>';
    target.innerHTML = `<div class="version-detail"><h2>Version ${esc(detail.version)}${detail.comment ? ` · ${esc(detail.comment)}` : ""}</h2><h3>Changes</h3><div class="table-wrap">${changes}</div><h3>Title</h3><p>${esc(detail.snapshot.title || "")}</p><h3>Steps in this version</h3><div class="table-wrap"><table><thead><tr><th>#</th><th>Action</th><th>Expected result</th><th>Test data</th></tr></thead><tbody>${detail.steps.map((step) => `<tr><td>${esc(step.step_number)}</td><td>${esc(step.action)}</td><td>${esc(step.expected_result)}</td><td>${esc(step.test_data || "—")}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">No steps.</td></tr>'}</tbody></table></div></div>`;
    target.scrollIntoView({ block: "nearest" });
  } catch (error) { toast(error.message); }
}

async function caseTransition(caseId, target) {
  let comment = "";
  if (target === "Rejected") {
    const values = await reasonDialog("Reject test case", "Reject");
    if (!values) return;
    comment = values.comment;
  } else if (target === "Deprecated") {
    const values = await openDialog({ title: "Deprecate test case?", confirmLabel: "Deprecate", danger: true, body: '<p>It stays in the repository and history but cannot be added to new cycles.</p><div class="field"><label>Note (optional)</label><input name="comment" maxlength="500"></div>' });
    if (!values) return;
    comment = values.comment;
  }
  try {
    const updated = await api(`/test-cases/${encodeURIComponent(caseId)}/transition`, { method: "POST", body: JSON.stringify({ status: target, comment }) });
    toast(`${caseLabel(updated)} is now ${statusLabel(updated.status)}`);
    await viewCase(caseId);
  } catch (error) { toast(error.message); }
}

// ------------------------------------------------------------ approvals page
async function approvalsPage() {
  const queue = await api(`/approvals/queue?project_id=${encodeURIComponent(state.project.id)}`);
  const caseRows = queue.cases.map((testCase) => `<tr><td><input type="checkbox" class="queue-check" value="${esc(testCase.id)}" aria-label="Select ${esc(testCase.title)}"></td><td><button class="link case-key" onclick="viewCase(${jsArg(testCase.id)})">${esc(caseLabel(testCase))}</button></td><td>${esc(testCase.title)}<small class="muted block">${esc(testCase.folder_path)}</small></td><td>v${esc(testCase.version)}</td><td>${esc(testCase.submitted_by)}<small class="muted block">${esc(fmtDate(testCase.submitted_at))}</small></td><td class="row-actions"><button class="link" onclick="queueDecision([${jsArg(testCase.id)}], 'approve')">Approve</button><button class="link danger-text" onclick="queueDecision([${jsArg(testCase.id)}], 'reject')">Reject</button></td></tr>`).join("");
  const folderRows = queue.folders.map((folder) => `<tr><td><strong>${esc(folder.path)}</strong></td><td>${status(folder.approval_status)}</td><td>${esc(folder.case_count)}</td><td class="row-actions">${folder.approval_status !== "Approved" ? `<button class="link" onclick="queueFolder(${jsArg(folder.id)}, 'Approved')">Approve</button>` : ""}${["In Review", "Draft"].includes(folder.approval_status) ? `<button class="link danger-text" onclick="queueFolder(${jsArg(folder.id)}, 'Rejected')">Reject</button>` : ""}</td></tr>`).join("");
  const recent = queue.recent.map((row) => `<tr><td>${esc(fmtDate(row.created_at))}</td><td>${status(row.decision)}</td><td>${row.test_case_id ? `<button class="link case-key" onclick="viewCase(${jsArg(row.test_case_id)})">${esc(row.case_key || "Test case")}</button>` : `Module ${esc(row.folder_path)}`}</td><td>${esc(row.reviewer_name)}</td><td class="prewrap">${esc(row.comment || "—")}</td></tr>`).join("");
  return heading("Approvals", "Review test cases and modules before they reach the QA team.") +
    `<section class="panel"><div class="panel-head"><h2>Test cases pending approval</h2><div class="heading-actions"><span class="muted">${queue.cases.length}</span>${queue.cases.length ? '<button class="button small" onclick="queueDecision(null, \'approve\')">Approve selected</button><button class="button secondary small danger-text" onclick="queueDecision(null, \'reject\')">Reject selected</button>' : ""}</div></div>
      ${queue.cases.length ? `<div class="table-wrap"><table><thead><tr><th class="check-col"><input type="checkbox" id="queueAll" aria-label="Select all pending test cases"></th><th>ID</th><th>Test case</th><th>Version</th><th>Submitted</th><th></th></tr></thead><tbody>${caseRows}</tbody></table></div>` : '<div class="empty">Nothing is waiting for approval. Submitted test cases appear here.</div>'}</section>
    <section class="panel section-gap"><div class="panel-head"><h2>Modules not yet approved</h2><span class="muted">${queue.folders.length}</span></div>
      ${queue.folders.length ? `<div class="table-wrap"><table><thead><tr><th>Module</th><th>Status</th><th>Test cases</th><th></th></tr></thead><tbody>${folderRows}</tbody></table></div>` : '<div class="empty">All modules are approved.</div>'}</section>
    <section class="panel section-gap"><div class="panel-head"><h2>Recent decisions</h2></div>
      ${queue.recent.length ? `<div class="table-wrap"><table><thead><tr><th>When</th><th>Decision</th><th>Item</th><th>By</th><th>Comment</th></tr></thead><tbody>${recent}</tbody></table></div>` : '<div class="empty">No approval activity yet.</div>'}</section>`;
}

async function queueDecision(ids, action) {
  const selected = ids || [...document.querySelectorAll(".queue-check:checked")].map((item) => item.value);
  if (!selected.length) { toast("Select test cases first"); return; }
  const params = { action, ids: selected };
  if (action === "reject") {
    const values = await reasonDialog(`Reject ${selected.length} test case${selected.length === 1 ? "" : "s"}`, "Reject");
    if (!values) return;
    params.comment = values.comment;
  }
  try {
    const result = await api("/test-cases/bulk", { method: "POST", body: JSON.stringify(params) });
    showSkipped(result, action === "approve" ? "approved" : "rejected");
    await render();
  } catch (error) { toast(error.message); }
}

async function queueFolder(folderId, target) {
  let comment = "";
  if (target === "Rejected") {
    const values = await reasonDialog("Reject module", "Reject module");
    if (!values) return;
    comment = values.comment;
  }
  try {
    await api(`/folders/${encodeURIComponent(folderId)}/transition`, { method: "POST", body: JSON.stringify({ status: target, comment }) });
    toast(`Module ${statusLabel(target).toLowerCase()}`);
    await render();
  } catch (error) { toast(error.message); }
}

// ------------------------------------------------------------------ test cycles
const TESTING_TYPES = ["Regression", "Sanity", "Smoke", "Functional", "Retesting", "Integration", "UAT", "Custom"];
let wiz = null;

function progressBar(percent, label) {
  return `<div class="progress" role="progressbar" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100" aria-label="${esc(label)}"><span style="width:${Math.min(100, percent)}%"></span></div>`;
}

async function setCycleStatus(cycleId, select) {
  try {
    await api(`/test_cycles/${encodeURIComponent(cycleId)}`, { method: "PUT", body: JSON.stringify({ status: select.value }) });
    toast(`Cycle marked ${select.value}`);
    await render();
  } catch (error) { toast(error.message); await render(); }
}

async function removeRun(runId, key) {
  const confirmed = await openDialog({ title: `Remove ${key} from this cycle?`, confirmLabel: "Remove", danger: true, body: "<p>Only test cases that have not been executed can be removed. You can add it back later.</p>" });
  if (!confirmed) return;
  try {
    await api(`/test_runs/${encodeURIComponent(runId)}`, { method: "DELETE" });
    toast(`${key} removed from the cycle`);
    await render();
  } catch (error) { toast(error.message); }
}

function cycleInfoFields(values = {}, plans = []) {
  const type = TESTING_TYPES.includes(values.testing_type) || !values.testing_type ? values.testing_type || "Regression" : "Custom";
  const custom = type === "Custom" ? (values.custom_type || (TESTING_TYPES.includes(values.testing_type) ? "" : values.testing_type || "")) : "";
  return `<div class="form-grid">
    <div class="field full"><label>Cycle name *</label><input name="name" required maxlength="150" value="${esc(values.name || "")}" placeholder="e.g. Regression Cycle 01"></div>
    <div class="field"><label>Testing type *</label><select name="testing_type" onchange="document.getElementById('customTypeField').hidden = this.value !== 'Custom'">${TESTING_TYPES.map((item) => `<option ${item === type ? "selected" : ""}>${item}</option>`).join("")}</select></div>
    <div class="field" id="customTypeField" ${type === "Custom" ? "" : "hidden"}><label>Custom type name *</label><input name="custom_type" maxlength="60" value="${esc(custom)}" placeholder="e.g. Accessibility"></div>
    <div class="field"><label>Project</label><input value="${esc(state.project.key)} · ${esc(state.project.name)}" disabled></div>
    <div class="field"><label>Environment</label><input name="environment" maxlength="80" value="${esc(values.environment || "")}" placeholder="e.g. QA, Staging"></div>
    <div class="field"><label>Build / version</label><input name="build" maxlength="80" value="${esc(values.build || "")}" placeholder="e.g. 2.5.0"></div>
    <div class="field"><label>Start date</label><input name="start_date" type="date" value="${esc(values.start_date || "")}"></div>
    <div class="field"><label>Target completion date</label><input name="target_date" type="date" value="${esc(values.target_date || values.end_date || "")}"></div>
    ${plans.length ? `<div class="field"><label>Test plan (release / sprint)</label><select name="plan_id"><option value="">No plan</option>${plans.map((plan) => `<option value="${esc(plan.id)}" ${plan.id === values.plan_id ? "selected" : ""}>${esc(plan.name)}</option>`).join("")}</select></div>` : ""}
    <div class="field full"><label>Description</label><textarea name="description" maxlength="2000" placeholder="Scope, goals, or notes for the team">${esc(values.description || "")}</textarea></div>
  </div>`;
}

async function editCycle(cycleId) {
  const [cycle, plans] = await Promise.all([api(`/test_cycles/${encodeURIComponent(cycleId)}`), apiAll(`/test_plans?project_id=${encodeURIComponent(state.project.id)}`)]);
  const values = await openDialog({ title: "Edit cycle details", confirmLabel: "Save", body: cycleInfoFields(cycle, plans) });
  if (!values) return;
  try {
    await api(`/test_cycles/${encodeURIComponent(cycleId)}`, { method: "PUT", body: JSON.stringify(values) });
    toast("Cycle updated");
    await render();
  } catch (error) { toast(error.message); }
}

// ---------------------------------------------------------- create-cycle wizard
async function cycleWizard(cycleId = "", options = {}) {
  const cycle = cycleId ? await api(`/test_cycles/${encodeURIComponent(cycleId)}`) : null;
  const [plans, suites] = await Promise.all([cycle ? [] : apiAll(`/test_plans?project_id=${encodeURIComponent(state.project.id)}`), api(`/suites?project_id=${encodeURIComponent(state.project.id)}`).then((r) => r.items).catch(() => [])]);
  const suite = options.suite || null;
  wiz = { mode: "cycle", cycle, plans, suites, step: cycle ? 2 : 1, info: { plan_id: options.planId || "" }, modules: new Set(suite?.module_ids || []), include: new Set(suite?.case_ids || []), exclude: new Set(), includeUnapproved: false,
    data: null, focus: "", moduleQuery: "", caseQuery: "", collapsed: new Set(), preview: null, suiteRefs: suite ? [suite.id] : [] };
  renderWizard();
}

function wizardSteps() {
  if (wiz.mode === "suite") return "";
  const steps = wiz.cycle ? [[2, "Select test cases"], [3, "Preview"], [4, "Confirm"]] : [[1, "Cycle details"], [2, "Select test cases"], [3, "Preview"], [4, "Confirm"]];
  return `<ol class="stepper">${steps.map(([number, label], index) => `<li class="${number === wiz.step ? "current" : number < wiz.step ? "done" : ""}" ${number === wiz.step ? 'aria-current="step"' : ""}><span>${index + 1}</span>${esc(label)}</li>`).join("")}</ol>`;
}

async function renderWizard() {
  const suiteMode = wiz.mode === "suite";
  const title = suiteMode ? (wiz.suite ? `Edit suite “${wiz.suite.name}”` : "New test suite") : wiz.cycle ? `Add test cases to ${wiz.cycle.name}` : "Create test cycle";
  const cancel = `<button class="button secondary" onclick="wiz = null; go('${suiteMode ? "suites" : "execution"}')">Cancel</button>`;
  let body = "";
  if (wiz.step === 1) {
    body = `<form id="wizInfo" class="panel form-panel">${cycleInfoFields(wiz.info, wiz.plans)}<p id="wizError" class="form-error" role="alert"></p><div class="form-actions">${cancel}<button class="button">Next: select test cases</button></div></form>`;
  } else if (wiz.step === 2) {
    if (!wiz.data) {
      $("#app").innerHTML = heading(title, "Loading the repository…") + wizardSteps() + '<section class="panel"><div class="empty"><span class="spinner" aria-hidden="true"></span> Loading modules and test cases…</div></section>';
      try { wiz.data = await api(`/cycles/candidates?project_id=${encodeURIComponent(state.project.id)}${wiz.includeUnapproved ? "&include_unapproved=true" : ""}`); }
      catch (error) { toast(error.message); return; }
    }
    const excluded = Object.entries(wiz.data.excluded_counts || {}).map(([reason, count]) => `${count} ${reason.toLowerCase()}`).join(", ");
    body = `<section class="panel wizard-select">
      <div class="wizard-select-head">
        <label class="filter search-filter"><span class="visually-hidden">Search modules</span><input id="wizModuleSearch" type="search" placeholder="Search modules" value="${esc(wiz.moduleQuery)}"></label>
        <label class="filter search-filter"><span class="visually-hidden">Find test cases</span><input id="wizCaseSearch" type="search" placeholder="Find test cases by ID or title" value="${esc(wiz.caseQuery)}"></label>
        ${!suiteMode && (wiz.suites || []).length ? `<label class="filter"><span class="visually-hidden">Add a saved suite</span><select id="wizSuite" aria-label="Add a saved suite"><option value="">+ Add a saved suite…</option>${wiz.suites.map((item) => `<option value="${esc(item.id)}">${esc(item.name)} (${item.eligible})</option>`).join("")}</select></label>` : ""}
        <button class="button secondary small" onclick="wizSelectAll()">Select all modules</button><button class="button secondary small" onclick="wizClear()">Clear selection</button>
      </div>
      ${excluded ? `<p class="muted wizard-note">Not available for selection: ${esc(excluded)}.</p>` : ""}
      <div class="wizard-panes"><div class="wizard-tree" id="wizTree"></div><div class="wizard-cases" id="wizCases"></div></div>
      <div class="wizard-footer"><div id="wizSummary" aria-live="polite"></div><div class="heading-actions">${cancel}${suiteMode ? '<button class="button" id="wizNext" onclick="saveSuite()">Save suite</button>' : `${can("plans.write") ? '<button class="button secondary" onclick="saveSuite()" title="Save this selection as a reusable suite">Save as suite</button>' : ""}${wiz.cycle ? "" : '<button class="button secondary" onclick="wizGo(1)">Back</button>'}<button class="button" id="wizNext" onclick="wizPreview()">Next: preview</button>`}</div></div>
    </section>`;
  } else if (wiz.step === 3) {
    const p = wiz.preview;
    const groups = {};
    p.cases.forEach((row) => { (groups[row.module_path] ||= []).push(row); });
    body = `<section class="panel"><div class="panel-head"><h2>${p.total} test case${p.total === 1 ? "" : "s"} will be ${wiz.cycle ? "added" : "included"}</h2><span class="muted">${p.modules.length} module${p.modules.length === 1 ? "" : "s"}</span></div>
      ${p.skipped.length ? `<div class="notice warn wizard-notice"><strong>${p.skipped.length} selected test case${p.skipped.length === 1 ? " is" : "s are"} not eligible and will be left out:</strong><ul>${p.skipped.map((item) => `<li>${esc(item.case_key || item.id)} ${esc(item.title)} — ${esc(item.reason)}</li>`).join("")}</ul></div>` : ""}
      ${p.already_in_cycle.length ? `<div class="notice wizard-notice">${p.already_in_cycle.length} selected test case${p.already_in_cycle.length === 1 ? " is" : "s are"} already in this cycle and will not be added twice.</div>` : ""}
      <div class="table-wrap"><table class="case-table"><thead><tr><th>ID</th><th>Test case</th><th>Priority</th><th>Type</th><th>Version</th></tr></thead><tbody>
      ${Object.entries(groups).map(([path, rows]) => `<tr class="group-row"><td colspan="5"><strong>${esc(path || "No module")}</strong> <span class="muted">${rows.length}</span></td></tr>` + rows.map((row) => `<tr><td class="case-key">${esc(row.case_key)}</td><td>${esc(row.title)}${row.status === "Deprecated" ? ` ${status(row.status)}` : ""}</td><td>${esc(row.priority || "—")}</td><td>${esc(row.test_type || "—")}</td><td>v${esc(row.version)}</td></tr>`).join("")).join("") || '<tr><td colspan="5"><div class="empty">Nothing to add. Go back and select test cases.</div></td></tr>'}
      </tbody></table></div>
      <div class="wizard-footer"><div class="muted">Each test case is added at its current version; later repository edits will not change this cycle.</div><div class="heading-actions">${cancel}<button class="button secondary" onclick="wizGo(2)">Back</button><button class="button" ${p.total ? "" : "disabled"} onclick="wizGo(4)">Next: confirm</button></div></div></section>`;
  } else {
    const p = wiz.preview;
    const info = wiz.info;
    const type = info.testing_type === "Custom" ? info.custom_type : info.testing_type;
    body = `<section class="panel form-panel confirm-panel">
      ${wiz.cycle ? `<h2>Add ${p.total} test case${p.total === 1 ? "" : "s"} to “${esc(wiz.cycle.name)}”</h2>` : `<h2>${esc(info.name)}</h2>
      <dl class="confirm-grid"><div><dt>Testing type</dt><dd>${esc(type)}</dd></div><div><dt>Environment</dt><dd>${esc(info.environment || "—")}</dd></div><div><dt>Build</dt><dd>${esc(info.build || "—")}</dd></div><div><dt>Dates</dt><dd>${esc(info.start_date || "—")} → ${esc(info.target_date || "—")}</dd></div><div><dt>Test plan</dt><dd>${esc(wiz.plans.find((plan) => plan.id === info.plan_id)?.name || "None")}</dd></div><div><dt>Test cases</dt><dd>${p.total}</dd></div></dl>
      ${info.description ? `<p class="prewrap">${esc(info.description)}</p>` : ""}`}
      <h3 class="confirm-subhead">By module</h3><ul class="module-counts">${p.modules.map((module) => `<li><span>${esc(module.path || "No module")}</span><strong>${module.count}</strong></li>`).join("")}</ul>
      <p id="wizError" class="form-error" role="alert"></p>
      <div class="form-actions">${cancel}<button class="button secondary" onclick="wizGo(3)">Back</button><button class="button" id="wizCreate" onclick="wizCreate()">${wiz.cycle ? `Add ${p.total} test case${p.total === 1 ? "" : "s"}` : "Create cycle"}</button></div></section>`;
  }
  $("#app").innerHTML = heading(title, suiteMode ? "Tick modules to include all their test cases (now and in future), or pick individual test cases." : wiz.cycle ? "Pick more test cases from the repository. Test cases already in the cycle are skipped." : "Choose modules from the master repository; their eligible test cases are added automatically.") + wizardSteps() + body;
  associateFormLabels();
  if (wiz.step === 1) {
    $("#wizInfo").onsubmit = (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.currentTarget));
      const error = !values.name.trim() ? "Cycle name is required"
        : values.testing_type === "Custom" && !values.custom_type.trim() ? "Enter a name for the custom testing type"
        : values.start_date && values.target_date && values.target_date < values.start_date ? "Target completion date cannot be before the start date" : "";
      if (error) { $("#wizError").textContent = error; return; }
      wiz.info = values;
      wizGo(2);
    };
  } else if (wiz.step === 2) {
    $("#wizModuleSearch").oninput = (event) => { wiz.moduleQuery = event.target.value; drawWizTree(); };
    $("#wizCaseSearch").oninput = (event) => { wiz.caseQuery = event.target.value; drawWizCases(); };
    const suitePicker = $("#wizSuite");
    if (suitePicker) suitePicker.onchange = () => { if (suitePicker.value) applySuiteToWizard(suitePicker.value); suitePicker.value = ""; };
    const unapproved = $("#wizUnapproved");
    if (unapproved) unapproved.onchange = () => { wiz.includeUnapproved = unapproved.checked; wiz.data = null; renderWizard(); };
    drawWizTree(); drawWizCases(); drawWizSummary();
  }
}

function wizGo(step) { wiz.step = step; renderWizard(); }

function wizChildren() {
  const ids = new Set(wiz.data.folders.map((folder) => folder.id));
  const children = new Map();
  wiz.data.folders.forEach((folder) => {
    const parent = ids.has(folder.parent_id) ? folder.parent_id : "";
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(folder);
  });
  children.forEach((list) => list.sort((a, b) => a.name.localeCompare(b.name)));
  return children;
}

function wizAncestorSelected(folderId) {
  const byId = new Map(wiz.data.folders.map((folder) => [folder.id, folder]));
  let current = byId.get(byId.get(folderId)?.parent_id);
  while (current) { if (wiz.modules.has(current.id)) return true; current = byId.get(current.parent_id); }
  return false;
}

function wizCovered(folderId) { return wiz.modules.has(folderId) || wizAncestorSelected(folderId); }

function wizIsSelected(testCase) {
  return (wizCovered(testCase.folder_id) && !wiz.exclude.has(testCase.id)) || wiz.include.has(testCase.id);
}

function wizSubtree(folderId, children) {
  const ids = [folderId];
  (children.get(folderId) || []).forEach((child) => ids.push(...wizSubtree(child.id, children)));
  return ids;
}

function drawWizTree() {
  const children = wizChildren();
  const query = wiz.moduleQuery.trim().toLowerCase();
  const casesByFolder = new Map();
  wiz.data.cases.forEach((testCase) => { (casesByFolder.get(testCase.folder_id) || casesByFolder.set(testCase.folder_id, []).get(testCase.folder_id)).push(testCase); });
  const matches = (folder) => !query || folder.path.toLowerCase().includes(query) || (children.get(folder.id) || []).some(matches);
  const branch = (parentId, depth) => (children.get(parentId) || []).filter(matches).map((folder) => {
    const subtree = wizSubtree(folder.id, children);
    const eligible = subtree.flatMap((id) => casesByFolder.get(id) || []);
    const selected = eligible.filter(wizIsSelected).length;
    const inherited = wizAncestorSelected(folder.id);
    const checked = wiz.modules.has(folder.id) || inherited;
    const partial = !checked && selected > 0;
    const hasChildren = (children.get(folder.id) || []).length > 0;
    const collapsed = wiz.collapsed.has(folder.id) && !query;
    return `<div class="wiz-node ${wiz.focus === folder.id ? "focused" : ""}" style="--depth:${Math.min(depth, 8)}">
        ${hasChildren ? `<button type="button" class="tree-toggle" onclick="wizToggleCollapse(${jsArg(folder.id)})" aria-label="${collapsed ? "Expand" : "Collapse"} ${esc(folder.name)}" aria-expanded="${!collapsed}">${collapsed ? "▸" : "▾"}</button>` : '<span class="tree-toggle-placeholder"></span>'}
        <input type="checkbox" ${checked ? "checked" : ""} ${inherited || !eligible.length ? "disabled" : ""} data-partial="${partial}" aria-label="Select module ${esc(folder.name)}" onchange="wizToggleModule(${jsArg(folder.id)})">
        <button type="button" class="wiz-node-label" onclick="wizFocus(${jsArg(folder.id)})"><span>${esc(folder.name)}</span><span class="tree-count">${selected ? `${selected}/` : ""}${eligible.length}</span></button>
      </div>${hasChildren && !collapsed ? branch(folder.id, depth + 1) : ""}`;
  }).join("");
  const html = branch("", 1);
  $("#wizTree").innerHTML = html || `<div class="empty">${query ? "No modules match your search." : "No modules with eligible test cases."}</div>`;
  document.querySelectorAll('#wizTree input[data-partial="true"]').forEach((input) => { input.indeterminate = true; });
}

function drawWizCases() {
  const query = wiz.caseQuery.trim().toLowerCase();
  const children = wizChildren();
  let cases;
  let caption;
  if (query) {
    cases = wiz.data.cases.filter((testCase) => `${testCase.case_key} ${testCase.title}`.toLowerCase().includes(query));
    caption = `${cases.length} matching test case${cases.length === 1 ? "" : "s"}`;
  } else if (wiz.focus) {
    const ids = new Set(wizSubtree(wiz.focus, children));
    cases = wiz.data.cases.filter((testCase) => ids.has(testCase.folder_id));
    caption = wiz.data.folders.find((folder) => folder.id === wiz.focus)?.path || "";
  } else {
    $("#wizCases").innerHTML = '<div class="empty">Tick modules to include all their eligible test cases, or click a module name to choose individual test cases. Use “Find test cases” to search across all modules.</div>';
    return;
  }
  const paths = new Map(wiz.data.folders.map((folder) => [folder.id, folder.path]));
  cases.sort((a, b) => String(a.case_key).localeCompare(String(b.case_key), undefined, { numeric: true }));
  $("#wizCases").innerHTML = `<div class="wiz-cases-head"><strong>${esc(caption)}</strong>${cases.length ? `<span class="heading-actions"><button class="link" onclick="wizSetCases(true)">Select these</button><button class="link" onclick="wizSetCases(false)">Unselect these</button></span>` : ""}</div>
    <ul class="wiz-case-list">${cases.map((testCase) => `<li><label><input type="checkbox" ${wizIsSelected(testCase) ? "checked" : ""} onchange="wizToggleCase(${jsArg(testCase.id)})"><span class="case-key">${esc(testCase.case_key)}</span><span class="wiz-case-title">${esc(testCase.title)}</span>${testCase.status === "Deprecated" ? status(testCase.status) : ""}<small class="muted">${esc(query ? paths.get(testCase.folder_id) || "" : testCase.priority || "")}</small></label></li>`).join("") || '<li class="empty">No eligible test cases here.</li>'}</ul>`;
  wiz.visibleCaseIds = cases.map((testCase) => testCase.id);
}

function drawWizSummary() {
  const selected = wiz.data.cases.filter(wizIsSelected);
  const modules = new Set(selected.map((testCase) => testCase.folder_id));
  $("#wizSummary").innerHTML = `<strong>${selected.length}</strong> test case${selected.length === 1 ? "" : "s"} selected from <strong>${modules.size}</strong> module${modules.size === 1 ? "" : "s"}${wiz.exclude.size ? ` · ${wiz.exclude.size} excluded` : ""}`;
  $("#wizNext").disabled = selected.length === 0;
}

function wizRedraw() { drawWizTree(); drawWizCases(); drawWizSummary(); }

function wizToggleCollapse(folderId) {
  if (wiz.collapsed.has(folderId)) wiz.collapsed.delete(folderId); else wiz.collapsed.add(folderId);
  drawWizTree();
}

function wizFocus(folderId) { wiz.focus = folderId; wiz.caseQuery = ""; const search = $("#wizCaseSearch"); if (search) search.value = ""; drawWizTree(); drawWizCases(); }

function wizToggleModule(folderId) {
  const children = wizChildren();
  const subtree = wizSubtree(folderId, children);
  if (wiz.modules.has(folderId)) {
    wiz.modules.delete(folderId);
  } else {
    subtree.forEach((id) => wiz.modules.delete(id));
    wiz.modules.add(folderId);
    const ids = new Set(subtree);
    wiz.data.cases.filter((testCase) => ids.has(testCase.folder_id)).forEach((testCase) => { wiz.exclude.delete(testCase.id); wiz.include.delete(testCase.id); });
  }
  if (!wiz.focus) wiz.focus = folderId;
  wizRedraw();
}

function wizToggleCase(caseId) {
  const testCase = wiz.data.cases.find((item) => item.id === caseId);
  if (!testCase) return;
  if (wizIsSelected(testCase)) {
    if (wiz.include.has(caseId)) wiz.include.delete(caseId); else wiz.exclude.add(caseId);
  } else if (wiz.exclude.has(caseId)) wiz.exclude.delete(caseId); else wiz.include.add(caseId);
  wizRedraw();
}

function wizSetCases(select) {
  (wiz.visibleCaseIds || []).forEach((caseId) => {
    const testCase = wiz.data.cases.find((item) => item.id === caseId);
    if (testCase && wizIsSelected(testCase) !== select) wizToggleCase(caseId);
  });
  wizRedraw();
}

function wizSelectAll() {
  const children = wizChildren();
  wiz.modules = new Set((children.get("") || []).map((folder) => folder.id));
  wiz.include.clear(); wiz.exclude.clear();
  wizRedraw();
}

function wizClear() { wiz.modules.clear(); wiz.include.clear(); wiz.exclude.clear(); wizRedraw(); }

function wizSelection() {
  return { module_ids: [...wiz.modules], include_case_ids: [...wiz.include], exclude_case_ids: [...wiz.exclude], include_unapproved: wiz.includeUnapproved, suite_refs: wiz.suiteRefs || [] };
}

async function wizPreview() {
  const button = $("#wizNext");
  if (button) button.disabled = true;
  try {
    const path = wiz.cycle ? `/cycles/${encodeURIComponent(wiz.cycle.id)}/preview` : "/cycles/preview";
    wiz.preview = await api(path, { method: "POST", body: JSON.stringify({ project_id: state.project.id, ...wizSelection() }) });
    wizGo(3);
  } catch (error) { toast(error.message); if (button) button.disabled = false; }
}

async function wizCreate() {
  const button = $("#wizCreate");
  button.disabled = true;
  button.textContent = wiz.cycle ? "Adding…" : "Creating…";
  try {
    if (wiz.cycle) {
      const result = await api(`/cycles/${encodeURIComponent(wiz.cycle.id)}/add-cases`, { method: "POST", body: JSON.stringify(wizSelection()) });
      toast(`${result.added} test case${result.added === 1 ? "" : "s"} added to ${wiz.cycle.name}`);
      state.selectedCycle = wiz.cycle.id;
    } else {
      const result = await api("/cycles", { method: "POST", body: JSON.stringify({ project_id: state.project.id, ...wiz.info, ...wizSelection() }) });
      toast(`${result.cycle.name} created with ${result.added} test case${result.added === 1 ? "" : "s"}`);
      state.selectedCycle = result.cycle.id;
    }
    wiz = null;
    await go("execution");
  } catch (error) {
    $("#wizError").textContent = error.message;
    button.disabled = false;
    button.textContent = wiz.cycle ? "Add test cases" : "Create cycle";
  }
}

// ------------------------------------------------------------- execution page
let cycleData = null;  // assignment overview of the selected cycle
let boardDrag = null;

function ownerName(id) { return id ? cycleData?.names?.[id] || state.users.find((user) => user.id === id)?.name || "Former member" : ""; }

function ownerChip(run) {
  if (!run.assignee_id) return `<span class="owner none">${run.assignment_source === "manual" ? "Unassigned (kept)" : "Unassigned"}</span>`;
  const name = ownerName(run.assignee_id);
  const how = run.assignment_source === "manual" ? "individually" : "via module";
  return `<span class="owner" title="Assigned ${how}"><span class="avatar xs" aria-hidden="true">${esc(initials(name))}</span><span>${esc(name)}</span><small class="owner-how ${run.assignment_source === "manual" ? "manual" : ""}">${run.assignment_source === "manual" ? "✎" : "▤"}<span class="visually-hidden"> assigned ${how}</span></small></span>`;
}

function runStatusSelect(run, label) {
  const current = run.status || "Not Run";
  const mine = run.assignee_id === state.user.id;
  const allowed = can("execution.update_any") || (can("execution.update") && mine);
  return allowed
    ? `<select class="last-status-select status-${esc(current.toLowerCase().replaceAll(" ", "-"))}" aria-label="Status for ${esc(label)}" data-run-status="${esc(run.id)}" data-previous-status="${esc(current)}">${RUN_STATES.map((item) => `<option ${item === current ? "selected" : ""}>${item}</option>`).join("")}</select>`
    : status(current);
}

function cycleTabs(cycle) {
  const tabs = [["cases", "Test cases"], ["modules", "Assignments"], ["board", "Board"], ["history", "Cycle history"]];
  if (!can("assignments.manage")) tabs.splice(2, 1);
  if (!tabs.some(([key]) => key === state.cycleTab)) state.cycleTab = "cases";
  const body = { cases: cycleCasesTab, modules: cycleModulesTab, board: cycleBoardTab, history: () => '<div id="assignHistory"><div class="empty"><span class="spinner" aria-hidden="true"></span> Loading history…</div></div>' }[state.cycleTab](cycle);
  return `<section class="panel cycle-tabs"><div class="tabs" role="tablist">${tabs.map(([key, text]) => `<button role="tab" aria-selected="${key === state.cycleTab}" class="tab ${key === state.cycleTab ? "active" : ""}" onclick="setCycleTab('${key}')">${esc(text)}${key === "cases" ? ` <span class="tab-count">${cycleData.total}</span>` : ""}</button>`).join("")}</div>${body}</section>`;
}

async function setCycleTab(tab) { state.cycleTab = tab; state.runSelection.clear(); await render(); }

// ----------------------------------------------------------- test cases tab
function filteredCycleRuns() {
  const f = state.runFilter;
  const needle = f.q.trim().toLowerCase();
  return cycleData.runs.filter((run) => {
    if (f.assignee === "__none" && run.assignee_id) return false;
    if (f.assignee === "__me" && run.assignee_id !== state.user.id) return false;
    if (f.assignee && !f.assignee.startsWith("__") && run.assignee_id !== f.assignee) return false;
    if (f.status && (run.status || "Not Run") !== f.status) return false;
    if (f.module && !(run.module_path === f.module || String(run.module_path).startsWith(`${f.module}/`))) return false;
    return !needle || `${run.case_key} ${run.case_title}`.toLowerCase().includes(needle);
  }).sort((a, b) => String(a.module_path).localeCompare(String(b.module_path)) || String(a.case_key).localeCompare(String(b.case_key), undefined, { numeric: true }));
}

function cycleCasesTab() {
  const manage = can("assignments.manage");
  const f = state.runFilter;
  const members = cycleData.members;
  const modules = cycleData.modules.map((module) => module.path);
  return `<div class="filter-bar" role="search">
      <label class="filter search-filter"><span class="visually-hidden">Search test cases</span><input id="runSearch" type="search" placeholder="Search by ID or title" value="${esc(f.q)}"></label>
      <label class="filter"><span class="visually-hidden">Assigned to</span><select id="runAssignee" aria-label="Assigned to"><option value="">Assigned to: Anyone</option><option value="__me" ${f.assignee === "__me" ? "selected" : ""}>Assigned to me</option><option value="__none" ${f.assignee === "__none" ? "selected" : ""}>Unassigned (${cycleData.unassigned})</option>${members.map((member) => `<option value="${esc(member.id)}" ${f.assignee === member.id ? "selected" : ""}>${esc(member.name)} (${member.cycle_total})</option>`).join("")}</select></label>
      <label class="filter"><span class="visually-hidden">Module</span><select id="runModule" aria-label="Module"><option value="">Module: All</option>${modules.map((path) => `<option value="${esc(path)}" ${f.module === path ? "selected" : ""}>${esc(path)}</option>`).join("")}</select></label>
      <label class="filter"><span class="visually-hidden">Status</span><select id="runStatusFilter" aria-label="Status"><option value="">Status: All</option>${RUN_STATES.map((item) => `<option ${f.status === item ? "selected" : ""}>${item}</option>`).join("")}</select></label>
      <span class="muted"><strong id="runCount">${filteredCycleRuns().length}</strong> shown</span>
    </div>
    <div class="table-wrap"><table class="case-table exec-table"><thead><tr>${manage ? '<th class="check-col"><input type="checkbox" id="runSelectAll" aria-label="Select all shown test cases"></th>' : ""}<th class="id-col">ID</th><th>Test case</th><th>Module</th><th class="pri-col">Priority</th><th>Assigned to</th><th class="status-col">Status</th>${manage ? "<th></th>" : ""}</tr></thead><tbody id="runRows">${cycleRunRows()}</tbody></table></div>
    ${manage ? '<div id="runBulkBar" class="bulk-bar" role="toolbar" aria-label="Assignment actions" hidden></div>' : ""}
    <p class="muted panel-note">▤ follows its module's default owner · ✎ assigned individually (module changes leave it alone).</p>`;
}

function refreshRunRows() {
  const body = $("#runRows");
  if (!body) return;
  body.innerHTML = cycleRunRows();
  $("#runCount").textContent = String(filteredCycleRuns().length);
  bindRunRows();
}

function bindRunRows() {
  document.querySelectorAll(".run-check").forEach((item) => {
    item.onchange = () => { if (item.checked) state.runSelection.add(item.dataset.run); else state.runSelection.delete(item.dataset.run); item.closest("tr").classList.toggle("row-selected", item.checked); updateRunBulkBar(); };
  });
  document.querySelectorAll("#runRows .last-status-select[data-run-status]").forEach((item) => { item.onchange = () => updateRunStatus(item); });
  const all = $("#runSelectAll");
  if (all) {
    const shown = filteredCycleRuns().map((run) => run.id);
    all.checked = shown.length > 0 && shown.every((id) => state.runSelection.has(id));
    all.onchange = () => { shown.forEach((id) => (all.checked ? state.runSelection.add(id) : state.runSelection.delete(id))); refreshRunRows(); };
  }
  updateRunBulkBar();
}

function updateRunBulkBar() {
  const bar = $("#runBulkBar");
  if (!bar) return;
  const count = state.runSelection.size;
  bar.hidden = count === 0;
  bar.innerHTML = count ? `<strong>${count} selected</strong><button class="button secondary small" onclick="assignSelectedRuns()">Assign to…</button><button class="button secondary small" onclick="resetSelectedRuns()">Use module default</button><button class="link" onclick="state.runSelection.clear(); refreshRunRows()">Clear selection</button>` : "";
}

function bindExecutionView() {
  const search = $("#runSearch");
  if (search) {
    let timer = null;
    search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { state.runFilter.q = search.value; refreshRunRows(); }, 150); };
    $("#runAssignee").onchange = (event) => { state.runFilter.assignee = event.target.value; refreshRunRows(); };
    $("#runModule").onchange = (event) => { state.runFilter.module = event.target.value; refreshRunRows(); };
    $("#runStatusFilter").onchange = (event) => { state.runFilter.status = event.target.value; refreshRunRows(); };
    bindRunRows();
  }
  if ($("#assignBoard")) bindBoard();
  if ($("#assignHistory")) loadAssignmentHistory();
}

function memberOptions(selected = "", { allowNone = false, noneLabel = "Nobody" } = {}) {
  return (allowNone ? `<label class="member-option"><input type="radio" name="assignee_id" value="" aria-label="${esc(noneLabel)}" ${selected ? "" : "checked"}><span><strong>${esc(noneLabel)}</strong></span></label>` : "") +
    cycleData.members.map((member) => `<label class="member-option"><input type="radio" name="assignee_id" value="${esc(member.id)}" aria-label="${esc(member.name)}, ${member.open_workload} open across active cycles" ${member.id === selected ? "checked" : ""} required><span class="avatar xs" aria-hidden="true">${esc(initials(member.name))}</span><span><strong>${esc(member.name)}</strong><small>${member.open_workload} open across active cycles · ${member.cycle_total} in this cycle</small></span></label>`).join("");
}

async function assignSelectedRuns() {
  const ids = [...state.runSelection];
  const values = await openDialog({ title: `Assign ${ids.length} test case${ids.length === 1 ? "" : "s"}`, confirmLabel: "Assign", body: `<fieldset class="member-list"><legend class="visually-hidden">Team member</legend>${memberOptions("", { allowNone: true, noneLabel: "Leave unassigned" })}</fieldset><p class="muted">Individual assignments override the module default and are kept when the module's owner changes.</p>` });
  if (!values) return;
  await sendAssignment({ run_ids: ids, assignee_id: values.assignee_id || "" }, "assigned");
}

async function resetSelectedRuns() {
  await sendAssignment({ run_ids: [...state.runSelection], use_module_default: true }, "set to follow their module");
}

async function sendAssignment(payload, verb) {
  try {
    const result = await api(`/cycles/${encodeURIComponent(state.selectedCycle)}/assign`, { method: "POST", body: JSON.stringify(payload) });
    toast(`${result.updated} test case${result.updated === 1 ? "" : "s"} ${verb}`);
    state.runSelection.clear();
    await render();
  } catch (error) { toast(error.message); await render(); }
}

// --------------------------------------------------------------- modules tab
function cycleModulesTab() {
  const manage = can("assignments.manage");
  const rows = cycleData.modules.map((module) => {
    const owner = module.default_assignee_id ? `<strong>${esc(ownerName(module.default_assignee_id))}</strong>` : module.inherited_assignee_id ? `<span class="muted">${esc(ownerName(module.inherited_assignee_id))} <small>(from ${esc(module.inherited_from)})</small></span>` : '<span class="muted">No default</span>';
    const breakdown = Object.entries(module.by_assignee).sort((a, b) => b[1] - a[1]).map(([id, count]) => `<span class="owner-count"><span class="avatar xs" aria-hidden="true">${esc(initials(ownerName(id)))}</span>${esc(ownerName(id))} ${count}</span>`).join("") + (module.unassigned ? `<span class="owner-count none">Unassigned ${module.unassigned}</span>` : "");
    return `<tr><td style="padding-left:${12 + (module.depth - 1) * 18}px"><strong>${esc(module.name)}</strong><small class="muted block">${esc(module.path)}</small></td><td>${module.total}</td><td>${owner}</td><td class="owner-breakdown">${breakdown}${module.manual ? `<small class="muted block">${module.manual} assigned individually</small>` : ""}</td>
      ${manage ? `<td class="row-actions"><button class="button secondary small" aria-label="${module.default_assignee_id ? "Change owner of" : "Assign"} module ${esc(module.path)}" onclick="assignModule(${jsArg(module.path)})">${module.default_assignee_id ? "Change" : "Assign"}</button></td>` : ""}</tr>`;
  }).join("");
  const members = cycleData.members.filter((member) => member.cycle_total || manage).map((member) => `<li><span class="avatar xs" aria-hidden="true">${esc(initials(member.name))}</span><span><strong>${esc(member.name)}</strong><small>${member.cycle_open} open of ${member.cycle_total} in this cycle</small></span><span class="workload" title="Open test cases across active cycles">${member.open_workload}</span></li>`).join("");
  return `<div class="assign-layout"><div class="table-wrap"><table class="case-table"><thead><tr><th>Module</th><th>Test cases</th><th>Default owner</th><th>Current owners</th>${manage ? "<th></th>" : ""}</tr></thead><tbody>${rows || `<tr><td colspan="5"><div class="empty">This cycle has no test cases.</div></td></tr>`}</tbody></table></div>
    <aside class="workload-panel"><h3>Team workload</h3><p class="muted">Open test cases (Not Run or In Progress) across all active cycles.</p><ul class="workload-list">${members || '<li class="muted">No team members yet.</li>'}</ul>${cycleData.unassigned ? `<div class="notice warn">${cycleData.unassigned} test case${cycleData.unassigned === 1 ? " has" : "s have"} no owner. <button class="link" onclick="showUnassigned()">Show them</button></div>` : '<div class="notice ok">Every test case has an owner.</div>'}</aside></div>`;
}

async function showUnassigned() {
  state.runFilter = { ...state.runFilter, assignee: "__none", q: "", module: "", status: "" };
  state.cycleTab = "cases";
  await render();
}

async function assignModule(path) {
  const module = cycleData.modules.find((item) => item.path === path);
  const values = await openDialog({ title: `Assign ${module.name}`, confirmLabel: "Assign module",
    body: `<p><strong>${module.total}</strong> test case${module.total === 1 ? "" : "s"} in ${esc(path)}${module.depth ? "" : ""}, including its submodules.</p><fieldset class="member-list"><legend class="visually-hidden">Default owner</legend>${memberOptions(module.default_assignee_id, { allowNone: Boolean(module.default_assignee_id), noneLabel: "Remove this module's default" })}</fieldset><p class="muted">Test cases assigned individually${module.manual ? ` (${module.manual})` : ""} and test cases that are already in progress or executed keep their current owner.</p>` });
  if (!values) return;
  try {
    const result = await api(`/cycles/${encodeURIComponent(state.selectedCycle)}/module-assignment`, { method: "POST", body: JSON.stringify({ module_path: path, assignee_id: values.assignee_id || "" }) });
    const kept = [result.kept_manual && `${result.kept_manual} individually assigned`, result.kept_started && `${result.kept_started} already started`].filter(Boolean).join(", ");
    toast(`${result.updated} test case${result.updated === 1 ? "" : "s"} updated${kept ? ` · kept: ${kept}` : ""}`);
    await render();
  } catch (error) { toast(error.message); }
}

// ----------------------------------------------------------------- board tab
function cycleBoardTab() {
  const f = state.boardFilter;
  const needle = f.q.trim().toLowerCase();
  const runs = cycleData.runs.filter((run) => (!f.module || run.module_path === f.module || String(run.module_path).startsWith(`${f.module}/`)) && (!needle || `${run.case_key} ${run.case_title}`.toLowerCase().includes(needle)))
    .sort((a, b) => String(a.case_key).localeCompare(String(b.case_key), undefined, { numeric: true }));
  const columns = [{ id: "", name: "Unassigned" }, ...cycleData.members];
  const card = (run) => `<article class="board-card ${state.boardSelection.has(run.id) ? "selected" : ""}" draggable="true" data-run="${esc(run.id)}" data-assignee="${esc(run.assignee_id || "")}" tabindex="0" aria-label="${esc(run.case_key)} ${esc(run.case_title)}">
      <div class="board-card-top"><span class="case-key">${esc(run.case_key)}</span>${status(run.status || "Not Run")}</div>
      <div class="board-card-title">${esc(run.case_title)}</div>
      <div class="board-card-meta"><small class="muted">${esc(run.module_path)}</small><small class="owner-how ${run.assignment_source === "manual" ? "manual" : ""}" title="${run.assignment_source === "manual" ? "Assigned individually" : run.assignee_id ? "Follows module default" : ""}">${run.assignee_id || run.assignment_source === "manual" ? (run.assignment_source === "manual" ? "✎" : "▤") : ""}</small></div>
      <label class="visually-hidden" for="move-${esc(run.id)}">Move ${esc(run.case_key)} to</label><select id="move-${esc(run.id)}" class="board-move" data-move="${esc(run.id)}"><option value="__keep">Move to…</option>${columns.map((column) => `<option value="${esc(column.id)}">${esc(column.name)}</option>`).join("")}</select>
    </article>`;
  return `<div class="filter-bar"><label class="filter search-filter"><span class="visually-hidden">Search board</span><input id="boardSearch" type="search" placeholder="Search by ID or title" value="${esc(f.q)}"></label>
      <label class="filter"><span class="visually-hidden">Module</span><select id="boardModule" aria-label="Module"><option value="">Module: All</option>${cycleData.modules.map((module) => `<option value="${esc(module.path)}" ${f.module === module.path ? "selected" : ""}>${esc(module.path)}</option>`).join("")}</select></label>
      <span class="muted">Drag cards between people. Click to select several (Ctrl/⌘ for more), then drag one of them to move all.</span></div>
    <div class="board" id="assignBoard">${columns.map((column) => {
      const mine = runs.filter((run) => (run.assignee_id || "") === column.id);
      return `<section class="board-column" data-column="${esc(column.id)}" aria-label="${esc(column.name)}, ${mine.length} test cases"><header>${column.id ? `<span class="avatar xs" aria-hidden="true">${esc(initials(column.name))}</span>` : ""}<strong>${esc(column.name)}</strong><span class="tree-count">${mine.length}</span>${column.id ? `<small class="muted">${column.open_workload ?? 0} open overall</small>` : ""}</header><div class="board-drop">${mine.map(card).join("") || '<p class="board-empty">Drop test cases here</p>'}</div></section>`;
    }).join("")}</div>`;
}

function bindBoard() {
  const search = $("#boardSearch");
  let timer = null;
  search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { state.boardFilter.q = search.value; redrawBoard(); }, 150); };
  $("#boardModule").onchange = (event) => { state.boardFilter.module = event.target.value; redrawBoard(); };
  document.querySelectorAll(".board-card").forEach((card) => {
    card.onclick = (event) => {
      if (event.target.closest("select")) return;
      const id = card.dataset.run;
      if (!(event.ctrlKey || event.metaKey)) {
        const only = state.boardSelection.size === 1 && state.boardSelection.has(id);
        state.boardSelection.clear();
        if (!only) state.boardSelection.add(id);
      } else if (state.boardSelection.has(id)) state.boardSelection.delete(id); else state.boardSelection.add(id);
      document.querySelectorAll(".board-card").forEach((item) => item.classList.toggle("selected", state.boardSelection.has(item.dataset.run)));
    };
    card.ondragstart = (event) => {
      const ids = state.boardSelection.has(card.dataset.run) ? [...state.boardSelection] : [card.dataset.run];
      boardDrag = ids;
      event.dataTransfer.setData("text/plain", JSON.stringify(ids));
      event.dataTransfer.effectAllowed = "move";
      ids.forEach((id) => document.querySelector(`.board-card[data-run="${CSS.escape(id)}"]`)?.classList.add("dragging"));
    };
    card.ondragend = () => {
      document.querySelectorAll(".board-card.dragging").forEach((item) => item.classList.remove("dragging"));
      document.querySelectorAll(".board-column.drop-target").forEach((item) => item.classList.remove("drop-target"));
    };
  });
  document.querySelectorAll(".board-column").forEach((column) => {
    column.ondragover = (event) => { if (!boardDrag) return; event.preventDefault(); event.dataTransfer.dropEffect = "move"; column.classList.add("drop-target"); };
    column.ondragleave = (event) => { if (!column.contains(event.relatedTarget)) column.classList.remove("drop-target"); };
    column.ondrop = (event) => {
      event.preventDefault();
      column.classList.remove("drop-target");
      let ids = boardDrag;
      try { ids = JSON.parse(event.dataTransfer.getData("text/plain")) || ids; } catch (ignored) {}
      boardDrag = null;
      moveCards(ids || [], column.dataset.column);
    };
  });
  document.querySelectorAll(".board-move").forEach((select) => {
    select.onchange = () => { if (select.value !== "__keep") moveCards([select.dataset.move], select.value); };
  });
}

function redrawBoard() {
  const tab = document.querySelector(".cycle-tabs");
  const board = cycleBoardTab();
  const holder = document.createElement("div");
  holder.innerHTML = board;
  tab.querySelector(".board").replaceWith(holder.querySelector(".board"));
  bindBoard();
}

async function moveCards(ids, assigneeId) {
  const runs = ids.map((id) => cycleData.runs.find((run) => run.id === id)).filter(Boolean).filter((run) => (run.assignee_id || "") !== assigneeId || run.assignment_source !== "manual");
  if (!runs.length) return;
  const expected = Object.fromEntries(runs.map((run) => [run.id, run.assignee_id || ""]));
  const previous = runs.map((run) => ({ ...run }));
  runs.forEach((run) => { run.assignee_id = assigneeId; run.assignment_source = "manual"; });  // optimistic
  state.boardSelection.clear();
  redrawBoard();
  try {
    const result = await api(`/cycles/${encodeURIComponent(state.selectedCycle)}/assign`, { method: "POST", body: JSON.stringify({ run_ids: runs.map((run) => run.id), assignee_id: assigneeId, expected }) });
    const name = assigneeId ? ownerName(assigneeId) : "Unassigned";
    toast(`${result.updated} test case${result.updated === 1 ? "" : "s"} moved to ${name}`);
    cycleData = await api(`/cycles/${encodeURIComponent(state.selectedCycle)}/assignments`);
    const stat = $("#statUnassigned");
    if (stat) { stat.textContent = String(cycleData.unassigned); stat.classList.toggle("warn-text", cycleData.unassigned > 0); }
    redrawBoard();
  } catch (error) {
    previous.forEach((before) => Object.assign(cycleData.runs.find((run) => run.id === before.id), before));
    toast(error.message);
    await render();  // re-sync with the server (e.g. someone else moved the card)
  }
}

// --------------------------------------------------------------- history tab

// ------------------------------------------------------------------ my work
const QUICK = [["Passed", "✓", "Pass"], ["Failed", "✗", "Fail"], ["Blocked", "⦸", "Blocked"], ["Not Applicable", "–", "N/A"]];
let execState = null;  // the open execution screen

function defectList(defects) {
  return Array.isArray(defects) ? defects : String(defects || "").split(",").map((item) => item.trim()).filter(Boolean);
}

function defectLinks(defects) {
  const list = defectList(defects);
  return list.map((defect) => /^https?:\/\//i.test(defect)
    ? `<a class="defect-chip" href="${esc(defect)}" target="_blank" rel="noopener noreferrer">${esc(defect.replace(/^https?:\/\/[^/]+\//i, "").slice(0, 40) || defect)}</a>`
    : `<span class="defect-chip">${esc(defect)}</span>`).join("");
}

async function workspace() {
  const ws = state.ws;
  const assignee = can("execution.update_any") ? ws.assignee : "me";
  const data = await api(`/workspace?project_id=${encodeURIComponent(state.project.id)}&assignee=${encodeURIComponent(assignee)}&cycle_id=${encodeURIComponent(ws.cycle)}${ws.includeCompleted ? "&include_completed=true" : ""}`);
  state.wsData = data;
  const viewingOthers = assignee !== "me";
  const remaining = (data.counts["Not Run"] || 0) + (data.counts["In Progress"] || 0);
  const who = assignee === "me" ? "My work" : assignee === "__all" ? "Everyone's work" : assignee === "__none" ? "Unassigned work" : `Work for ${state.users.find((user) => user.id === assignee)?.name || "member"}`;
  const people = can("execution.update_any") ? `<label class="filter"><span class="visually-hidden">Showing</span><select id="wsAssignee" aria-label="Showing work for"><option value="me" ${assignee === "me" ? "selected" : ""}>Me</option><option value="__all" ${assignee === "__all" ? "selected" : ""}>Everyone</option><option value="__none" ${assignee === "__none" ? "selected" : ""}>Unassigned</option>${state.users.filter((user) => user.status === "Active" && user.id !== state.user.id).map((user) => `<option value="${esc(user.id)}" ${assignee === user.id ? "selected" : ""}>${esc(user.name)}</option>`).join("")}</select></label>` : "";
  return heading(who, viewingOthers ? "Execution progress for the selected people." : "Test cases assigned to you in active cycles. Record results here.") +
    `<div class="metrics ws-metrics">
      <div class="metric"><div class="metric-label">Assigned</div><div class="metric-value">${data.total}</div><div class="metric-note">in ${new Set(data.runs.map((run) => run.cycle_id)).size} cycle${new Set(data.runs.map((run) => run.cycle_id)).size === 1 ? "" : "s"}</div></div>
      <div class="metric"><div class="metric-label">Completion</div><div class="metric-value">${data.completion}%</div>${progressBar(data.completion, "Completion")}<div class="metric-note">${data.executed} of ${data.total} executed</div></div>
      <div class="metric"><div class="metric-label">Remaining</div><div class="metric-value">${remaining}</div><div class="metric-note">${data.counts["In Progress"] || 0} in progress</div></div>
      <div class="metric"><div class="metric-label">Passed</div><div class="metric-value good-text">${data.counts.Passed || 0}</div></div>
      <div class="metric"><div class="metric-label">Failed</div><div class="metric-value bad-text">${data.counts.Failed || 0}</div></div>
      <div class="metric"><div class="metric-label">Blocked</div><div class="metric-value warn-text">${data.counts.Blocked || 0}</div></div>
    </div>
    <section class="panel">
      <div class="filter-bar" role="search">${people}
        <label class="filter"><span class="visually-hidden">Cycle</span><select id="wsCycle" aria-label="Cycle"><option value="">Cycle: All active</option>${data.cycles.map((cycle) => `<option value="${esc(cycle.id)}" ${ws.cycle === cycle.id ? "selected" : ""}>${esc(cycle.name)}${cycle.status === "Completed" ? " (completed)" : ""}</option>`).join("")}</select></label>
        <label class="filter"><span class="visually-hidden">Status</span><select id="wsStatus" aria-label="Status"><option value="">Status: All</option><option value="__open" ${ws.status === "__open" ? "selected" : ""}>Open (Not Run / In Progress)</option>${RUN_STATES.map((item) => `<option ${ws.status === item ? "selected" : ""}>${item}</option>`).join("")}</select></label>
        <label class="filter search-filter"><span class="visually-hidden">Search</span><input id="wsSearch" type="search" placeholder="Search by ID, title or module" value="${esc(ws.q)}"></label>
        <label class="checkbox-filter"><input type="checkbox" id="wsCompleted" aria-label="Include completed cycles" ${ws.includeCompleted ? "checked" : ""}> Include completed cycles</label>
      </div>
      <div class="table-wrap"><table class="case-table"><thead><tr><th>ID</th><th>Test case</th><th>Cycle</th><th>Priority</th>${viewingOthers ? "<th>Assigned to</th>" : ""}<th>Status</th><th>Record result</th></tr></thead><tbody id="wsRows">${workspaceRows()}</tbody></table></div>
    </section>`;
}

function workspaceVisibleRuns() {
  const ws = state.ws;
  const needle = ws.q.trim().toLowerCase();
  return state.wsData.runs.filter((run) => {
    const current = run.status || "Not Run";
    if (ws.status === "__open" && !["Not Run", "In Progress"].includes(current)) return false;
    if (ws.status && ws.status !== "__open" && current !== ws.status) return false;
    return !needle || `${run.case_key} ${run.case_title} ${run.module_path}`.toLowerCase().includes(needle);
  });
}

function workspaceRows() {
  const runs = workspaceVisibleRuns();
  state.wsList = runs.map((run) => run.id);
  const viewingOthers = can("execution.update_any") && state.ws.assignee !== "me";
  if (!runs.length) return `<tr><td colspan="${viewingOthers ? 7 : 6}"><div class="empty">${state.wsData.runs.length ? "Nothing matches these filters." : viewingOthers ? "No test cases here." : "Nothing is assigned to you in an active cycle. When your lead assigns test cases, they appear here."}</div></td></tr>`;
  return runs.map((run) => {
    const mine = run.assignee_id === state.user.id || can("execution.update_any");
    const closed = run.cycle_status === "Completed";
    const quick = mine && !closed ? `<div class="quick-actions" role="group" aria-label="Record result for ${esc(run.case_key)}">${QUICK.map(([value, icon, label]) => `<button class="quick ${esc(value.toLowerCase().replaceAll(" ", "-"))} ${run.status === value ? "current" : ""}" title="${esc(label)}" aria-label="${esc(label)} ${esc(run.case_key)}" onclick="quickResult(${jsArg(run.id)}, ${jsArg(value)})">${icon}</button>`).join("")}<button class="button small" aria-label="Execute ${esc(run.case_key)}" onclick="openExecution(${jsArg(run.id)})">Execute</button></div>` : `<button class="button secondary small" aria-label="View ${esc(run.case_key)}" onclick="openExecution(${jsArg(run.id)})">View</button>`;
    return `<tr><td class="case-id-cell"><button class="link case-key" onclick="openExecution(${jsArg(run.id)})">${esc(run.case_key)}</button></td>
      <td class="case-title-cell"><button class="link-plain" onclick="openExecution(${jsArg(run.id)})">${esc(run.case_title)}</button><small class="muted block">${esc(run.module_path)}</small>${defectList(run.defect_ids).length ? `<div class="defects">${defectLinks(run.defect_ids)}</div>` : ""}</td>
      <td>${esc(run.cycle_name)}</td><td>${esc(run.priority || "—")}</td>${viewingOthers ? `<td>${esc(run.assignee_name || "Unassigned")}</td>` : ""}
      <td>${status(run.status || "Not Run")}${run.executed_at ? `<small class="muted block">${esc(fmtDate(run.executed_at))}</small>` : ""}</td><td>${quick}</td></tr>`;
  }).join("");
}

function bindWorkspace() {
  if (!$("#wsRows")) return;
  const refresh = () => { $("#wsRows").innerHTML = workspaceRows(); };
  let timer = null;
  $("#wsSearch").oninput = (event) => { clearTimeout(timer); timer = setTimeout(() => { state.ws.q = event.target.value; refresh(); }, 150); };
  $("#wsStatus").onchange = (event) => { state.ws.status = event.target.value; refresh(); };
  $("#wsCycle").onchange = async (event) => { state.ws.cycle = event.target.value; await render(); };
  $("#wsCompleted").onchange = async (event) => { state.ws.includeCompleted = event.target.checked; state.ws.cycle = ""; await render(); };
  const who = $("#wsAssignee");
  if (who) who.onchange = async (event) => { state.ws.assignee = event.target.value; await render(); };
}

async function quickResult(runId, value) {
  const run = state.wsData?.runs.find((item) => item.id === runId);
  const base = Number(run?.attempt_count || 0);
  try {
    if (value === "Failed" || value === "Blocked") {
      const values = await openDialog({ title: `${value === "Failed" ? "Fail" : "Block"} ${run?.case_key || "test case"}`, confirmLabel: `Mark ${value}`, danger: value === "Failed",
        body: `<div class="field"><label>Actual result${value === "Blocked" ? " / reason" : ""}</label><textarea name="actual_result" maxlength="4000" placeholder="${value === "Failed" ? "What happened instead of the expected result?" : "What is blocking this test?"}"></textarea></div><div class="field"><label>Defect IDs or links (optional)</label><input name="defect_ids" placeholder="e.g. PAY-412, https://jira.example.com/browse/PAY-413"></div><p class="muted">Use Execute for step results and evidence.</p>` });
      if (!values) return;
      const payload = { status: value, base_attempt: base, actual_result: values.actual_result };
      if (values.defect_ids.trim()) payload.defect_ids = values.defect_ids;
      await api(`/test-runs/${encodeURIComponent(runId)}/execute`, { method: "POST", body: JSON.stringify(payload) });
    } else {
      await api(`/test-runs/${encodeURIComponent(runId)}/status`, { method: "PUT", body: JSON.stringify({ status: value, base_attempt: base }) });
    }
    toast(`${run?.case_key || "Test case"} marked ${value}`);
  } catch (error) { toast(error.message); }
  await render();
}

// ------------------------------------------------------------ execution screen
async function openExecution(runId, { keepDraft = false } = {}) {
  let detail;
  try { detail = await api(`/test-runs/${encodeURIComponent(runId)}/execution`); }
  catch (error) { toast(error.message); return; }
  const run = detail.run;
  const canRecord = (can("execution.update_any") || (can("execution.update") && run.assignee_id === state.user.id)) && detail.cycle.status !== "Completed";
  const draft = keepDraft && execState?.runId === runId ? execState : null;
  execState = { runId, base: Number(run.attempt_count || 0), pending: draft?.pending || [], canRecord, back: draft?.back || state.view };
  const list = state.wsList || [];
  const index = list.indexOf(runId);
  const prev = index > 0 ? list[index - 1] : "";
  const next = index >= 0 && index < list.length - 1 ? list[index + 1] : "";
  const testCase = detail.test_case;
  const stepRows = detail.steps.map(({ step, result }, position) => `<tr data-step="${esc(step.id)}"><td>${esc(step.step_number)}</td><td class="prewrap">${esc(step.action)}</td><td class="prewrap">${esc(step.expected_result)}</td><td>${esc(step.test_data || "—")}</td>
      <td class="step-result">${canRecord ? `<select class="step-status" aria-label="Result of step ${esc(step.step_number)}">${RUN_STATES.map((item) => `<option ${item === (result.status || "Not Run") ? "selected" : ""}>${item}</option>`).join("")}</select><input class="step-actual" aria-label="Actual result of step ${esc(step.step_number)}" maxlength="2000" placeholder="Actual result" value="${esc(result.actual_result || "")}">` : `${result.status ? status(result.status) : '<span class="muted">—</span>'}<div class="prewrap">${esc(result.actual_result || "")}</div>`}</td></tr>`).join("");
  const current = run.status || "Not Run";
  const resultPanel = canRecord ? `<section class="panel record-panel" aria-labelledby="recordTitle"><h2 id="recordTitle">Record result</h2>
      <fieldset class="status-choice"><legend class="visually-hidden">Execution status</legend>${RUN_STATES.map((item) => `<label class="status-option ${esc(item.toLowerCase().replaceAll(" ", "-"))}"><input type="radio" name="runStatus" value="${esc(item)}" ${item === current ? "checked" : ""}><span>${esc(item)}</span></label>`).join("")}</fieldset>
      <p id="stepHint" class="muted step-hint"></p>
      <div class="field"><label for="execActual">Actual result</label><textarea id="execActual" maxlength="4000" placeholder="What actually happened">${esc(run.actual_result || "")}</textarea></div>
      <div class="field"><label for="execNotes">Execution notes</label><textarea id="execNotes" maxlength="4000" placeholder="Data used, observations, anything the next tester should know">${esc(run.execution_notes || "")}</textarea></div>
      <div class="field"><label for="execDefects">Defects</label><input id="execDefects" placeholder="e.g. LMS-DEF-0001, PAY-412" value="${esc(defectList(run.defect_ids).join(", "))}"><span class="defect-actions"><button type="button" class="link" onclick="logDefectFromExecution(${jsArg(run.id)}, ${jsArg(run.case_key || "")}, ${jsArg(run.case_title || testCase.title || "")})">+ Log defect</button><button type="button" class="link" onclick="linkDefectFromExecution(${jsArg(run.id)})">Link existing</button></span></div>
      <div class="field"><span class="detail-label">Evidence</span><div class="evidence-drop" id="evidenceDrop" tabindex="0" aria-label="Add evidence: paste a screenshot or choose files"><strong>Paste a screenshot</strong> (Ctrl+V) or <label class="link file-link">choose files<input id="evidenceInput" type="file" multiple class="visually-hidden"></label><small class="muted">Images, PDF, logs, HAR, Office files, ZIP or video · 10 MB each</small></div><div id="evidenceList"></div></div>
      <p id="execError" class="form-error" role="alert"></p>
      <div class="form-actions"><button class="button secondary" id="saveResult">Save result</button>${next ? '<button class="button" id="saveNext">Save &amp; next</button>' : ""}</div></section>`
    : `<section class="panel record-panel"><h2>Result</h2><div>${status(current)}</div>${run.actual_result ? `<h3 class="confirm-subhead">Actual result</h3><p class="prewrap">${esc(run.actual_result)}</p>` : ""}${run.execution_notes ? `<h3 class="confirm-subhead">Notes</h3><p class="prewrap">${esc(run.execution_notes)}</p>` : ""}${defectList(run.defect_ids).length ? `<h3 class="confirm-subhead">Defects</h3><div class="defects">${defectLinks(run.defect_ids)}</div>` : ""}<div id="evidenceList"></div><p class="muted">${detail.cycle.status === "Completed" ? "This cycle is completed; results are read-only." : run.assignee_id ? `Only ${esc(detail.assignee_name || "the assigned member")} can record results.` : "Not assigned yet."}</p></section>`;
  const history = detail.history.map((entry) => `<li class="history-entry"><div class="history-head"><strong>Attempt ${esc(entry.attempt)}</strong> ${status(entry.status)} <span class="muted">from ${esc(entry.previous_status)} · ${esc(entry.executed_by_name)} · ${esc(fmtDate(entry.executed_at))} · ${esc({ quick: "quick update", execution: "full execution", steps: "step results" }[entry.source] || entry.source)}</span></div>
      ${entry.actual_result ? `<p class="prewrap"><span class="detail-label">Actual</span>${esc(entry.actual_result)}</p>` : ""}${entry.notes ? `<p class="prewrap"><span class="detail-label">Notes</span>${esc(entry.notes)}</p>` : ""}
      ${defectList(entry.defect_ids).length ? `<div class="defects">${defectLinks(entry.defect_ids)}</div>` : ""}
      ${(entry.attachments || []).length ? `<div class="history-files">${entry.attachments.map((file) => `<a href="/api/attachments/${esc(file.id)}/file" target="_blank" rel="noopener">${esc(file.filename)}</a>`).join("")}</div>` : ""}
      ${(entry.step_results || []).length ? `<details><summary>Step results</summary><ol class="history-steps">${entry.step_results.map((item) => `<li>Step ${esc(item.step)}: ${status(item.status)} ${esc(item.actual || "")}</li>`).join("")}</ol></details>` : ""}</li>`).join("");
  $("#app").innerHTML = heading(testCase.title, `${run.case_key || caseLabel(testCase)} · version ${run.test_case_version} · ${run.module_path || ""} · ${detail.cycle.name}`,
    `<div class="heading-actions"><button class="button secondary" onclick="leaveExecution()">Back</button>${prev ? `<button class="button secondary" onclick="openExecution(${jsArg(prev)})" aria-label="Previous test case">◀ Previous</button>` : ""}${next ? `<button class="button secondary" onclick="openExecution(${jsArg(next)})" aria-label="Next test case">Next ▶</button>` : ""}</div>`) +
    `<div class="exec-strip"><span>${status(current)}</span><span><span class="detail-label">Assigned to</span>${esc(detail.assignee_name || "Unassigned")}</span><span><span class="detail-label">Priority</span>${esc(run.priority || testCase.priority || "—")}</span><span><span class="detail-label">Environment</span>${esc(run.environment || detail.cycle.environment || "—")} · build ${esc(run.build || detail.cycle.build || "—")}</span><span><span class="detail-label">Last executed</span>${run.executed_at ? `${esc(detail.executed_by_name || "—")} · ${esc(fmtDate(run.executed_at))}` : "Never"}</span><span><span class="detail-label">Attempts</span>${esc(run.attempt_count || 0)}</span></div>
    <div class="exec-layout"><div class="exec-main">
      <section class="panel detail-panel">${testCase.objective ? `<div class="detail-section"><h2>Objective</h2><p class="prewrap">${esc(testCase.objective)}</p></div>` : ""}
        <div class="detail-section"><h2>Preconditions</h2><p class="prewrap">${esc(testCase.preconditions || "None.")}</p></div>
        <div class="detail-section"><h2>Expected result</h2><p class="prewrap">${esc(testCase.expected_result || "See the steps.")}</p></div>
        <div class="detail-section"><div class="section-head"><h2>Test steps</h2>${canRecord && detail.steps.length ? '<button class="button secondary small" id="allPassed">Mark all steps passed</button>' : ""}</div>
        <div class="table-wrap"><table class="steps-table"><thead><tr><th>#</th><th>Action</th><th>Expected result</th><th>Test data</th><th>Result</th></tr></thead><tbody>${stepRows || '<tr><td colspan="5" class="muted">This version has no steps; record the overall result.</td></tr>'}</tbody></table></div></div>
      </section>
      <section class="panel detail-panel"><h2>Execution history</h2>${history ? `<ol class="history-list">${history}</ol>` : '<p class="muted">No results recorded yet.</p>'}</section>
    </div><aside class="exec-side">${resultPanel}</aside></div>`;
  renderEvidence(detail.attachments, detail.history);
  if (canRecord) bindExecution(runId, next);
}

function leaveExecution() {
  const back = execState?.back || "workspace";
  execState = null;
  go(back);
}

function renderEvidence(files, history = []) {
  const holder = $("#evidenceList");
  if (!holder) return;
  const saved = new Set(history.flatMap((entry) => entry.attachment_ids || []));
  const pending = new Set(execState?.pending || []);
  holder.innerHTML = files.length ? `<ul class="evidence-list">${files.map((file) => `<li>${file.inline ? `<a href="/api/attachments/${esc(file.id)}/file" target="_blank" rel="noopener"><img src="/api/attachments/${esc(file.id)}/file" alt="${esc(file.filename)}" loading="lazy"></a>` : '<span class="file-icon" aria-hidden="true">▤</span>'}
      <span class="evidence-name"><a href="/api/attachments/${esc(file.id)}/file" target="_blank" rel="noopener">${esc(file.filename)}</a><small class="muted">${Math.max(1, Math.round(Number(file.size) / 1024))} KB · ${esc(file.uploaded_by_name || "")}${pending.has(file.id) ? " · will be attached when you save" : saved.has(file.id) ? " · in a saved result" : " · not in a saved result"}</small></span>
      ${execState?.canRecord && !saved.has(file.id) ? `<button class="link danger-text" aria-label="Remove ${esc(file.filename)}" onclick="removeEvidence(${jsArg(file.id)})">Remove</button>` : ""}</li>`).join("")}</ul>` : '<p class="muted">No evidence yet.</p>';
}

async function refreshEvidence() {
  const detail = await api(`/test-runs/${encodeURIComponent(execState.runId)}/execution`);
  renderEvidence(detail.attachments, detail.history);
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

async function uploadEvidence(files) {
  for (const file of files) {
    if (file.size > 10 * 1024 * 1024) { toast(`${file.name} is larger than 10 MB`); continue; }
    try {
      toast(`Uploading ${file.name}…`);
      const record = await api("/attachments", { method: "POST", body: JSON.stringify({ owner_type: "test_run", owner_id: execState.runId, filename: file.name, data: await fileToBase64(file) }) });
      execState.pending.push(record.id);
      toast(`${record.filename} added`);
    } catch (error) { toast(`${file.name}: ${error.message}`); }
  }
  await refreshEvidence();
}

async function removeEvidence(fileId) {
  try {
    await api(`/attachments/${encodeURIComponent(fileId)}`, { method: "DELETE" });
    execState.pending = execState.pending.filter((id) => id !== fileId);
    await refreshEvidence();
  } catch (error) { toast(error.message); }
}

function collectStepResults() {
  return [...document.querySelectorAll(".steps-table tr[data-step]")].map((row) => ({
    step_id: row.dataset.step, status: row.querySelector(".step-status").value, actual_result: row.querySelector(".step-actual").value.trim(),
  }));
}

function updateStepHint() {
  const hint = $("#stepHint");
  if (!hint) return;
  const statuses = collectStepResults().map((item) => item.status);
  if (!statuses.length || statuses.every((item) => item === "Not Run")) { hint.textContent = ""; return; }
  const derived = statuses.includes("Failed") ? "Failed" : statuses.includes("Blocked") ? "Blocked" : statuses.every((item) => item === "Not Applicable") ? "Not Applicable" : statuses.every((item) => ["Passed", "Not Applicable"].includes(item)) ? "Passed" : "In Progress";
  hint.innerHTML = `Step results suggest <strong>${esc(derived)}</strong>. <button class="link" type="button" id="useDerived">Use it</button>`;
  $("#useDerived").onclick = () => { const radio = document.querySelector(`input[name="runStatus"][value="${derived}"]`); if (radio) radio.checked = true; };
}

function bindExecution(runId, next) {
  document.querySelectorAll(".step-status").forEach((select) => { select.onchange = updateStepHint; });
  const all = $("#allPassed");
  if (all) all.onclick = () => { document.querySelectorAll(".step-status").forEach((select) => { select.value = "Passed"; }); document.querySelectorAll(".step-actual").forEach((input) => { if (!input.value.trim()) input.value = "As expected"; }); updateStepHint(); };
  $("#evidenceInput").onchange = (event) => { uploadEvidence([...event.target.files]); event.target.value = ""; };
  const drop = $("#evidenceDrop");
  drop.ondragover = (event) => { event.preventDefault(); drop.classList.add("active"); };
  drop.ondragleave = () => drop.classList.remove("active");
  drop.ondrop = (event) => { event.preventDefault(); drop.classList.remove("active"); uploadEvidence([...event.dataTransfer.files]); };
  document.onpaste = (event) => {
    if (!execState || execState.runId !== runId || !document.body.contains(drop)) return;
    const images = [...(event.clipboardData?.files || [])].filter((file) => file.type.startsWith("image/"));
    if (!images.length) return;
    event.preventDefault();
    const now = new Date(); const pad = (value) => String(value).padStart(2, "0");
    const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}_${pad(now.getHours())}-${pad(now.getMinutes())}-${pad(now.getSeconds())}`;  // local time
    uploadEvidence(images.map((file, index) => new File([file], `screenshot-${stamp}${index ? `-${index}` : ""}.${(file.type.split("/")[1] || "png").replace("jpeg", "jpg")}`, { type: file.type })));
  };
  const save = async (goNext) => {
    const status = document.querySelector('input[name="runStatus"]:checked')?.value;
    const error = $("#execError");
    error.textContent = "";
    const steps = collectStepResults().filter((item) => item.status !== "Not Run" || item.actual_result);
    const missing = steps.find((item) => ["Passed", "Failed"].includes(item.status) && !item.actual_result);
    if (missing) { error.textContent = "Enter an actual result for every step marked Passed or Failed."; return; }
    const payload = { status, actual_result: $("#execActual").value, notes: $("#execNotes").value, defect_ids: $("#execDefects").value, attachment_ids: execState.pending, base_attempt: execState.base };
    if (steps.length) payload.step_results = steps;
    document.querySelectorAll("#saveResult, #saveNext").forEach((button) => { button.disabled = true; });
    try {
      await api(`/test-runs/${encodeURIComponent(runId)}/execute`, { method: "POST", body: JSON.stringify(payload) });
      toast(`Result saved · ${status}`);
      execState.pending = [];
      if (goNext && next) await openExecution(next); else await openExecution(runId);
    } catch (failure) {
      error.innerHTML = failure.code === "stale_execution" ? `${esc(failure.message)} <button class="link" type="button" onclick="openExecution(${jsArg(runId)}, { keepDraft: true })">Reload</button>` : esc(failure.message);
      document.querySelectorAll("#saveResult, #saveNext").forEach((button) => { button.disabled = false; });
    }
  };
  $("#saveResult").onclick = () => save(false);
  const nextButton = $("#saveNext");
  if (nextButton) nextButton.onclick = () => save(true);
  updateStepHint();
}

// --------------------------------------------------- test case attachments tab
async function caseAttachmentsTab(caseId) {
  const holder = $("#caseFiles");
  if (!holder) return;
  try {
    const { items } = await api(`/attachments?owner_type=test_case&owner_id=${encodeURIComponent(caseId)}`);
    const canEdit = can("repository.write");
    holder.innerHTML = `${canEdit ? `<div class="evidence-drop"><label class="link file-link">Add attachments<input id="caseFileInput" type="file" multiple class="visually-hidden"></label><small class="muted">Specs, mock-ups, data files · 10 MB each</small></div>` : ""}
      ${items.length ? `<ul class="evidence-list">${items.map((file) => `<li>${file.inline ? `<img src="/api/attachments/${esc(file.id)}/file" alt="${esc(file.filename)}" loading="lazy">` : '<span class="file-icon" aria-hidden="true">▤</span>'}<span class="evidence-name"><a href="/api/attachments/${esc(file.id)}/file" target="_blank" rel="noopener">${esc(file.filename)}</a><small class="muted">${Math.max(1, Math.round(Number(file.size) / 1024))} KB · ${esc(file.uploaded_by_name)} · ${esc(fmtDate(file.created_at))}</small></span>${canEdit ? `<button class="link danger-text" aria-label="Remove ${esc(file.filename)}" onclick="removeCaseFile(${jsArg(caseId)}, ${jsArg(file.id)})">Remove</button>` : ""}</li>`).join("")}</ul>` : '<p class="muted">No attachments.</p>'}`;
    const input = $("#caseFileInput");
    if (input) input.onchange = async () => {
      for (const file of input.files) {
        try { await api("/attachments", { method: "POST", body: JSON.stringify({ owner_type: "test_case", owner_id: caseId, filename: file.name, data: await fileToBase64(file) }) }); toast(`${file.name} added`); }
        catch (error) { toast(`${file.name}: ${error.message}`); }
      }
      caseAttachmentsTab(caseId);
    };
  } catch (error) { holder.innerHTML = `<p class="muted">${esc(error.message)}</p>`; }
}

async function removeCaseFile(caseId, fileId) {
  try { await api(`/attachments/${encodeURIComponent(fileId)}`, { method: "DELETE" }); toast("Attachment removed"); }
  catch (error) { toast(error.message); }
  caseAttachmentsTab(caseId);
}

// --------------------------------------------------------------- compare cycles
const INSIGHTS = [
  ["regression", "Regressions", "Passed before, failing now", "critical", "▼"],
  ["new_failure", "New failures", "Failing now, not failing before", "critical", "!"],
  ["repeated_failure", "Repeated failures", "Failing in both cycles", "serious", "↻"],
  ["fixed", "Fixed", "Failed before, passing now", "good", "▲"],
  ["consistently_passing", "Consistently passing", "Passed every time it ran", "good", "✓"],
  ["blocked", "Blocked", "Blocked in the current cycle", "warning", "⦸"],
  ["not_executed", "Not executed", "In the current cycle, no result yet", "neutral", "○"],
  ["new_case", "Newly added", "First appears in the current cycle", "info", "+"],
  ["removed", "Removed", "In the previous cycle, not the current", "neutral", "−"],
];
// Blocked sits between Passed and Failed (green/red are hard to tell apart with
// red-green colour blindness), and Failed also carries a hatch texture.
const SEGMENTS = [["Passed", "Passed", "var(--c-good)"], ["Blocked", "Blocked", "var(--c-warning)"], ["Failed", "Failed", "url(#hatchFailed)"], ["Not Applicable", "Not applicable", "var(--c-neutral)"], ["Open", "Not executed", "var(--c-empty)"]];

function cmpDate(cycle) { return cycle.start_date || String(cycle.created_at || "").slice(0, 10); }

async function compareView() {
  const cycles = (await apiAll(`/test_cycles?project_id=${encodeURIComponent(state.project.id)}`))
    .sort((a, b) => cmpDate(a).localeCompare(cmpDate(b)) || String(a.created_at).localeCompare(String(b.created_at)));
  if (cycles.length < 2) {
    return heading("Compare Cycles", "See how every test case behaved across testing cycles.") +
      `<section class="panel"><div class="empty">You need at least two test cycles in this project to compare. ${can("cycles.write") ? '<button class="link" onclick="cycleWizard()">Create a test cycle</button>' : ""}</div></section>`;
  }
  const cmp = state.cmp;
  const known = new Set(cycles.map((cycle) => cycle.id));
  cmp.ids = cmp.ids.filter((id) => known.has(id));
  if (cmp.ids.length < 2) { cmp.ids = []; cmp.previous = ""; cmp.current = ""; }  // server picks the latest cycles with results
  const params = `project_id=${encodeURIComponent(state.project.id)}&cycle_ids=${cmp.ids.map(encodeURIComponent).join(",")}${cmp.previous ? `&previous_id=${encodeURIComponent(cmp.previous)}` : ""}${cmp.current ? `&current_id=${encodeURIComponent(cmp.current)}` : ""}`;
  const data = await api(`/comparison?${params}`);
  state.cmpData = data;
  cmp.ids = data.cycles.map((cycle) => cycle.id);
  cmp.previous = data.previous_id; cmp.current = data.current_id;
  const byId = new Map(data.cycles.map((cycle) => [cycle.id, cycle]));
  const picker = `<section class="panel cmp-picker"><div class="cmp-picker-row"><span class="detail-label">Cycles</span><div class="cycle-chips" role="group" aria-label="Cycles to include">${cycles.map((cycle) => `<label class="cycle-chip ${cmp.ids.includes(cycle.id) ? "on" : ""}"><input type="checkbox" value="${esc(cycle.id)}" ${cmp.ids.includes(cycle.id) ? "checked" : ""} aria-label="Include ${esc(cycle.name)}"><span>${esc(cycle.name)}</span><small>${esc(cmpDate(cycle))}</small></label>`).join("")}</div></div>
      <div class="cmp-picker-row"><label class="filter"><span class="detail-label">Previous</span><select id="cmpPrevious">${data.cycles.map((cycle) => `<option value="${esc(cycle.id)}" ${cycle.id === data.previous_id ? "selected" : ""}>${esc(cycle.name)}${cycle.has_results ? "" : " (no results yet)"}</option>`).join("")}</select></label><span class="cmp-arrow" aria-hidden="true">→</span><label class="filter"><span class="detail-label">Current</span><select id="cmpCurrent">${data.cycles.map((cycle) => `<option value="${esc(cycle.id)}" ${cycle.id === data.current_id ? "selected" : ""}>${esc(cycle.name)}${cycle.has_results ? "" : " (no results yet)"}</option>`).join("")}</select></label>
      ${[data.previous_id, data.current_id].map((id) => byId.get(id)).filter((cycle) => !cycle.has_results).map((cycle) => `<div class="notice warn cmp-explain">${esc(cycle.name)} has no results yet, so test cases in it show as Not executed. Pick a cycle that has been run.</div>`).join("")}
      <p class="muted cmp-explain">Click a test case or any result to see its history in every cycle; click a cycle name for everything that happened in that cycle. Test cases are matched by their record, not their title. Categories compare <strong>${esc(byId.get(data.previous_id)?.name)}</strong> with <strong>${esc(byId.get(data.current_id)?.name)}</strong>; the matrix and chart show every selected cycle in date order.</p></div></section>`;
  const cards = INSIGHTS.map(([key, label, hint, tone, icon]) => `<button type="button" class="insight ${tone} ${cmp.category === key ? "active" : ""}" aria-pressed="${cmp.category === key}" onclick="toggleInsight('category', '${key}')"><span class="insight-icon" aria-hidden="true">${icon}</span><span class="insight-count">${data.summary[key] || 0}</span><span class="insight-label">${esc(label)}</span><small>${esc(hint)}</small></button>`).join("") +
    `<button type="button" class="insight info ${cmp.flag === "modified" ? "active" : ""}" aria-pressed="${cmp.flag === "modified"}" onclick="toggleInsight('flag', 'modified')"><span class="insight-icon" aria-hidden="true">✎</span><span class="insight-count">${data.modified}</span><span class="insight-label">Definition changed</span><small>Different version between the two cycles</small></button>
     <button type="button" class="insight serious ${cmp.flag === "reopened" ? "active" : ""}" aria-pressed="${cmp.flag === "reopened"}" onclick="toggleInsight('flag', 'reopened')"><span class="insight-icon" aria-hidden="true">↺</span><span class="insight-count">${data.reopened}</span><span class="insight-label">Reopened</span><small>Failed, passed, then failed again</small></button>`;
  const modules = [...new Set(data.rows.map((row) => row.module_path).filter(Boolean))].sort();
  const assignees = [...new Map(data.rows.map((row) => row.cells[data.current_id]).filter((cell) => cell && cell.assignee_id).map((cell) => [cell.assignee_id, cell.assignee_name])).entries()];
  return heading("Compare Cycles", "Find regressions, fixes and unstable test cases across testing cycles.", `<a class="button secondary" href="/api/comparison/export.xlsx?${params}" download>⇩ Export Excel</a>`) + picker +
    `<div class="insights" role="group" aria-label="Insights">${cards}</div>
    <section class="panel cmp-trend"><div class="panel-head"><h2>Results by cycle</h2><div class="chart-legend" aria-hidden="true">${SEGMENTS.map(([key, label, color]) => `<span><i class="${key === "Failed" ? "hatched" : ""}" style="${key === "Failed" ? "" : `background:${color}`}"></i>${esc(label)}</span>`).join("")}</div></div>${trendChart(data.trend)}
      <details class="chart-table"><summary>Show as table</summary><div class="table-wrap"><table><thead><tr><th>Cycle</th><th>Test cases</th><th>Executed</th><th>Passed</th><th>Failed</th><th>Blocked</th><th>N/A</th><th>Not executed</th><th>Pass rate</th></tr></thead><tbody>${data.trend.map((t) => `<tr><td>${esc(t.name)}</td><td>${t.total}</td><td>${t.executed} (${t.completion}%)</td><td>${t.counts.Passed}</td><td>${t.counts.Failed}</td><td>${t.counts.Blocked}</td><td>${t.counts["Not Applicable"]}</td><td>${t.counts["Not Run"] + t.counts["In Progress"]}</td><td>${t.pass_rate}%</td></tr>`).join("")}</tbody></table></div></details></section>
    <section class="panel">
      <div class="filter-bar" role="search">
        <label class="filter search-filter"><span class="visually-hidden">Search</span><input id="cmpSearch" type="search" placeholder="Search by ID or title" value="${esc(cmp.q)}"></label>
        <label class="filter"><span class="visually-hidden">Module</span><select id="cmpModule" aria-label="Module"><option value="">Module: All</option>${modules.map((path) => `<option value="${esc(path)}" ${cmp.module === path ? "selected" : ""}>${esc(path)}</option>`).join("")}</select></label>
        <label class="filter"><span class="visually-hidden">Insight</span><select id="cmpCategory" aria-label="Insight"><option value="">Insight: All</option>${Object.entries(data.labels).map(([key, label]) => `<option value="${esc(key)}" ${cmp.category === key ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label>
        <label class="filter"><span class="visually-hidden">Current status</span><select id="cmpStatus" aria-label="Status in current cycle"><option value="">Current status: All</option>${RUN_STATES.map((item) => `<option ${cmp.status === item ? "selected" : ""}>${item}</option>`).join("")}</select></label>
        <label class="filter"><span class="visually-hidden">Assigned to</span><select id="cmpAssignee" aria-label="Assigned to in current cycle"><option value="">Assigned to: Anyone</option>${assignees.map(([id, name]) => `<option value="${esc(id)}" ${cmp.assignee === id ? "selected" : ""}>${esc(name)}</option>`).join("")}</select></label>
        <span class="muted"><strong id="cmpCount"></strong> shown</span>
      </div>
      <div class="table-wrap"><table class="case-table cmp-matrix"><thead><tr><th>ID</th><th>Test case</th>${data.cycles.map((cycle) => `<th class="${cycle.id === data.previous_id ? "col-prev" : cycle.id === data.current_id ? "col-cur" : ""}"><button class="th-link" title="Cycle history" onclick="openCycleTimeline(${jsArg(cycle.id)})">${esc(cycle.name)}</button><small class="block">${cycle.id === data.previous_id ? "Previous · " : cycle.id === data.current_id ? "Current · " : ""}${esc(cmpDate(cycle))}${cycle.has_results ? "" : " · no results"}</small></th>`).join("")}<th>Insight</th></tr></thead><tbody id="cmpRows"></tbody></table></div>
    </section>`;
}

function trendChart(trend) {
  const width = 640, height = 220, left = 36, right = 12, top = 26, bottom = 34;
  const max = Math.max(1, ...trend.map((t) => t.total));
  const step = (width - left - right) / trend.length;
  const bar = Math.min(48, step * 0.5);
  const y = (value) => top + (height - top - bottom) * (1 - value / max);
  const ticks = [0, Math.round(max / 2), max];
  const bars = trend.map((t, index) => {
    const x = left + step * index + (step - bar) / 2;
    let base = 0;
    const values = { Passed: t.counts.Passed, Failed: t.counts.Failed, Blocked: t.counts.Blocked, "Not Applicable": t.counts["Not Applicable"], Open: t.counts["Not Run"] + t.counts["In Progress"] };
    const segments = SEGMENTS.map(([key, label, color]) => {
      const value = values[key];
      if (!value) return "";
      const y1 = y(base + value), y0 = y(base);
      base += value;
      return `<rect class="seg" x="${x}" y="${y1}" width="${bar}" height="${Math.max(0, y0 - y1)}" fill="${color}" data-tip="${esc(t.name)} · ${esc(label)}: ${value} of ${t.total}"></rect>`;
    }).join("");
    return `<g>${segments}<text class="bar-label" x="${x + bar / 2}" y="${y(t.total) - 7}" text-anchor="middle">${t.executed ? `${t.pass_rate}% pass` : "No results"}</text><text class="axis-label" x="${x + bar / 2}" y="${height - 12}" text-anchor="middle">${esc(t.name.length > 16 ? `${t.name.slice(0, 15)}…` : t.name)}</text></g>`;
  }).join("");
  const grid = ticks.map((tick) => `<line class="grid" x1="${left}" x2="${width - right}" y1="${y(tick)}" y2="${y(tick)}"></line><text class="axis-label" x="${left - 6}" y="${y(tick) + 4}" text-anchor="end">${tick}</text>`).join("");
  return `<div class="chart-box"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Stacked results per cycle: ${esc(trend.map((t) => `${t.name} ${t.counts.Passed} passed, ${t.counts.Failed} failed`).join("; "))}"><defs><pattern id="hatchFailed" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="#c4384a"></rect><line x1="0" y1="0" x2="0" y2="6" stroke="#ffffff" stroke-opacity=".55" stroke-width="2"></line></pattern></defs>${grid}${bars}</svg><div class="chart-tip" id="chartTip" role="status" hidden></div></div>`;
}

function cmpVisibleRows() {
  const cmp = state.cmp, data = state.cmpData;
  const needle = cmp.q.trim().toLowerCase();
  return data.rows.filter((row) => {
    if (cmp.category && row.category !== cmp.category) return false;
    if (cmp.flag && !row.flags.includes(cmp.flag)) return false;
    if (cmp.module && !(row.module_path === cmp.module || row.module_path.startsWith(`${cmp.module}/`))) return false;
    const current = row.cells[data.current_id];
    if (cmp.status && (!current || current.status !== cmp.status)) return false;
    if (cmp.assignee && (!current || current.assignee_id !== cmp.assignee)) return false;
    return !needle || `${row.case_key} ${row.title}`.toLowerCase().includes(needle);
  });
}

function cmpRowsHtml() {
  const data = state.cmpData;
  const rows = cmpVisibleRows();
  $("#cmpCount").textContent = String(rows.length);
  const tone = Object.fromEntries(INSIGHTS.map(([key, , , t]) => [key, t]));
  if (!rows.length) return `<tr><td colspan="${data.cycles.length + 3}"><div class="empty">${data.rows.length ? "No test cases match these filters." : "The selected cycles have no test cases."}</div></td></tr>`;
  return rows.map((row) => {
    let lastVersion = "";
    const cells = data.cycles.map((cycle) => {
      const cell = row.cells[cycle.id];
      const cls = cycle.id === data.previous_id ? "col-prev" : cycle.id === data.current_id ? "col-cur" : "";
      if (!cell) return `<td class="${cls}"><span class="muted" title="Not in this cycle">—</span></td>`;
      const changed = lastVersion && lastVersion !== cell.version;
      lastVersion = cell.version;
      return `<td class="${cls}"><button class="cell-link" aria-label="${esc(row.case_key)} in ${esc(cycle.name)}: ${esc(cell.status)}. Show history" onclick="openCaseHistory(${jsArg(row.test_case_id)}, ${jsArg(cycle.id)})">${status(cell.status)}</button><small class="cell-meta">v${esc(cell.version)}${changed ? ' <span class="changed" title="Test case definition changed since the previous cycle">✎ changed</span>' : ""}${cell.defect_ids && defectList(cell.defect_ids).length ? ` · ${esc(defectList(cell.defect_ids).join(", "))}` : ""}</small></td>`;
    }).join("");
    return `<tr><td class="case-id-cell"><button class="link case-key" title="History in every cycle" onclick="openCaseHistory(${jsArg(row.test_case_id)})">${esc(row.case_key)}</button></td><td class="case-title-cell"><button class="link-plain" onclick="openCaseHistory(${jsArg(row.test_case_id)})">${esc(row.title)}</button><small class="muted block">${esc(row.module_path)}</small></td>${cells}
      <td><span class="insight-chip ${tone[row.category] || "neutral"}">${esc(data.labels[row.category])}</span>${row.flags.includes("reopened") ? '<span class="insight-chip serious">Reopened</span>' : ""}${row.flips >= 2 ? `<small class="muted block">${row.flips} pass/fail flips</small>` : ""}</td></tr>`;
  }).join("");
}

function bindCompare() {
  if (!$("#cmpRows")) return;
  const cmp = state.cmp;
  const redraw = () => { $("#cmpRows").innerHTML = cmpRowsHtml(); };
  redraw();
  let timer = null;
  $("#cmpSearch").oninput = (event) => { clearTimeout(timer); timer = setTimeout(() => { cmp.q = event.target.value; redraw(); }, 150); };
  [["cmpModule", "module"], ["cmpStatus", "status"], ["cmpAssignee", "assignee"]].forEach(([id, key]) => { $(`#${id}`).onchange = (event) => { cmp[key] = event.target.value; redraw(); }; });
  $("#cmpCategory").onchange = (event) => { cmp.category = event.target.value; render(); };
  document.querySelectorAll(".cycle-chips input").forEach((input) => {
    input.onchange = () => {
      const ids = new Set(cmp.ids);
      if (input.checked) ids.add(input.value); else ids.delete(input.value);
      if (ids.size < 2) { input.checked = true; toast("Keep at least two cycles selected"); return; }
      cmp.ids = [...ids]; cmp.previous = ""; cmp.current = "";
      render();
    };
  });
  $("#cmpPrevious").onchange = (event) => { cmp.previous = event.target.value; render(); };
  $("#cmpCurrent").onchange = (event) => { cmp.current = event.target.value; render(); };
  const tip = $("#chartTip");
  document.querySelectorAll(".cmp-trend rect.seg").forEach((rect) => {
    rect.onmousemove = (event) => {
      const box = rect.closest(".chart-box").getBoundingClientRect();
      tip.hidden = false; tip.textContent = rect.dataset.tip;
      tip.style.left = `${Math.min(event.clientX - box.left + 12, box.width - 180)}px`; tip.style.top = `${event.clientY - box.top - 30}px`;
    };
    rect.onmouseleave = () => { tip.hidden = true; };
  });
}

function toggleInsight(kind, key) {
  const cmp = state.cmp;
  if (kind === "category") { cmp.category = cmp.category === key ? "" : key; }
  else { cmp.flag = cmp.flag === key ? "" : key; }
  render();
}

// ------------------------------------------------------------------ dashboard
const TREND_SERIES = [["regressions", "Regressions", "#2a78d6"], ["new_failures", "New failures", "#eb6834"], ["reopened", "Reopened", "#1baf7a"]];

function dashQuery() {
  const d = state.dash;
  const parts = [`project_id=${encodeURIComponent(state.project.id)}`];
  if (d.cycle === "__all") parts.push("cycle_scope=all"); else if (d.cycle) parts.push(`cycle_ids=${encodeURIComponent(d.cycle)}`);
  if (d.module) parts.push(`module=${encodeURIComponent(d.module)}`);
  if (d.member && can("reports.export")) parts.push(`member=${encodeURIComponent(d.member)}`);
  if (d.from) parts.push(`date_from=${d.from}`);
  if (d.to) parts.push(`date_to=${d.to}`);
  return parts.join("&");
}

async function dashboard() {
  if (!state.project) return projectTable();
  const [data, cycles] = await Promise.all([api(`/reports/dashboard?${dashQuery()}`), apiAll(`/test_cycles?project_id=${encodeURIComponent(state.project.id)}`)]);
  state.dashData = data;
  const d = state.dash;
  const ex = data.execution, repo = data.repository;
  const admin = can("reports.export");
  const basis = { active: "Active cycles", all: "All cycles", selected: "Selected cycle", dates: "Cycles starting in the date range" }[data.scope.basis];
  const scopeText = `${basis}: ${data.scope.cycles.map((cycle) => cycle.name).join(", ") || "none"}${data.scope.module ? ` · module ${data.scope.module}` : ""}${data.scope.member && admin ? ` · ${data.scope.member === "__none" ? "unassigned work" : state.users.find((user) => user.id === data.scope.member)?.name || "member"}` : ""}`;
  const modules = [...new Set(data.modules.map((module) => module.path))];
  const tile = (label, value, note = "", extra = "", cls = "") => `<div class="metric ${cls}"><div class="metric-label">${label}</div><div class="metric-value">${value}</div>${extra}${note ? `<div class="metric-note">${note}</div>` : ""}</div>`;
  const latest = data.latest;
  return heading(admin ? "Quality dashboard" : "My dashboard", scopeText, `<span class="muted dash-stamp">Updated ${esc(fmtDate(data.generated_at))}</span>`) +
    `<section class="panel dash-filters"><div class="filter-bar" role="search">
      <label class="filter"><span class="visually-hidden">Cycle</span><select id="dashCycle" aria-label="Cycle"><option value="">Active cycles</option><option value="__all" ${d.cycle === "__all" ? "selected" : ""}>All cycles</option>${cycles.sort((a, b) => cmpDate(b).localeCompare(cmpDate(a))).map((cycle) => `<option value="${esc(cycle.id)}" ${d.cycle === cycle.id ? "selected" : ""}>${esc(cycle.name)}${cycle.status === "Completed" ? " (completed)" : ""}</option>`).join("")}</select></label>
      <label class="filter"><span class="visually-hidden">Module</span><select id="dashModule" aria-label="Module"><option value="">All modules</option>${(d.module && !modules.includes(d.module) ? [d.module, ...modules] : modules).map((path) => `<option value="${esc(path)}" ${d.module === path ? "selected" : ""}>${esc(path)}</option>`).join("")}</select></label>
      ${admin ? `<label class="filter"><span class="visually-hidden">Member</span><select id="dashMember" aria-label="Member"><option value="">Everyone</option><option value="__none" ${d.member === "__none" ? "selected" : ""}>Unassigned</option>${state.users.filter((user) => user.status === "Active").map((user) => `<option value="${esc(user.id)}" ${d.member === user.id ? "selected" : ""}>${esc(user.name)}</option>`).join("")}</select></label>` : ""}
      <label class="filter date-filter">From <input id="dashFrom" type="date" value="${esc(d.from)}" aria-label="From date"></label>
      <label class="filter date-filter">To <input id="dashTo" type="date" value="${esc(d.to)}" aria-label="To date"></label>
      <button class="link" id="dashReset">Reset</button>
      <span class="muted dash-hint">Dates pick cycles that start in the range and set the activity window.</span>
    </div></section>
    <div class="metrics dash-metrics">
      ${tile("Test cases", repo.test_cases, `in ${repo.modules} module${repo.modules === 1 ? "" : "s"}`)}
      ${tile("Cycles", data.cycles.active, `active · ${data.cycles.completed} completed`)}
      ${tile("Executed", `${ex.executed}<span class="metric-of"> / ${ex.total}</span>`, `${ex.completion}% complete`, progressBar(ex.completion, "Execution completion"))}
      ${tile("Pass rate", `${ex.pass_rate}%`, "of executed, excluding N/A")}
      ${tile("Passed", ex.counts.Passed, "", "", "good")}
      ${tile("Failed", ex.counts.Failed, "", "", "bad")}
      ${tile("Blocked", ex.counts.Blocked, "", "", "warn")}
      ${tile("Not run", ex.open, `${ex.counts["In Progress"]} in progress`)}
    </div>
    <div class="dash-grid">
      <section class="panel dash-trend"><div class="panel-head"><h2>Regression trend</h2><button class="link" onclick="go('compare')">Open Compare Cycles</button></div>
        ${latest ? `<div class="latest-strip"><span>Latest: <strong>${esc(latest.cycle)}</strong></span><span class="chip critical">${latest.regressions} regressions</span><span class="chip critical">${latest.new_failures} new failures</span><span class="chip serious">${latest.reopened} reopened</span><span class="chip good">${latest.fixed} fixed</span></div>` : ""}
        ${trendLineChart(data.trend)}
      </section>
      <section class="panel"><div class="panel-head"><h2>Recent failures</h2><span class="muted">${data.recent_failures.length}</span></div>
        ${data.recent_failures.length ? `<ul class="failure-list">${data.recent_failures.map((item) => `<li><button class="link case-key" onclick="openExecution(${jsArg(item.run_id)})">${esc(item.case_key)}</button> <span>${esc(item.title)}</span><small class="muted block">${esc(item.cycle)} · ${esc(item.assignee || "Unassigned")} · ${esc(fmtDate(item.executed_at))}</small>${item.actual_result ? `<small class="block prewrap">${esc(item.actual_result)}</small>` : ""}${defectList(item.defect_ids).length ? `<div class="defects">${defectLinks(item.defect_ids)}</div>` : ""}</li>`).join("")}</ul>` : '<div class="empty">No failed test cases in this scope.</div>'}
      </section>
    </div>
    <div class="dash-grid two">
      <section class="panel"><div class="panel-head"><h2>Module progress</h2></div>${progressTable(data.modules, "module")}</section>
      <section class="panel"><div class="panel-head"><h2>${admin ? "Team progress and workload" : "My progress"}</h2></div>${progressTable(data.members, "member")}</section>
    </div>
    <section class="panel dash-activity"><div class="panel-head"><h2>Executions per day</h2><span class="muted">${esc(data.activity[0]?.date || "")} → ${esc(data.activity.at(-1)?.date || "")}</span></div>${activityChart(data.activity)}</section>`;
}

function progressTable(rows, kind) {
  if (!rows.length) return '<div class="empty">No test cases in this scope.</div>';
  const shown = kind === "module" ? rows.filter((row) => row.depth <= 2) : rows;
  return `<div class="table-wrap"><table class="progress-table"><thead><tr><th>${kind === "module" ? "Module" : "Member"}</th><th>Test cases</th><th>Progress</th><th>Passed</th><th>Failed</th><th>Blocked</th><th>Pass rate</th>${kind === "member" ? '<th title="Open test cases across all active cycles">Open workload</th>' : ""}</tr></thead><tbody>${shown.map((row) => `<tr>
    <td style="${kind === "module" ? `padding-left:${12 + (row.depth - 1) * 16}px` : ""}">${kind === "module" ? `<strong>${esc(row.name)}</strong>` : `<span class="owner"><span class="avatar xs" aria-hidden="true">${esc(row.id ? initials(row.name) : "–")}</span>${esc(row.name)}</span>`}</td>
    <td>${row.total}</td><td class="progress-cell">${progressBar(row.completion, `${row.name} completion`)}<small>${row.executed}/${row.total} · ${row.completion}%</small></td>
    <td>${row.counts.Passed}</td><td class="${row.counts.Failed ? "bad-text" : ""}">${row.counts.Failed}</td><td class="${row.counts.Blocked ? "warn-text" : ""}">${row.counts.Blocked}</td><td>${row.executed ? `${row.pass_rate}%` : "—"}</td>
    ${kind === "member" ? `<td>${row.workload === null ? "—" : `<span class="workload">${row.workload}</span>`}</td>` : ""}</tr>`).join("")}</tbody></table></div>${kind === "module" && shown.length < rows.length ? '<p class="muted panel-note">Showing modules and their first level of submodules. Filter by a module to see deeper levels.</p>' : ""}`;
}

function trendLineChart(trend) {
  if (trend.length < 2) return `<div class="empty">The trend needs at least two cycles with results${trend.length ? ` (only ${esc(trend[0].name)} so far)` : ""}.</div>`;
  const width = 640, height = 230, left = 34, right = 96, top = 18, bottom = 34;
  const max = Math.max(2, ...trend.flatMap((point) => TREND_SERIES.map(([key]) => point[key])));
  const step = (width - left - right) / (trend.length - 1);
  const x = (index) => left + step * index;
  const y = (value) => top + (height - top - bottom) * (1 - value / max);
  const ticks = [...new Set([0, Math.round(max / 2), max])];
  const grid = ticks.map((tick) => `<line class="grid" x1="${left}" x2="${width - right}" y1="${y(tick)}" y2="${y(tick)}"></line><text class="axis-label" x="${left - 6}" y="${y(tick) + 4}" text-anchor="end">${tick}</text>`).join("");
  const labels = trend.map((point, index) => `<text class="axis-label" x="${x(index)}" y="${height - 12}" text-anchor="middle">${esc(point.name.length > 14 ? `${point.name.slice(0, 13)}…` : point.name)}</text>`).join("");
  // Direct labels at the end of each line, nudged apart so they never overlap.
  // Stack from the bottom up so labels never fall onto the x-axis labels.
  const ends = TREND_SERIES.map(([key, label, color]) => ({ label, color, y: y(trend.at(-1)[key]) })).sort((a, b) => b.y - a.y);
  ends.forEach((end, index) => { end.y = Math.min(end.y, height - bottom - 6); if (index && ends[index - 1].y - end.y < 13) end.y = ends[index - 1].y - 13; });
  const lines = TREND_SERIES.map(([key, , color]) => `<polyline fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" points="${trend.map((point, index) => `${x(index)},${y(point[key])}`).join(" ")}"></polyline>${trend.map((point, index) => `<circle cx="${x(index)}" cy="${y(point[key])}" r="4" fill="${color}" stroke="white" stroke-width="2"></circle>`).join("")}`).join("");
  const direct = ends.map((end) => `<text class="series-label" x="${width - right + 8}" y="${end.y + 4}">${esc(end.label)}</text>`).join("");
  const bands = trend.map((point, index) => `<rect class="hit" x="${x(index) - step / 2}" y="${top}" width="${step}" height="${height - top - bottom}" data-index="${index}"></rect>`).join("");
  return `<div class="chart-legend" aria-hidden="true">${TREND_SERIES.map(([, label, color]) => `<span><i style="background:${color}"></i>${esc(label)}</span>`).join("")}</div>
    <div class="chart-box trend-box"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Regression trend: ${esc(trend.map((point) => `${point.name}: ${point.regressions} regressions, ${point.new_failures} new failures, ${point.reopened} reopened`).join("; "))}">${grid}<line class="crosshair" id="trendCross" x1="0" x2="0" y1="${top}" y2="${height - bottom}" visibility="hidden"></line>${lines}${direct}${labels}${bands}</svg><div class="chart-tip" id="trendTip" role="status" hidden></div></div>
    <details class="chart-table"><summary>Show as table</summary><div class="table-wrap"><table><thead><tr><th>Cycle</th><th>Start</th><th>Regressions</th><th>New failures</th><th>Reopened</th><th>Fixed</th><th>Executed</th><th>Pass rate</th></tr></thead><tbody>${trend.map((point) => `<tr><td>${esc(point.name)}</td><td>${esc(point.date)}</td><td>${point.regressions}</td><td>${point.new_failures}</td><td>${point.reopened}</td><td>${point.fixed}</td><td>${point.executed}/${point.total}</td><td>${point.pass_rate}%</td></tr>`).join("")}</tbody></table></div><p class="muted">Each cycle is compared with the cycle before it. The first cycle has no comparison.</p></details>`;
}

function activityChart(days) {
  const width = 640, height = 150, left = 30, right = 8, top = 12, bottom = 26;
  const max = Math.max(1, ...days.map((day) => day.count));
  const step = (width - left - right) / days.length;
  const bar = Math.max(2, Math.min(22, step - 4));
  const y = (value) => top + (height - top - bottom) * (1 - value / max);
  const every = Math.ceil(days.length / 8);
  const bars = days.map((day, index) => {
    const x = left + step * index + (step - bar) / 2, h = (height - top - bottom) * (day.count / max);
    const radius = Math.min(4, bar / 2, h);
    const path = day.count ? `<path d="M${x},${height - bottom} v${-(h - radius)} q0,${-radius} ${radius},${-radius} h${bar - 2 * radius} q${radius},0 ${radius},${radius} v${h - radius} z" fill="#2a78d6"></path>` : "";
    return `${path}<rect class="hit" x="${left + step * index}" y="${top}" width="${step}" height="${height - top - bottom}" data-tip="${esc(day.date)}: ${day.count} result${day.count === 1 ? "" : "s"} recorded"></rect>${index % every === 0 ? `<text class="axis-label" x="${x + bar / 2}" y="${height - 8}" text-anchor="middle">${esc(day.date.slice(5))}</text>` : ""}`;
  }).join("");
  const total = days.reduce((sum, day) => sum + day.count, 0);
  return `<div class="chart-box"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Executions per day, ${total} in total"><line class="grid" x1="${left}" x2="${width - right}" y1="${y(max)}" y2="${y(max)}"></line><text class="axis-label" x="${left - 6}" y="${y(max) + 4}" text-anchor="end">${max}</text><line class="axis" x1="${left}" x2="${width - right}" y1="${height - bottom}" y2="${height - bottom}"></line>${bars}</svg><div class="chart-tip" id="activityTip" role="status" hidden></div></div>
    <p class="muted panel-note">${total} result${total === 1 ? "" : "s"} recorded in this period (every save counts, including re-executions).</p>`;
}

function bindDashboard() {
  const cycle = $("#dashCycle");
  if (!cycle || !state.dashData) return;
  const d = state.dash;
  cycle.onchange = (event) => { d.cycle = event.target.value; render(); };
  $("#dashModule").onchange = (event) => { d.module = event.target.value; render(); };
  const member = $("#dashMember");
  if (member) member.onchange = (event) => { d.member = event.target.value; render(); };
  $("#dashFrom").onchange = (event) => { d.from = event.target.value; render(); };
  $("#dashTo").onchange = (event) => { d.to = event.target.value; render(); };
  $("#dashReset").onclick = () => { state.dash = { cycle: "", module: "", member: "", from: "", to: "" }; render(); };
  const trend = state.dashData.trend;
  const tip = $("#trendTip"), cross = $("#trendCross");
  document.querySelectorAll(".trend-box rect.hit").forEach((rect) => {
    rect.onmousemove = (event) => {
      const point = trend[Number(rect.dataset.index)];
      const box = rect.closest(".chart-box").getBoundingClientRect();
      const cx = Number(rect.getAttribute("x")) + Number(rect.getAttribute("width")) / 2;
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
      tip.hidden = false;
      tip.innerHTML = `<strong>${esc(point.name)}</strong>${TREND_SERIES.map(([key, label, color]) => `<span><i style="background:${color}"></i>${esc(label)}: ${point[key]}</span>`).join("")}<span>Fixed: ${point.fixed} · pass rate ${point.pass_rate}%</span>`;
      tip.style.left = `${Math.min(event.clientX - box.left + 14, box.width - 190)}px`; tip.style.top = `${Math.max(0, event.clientY - box.top - 60)}px`;
    };
    rect.onmouseleave = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); };
  });
  const atip = $("#activityTip");
  document.querySelectorAll(".dash-activity rect.hit").forEach((rect) => {
    rect.onmousemove = (event) => {
      const box = rect.closest(".chart-box").getBoundingClientRect();
      atip.hidden = false; atip.textContent = rect.dataset.tip;
      atip.style.left = `${Math.min(event.clientX - box.left + 12, box.width - 200)}px`; atip.style.top = `${event.clientY - box.top - 30}px`;
    };
    rect.onmouseleave = () => { atip.hidden = true; };
  });
}

// -------------------------------------------------------------------- reports
async function reportsPage() {
  if (!can("reports.export")) {
    return heading("Reports", "Downloadable QA reports.") + '<section class="panel"><div class="empty">Reports are available to admins. Your own progress is on <button class="link" onclick="go(\'dashboard\')">My dashboard</button> and <button class="link" onclick="go(\'workspace\')">My Work</button>.</div></section>';
  }
  const query = dashQuery();
  const scope = await api(`/reports/dashboard?${query}`);
  const scopeText = `${{ active: "Active cycles", all: "All cycles", selected: "Selected cycle", dates: "Cycles starting in the date range" }[scope.scope.basis]}: ${scope.scope.cycles.map((cycle) => cycle.name).join(", ") || "none"}${scope.scope.module ? ` · module ${scope.scope.module}` : ""}`;
  const report = (kind, title, text, count) => `<article class="report-card"><h3>${esc(title)}</h3><p class="muted">${esc(text)}</p><div class="report-foot"><span class="muted">${count}</span><a class="button small" href="/api/reports/${kind}.xlsx?${query}" download>⇩ Download Excel</a></div></article>`;
  return heading("Reports", "Excel reports built from the live workbooks. They use the same filters as the dashboard.") +
    `<section class="panel report-scope"><div><span class="detail-label">Current scope</span><strong>${esc(scopeText)}</strong></div><button class="button secondary small" onclick="go('dashboard')">Change filters on the dashboard</button></section>
    <div class="report-grid">
      ${report("execution", "Execution report", "Every test case in scope with its status, owner, who executed it and when, attempts, actual result, notes and defects.", `${scope.execution.total} test cases`)}
      ${report("modules", "Module summary", "Totals, completion, results and pass rate for every module and submodule.", `${scope.modules.length} modules`)}
      ${report("members", "Team summary", "Assigned, executed, results, open work and pass rate per member, with open workload across active cycles.", `${scope.members.length} rows`)}
      ${report("defects", "Defects report", "Every linked defect ID with its test case, module, cycle and current status.", "From linked defect IDs")}
      <article class="report-card"><h3>Cycle comparison</h3><p class="muted">Regressions, fixes, repeated failures and the full case-by-cycle matrix.</p><div class="report-foot"><span class="muted">Choose cycles first</span><button class="button small secondary" onclick="go('compare')">Open Compare Cycles</button></div></article>
      <article class="report-card"><h3>Test case repository</h3><p class="muted">All test cases with steps in the existing import/export template format.</p><div class="report-foot"><span class="muted">${scope.repository.test_cases} test cases</span><a class="button small secondary" href="/api/export/test-cases.xlsx?project_id=${encodeURIComponent(state.project.id)}" download>⇩ Download Excel</a></div></article>
    </div>`;
}

// ------------------------------------------------------------- excel import
let importJob = null;

async function importCases(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  event.target.value = "";
  if (!file.name.toLowerCase().endsWith(".xlsx")) { toast("Choose an .xlsx Excel workbook"); return; }
  if (file.size > 10 * 1024 * 1024) { toast("Excel files must be smaller than 10 MB"); return; }
  collapseRepoSettings();
  let workbook = "";
  try { workbook = await fileToBase64(file); }
  catch (error) { toast("The Excel file could not be read"); return; }
  const folder = state.folders.find((item) => item.id === state.selectedFolder);
  importJob = { name: file.name, workbook, target_folder_id: state.selectedFolder || "", target_label: folder?.path || "", sheet: "", allow_duplicates: false, create_missing_modules: false, plan: null };
  await previewImport();
}

function importPayload() {
  return { project_id: state.project.id, workbook: importJob.workbook, filename: importJob.name, target_folder_id: importJob.target_folder_id,
    sheet: importJob.sheet, allow_duplicates: importJob.allow_duplicates, create_missing_modules: importJob.create_missing_modules };
}

async function previewImport() {
  $("#app").innerHTML = heading("Import preview", importJob.name) + '<section class="panel"><div class="empty"><span class="spinner" aria-hidden="true"></span> Reading the workbook and checking every row…</div></section>';
  try { importJob.plan = await api("/import/test-cases/preview", { method: "POST", body: JSON.stringify(importPayload()) }); }
  catch (error) {
    $("#app").innerHTML = heading("Import preview", importJob.name, '<button class="button secondary" onclick="go(\'repository\')">Back to repository</button>') + `<section class="panel"><div class="notice danger">${esc(error.message)}</div></section>`;
    return;
  }
  renderImportPreview();
}

function renderImportPreview() {
  const plan = importJob.plan;
  const rec = plan.recognition;
  const s = plan.summary;
  const template = { standard: `Standard TMExpert template · ${rec.standard_columns} of ${rec.standard_total} columns`, partial: `Partly recognised · ${rec.standard_columns} of ${rec.standard_total} standard columns`, unrecognised: "Not recognised as a test case sheet" }[rec.template];
  const tone = { standard: "ok", partial: "", unrecognised: "danger" }[rec.template];
  const statusChip = (value) => `<span class="insight-chip ${value === "ready" ? "good" : value === "duplicate" ? "warning" : "critical"}">${esc({ ready: "Ready", duplicate: "Duplicate", error: "Needs fixing" }[value])}</span>`;
  const confirmLabel = s.cases ? `Import ${s.cases} test case${s.cases === 1 ? "" : "s"}` : "Import";
  const blocking = plan.errors.filter((error) => !error.duplicate).length;
  const onlyDuplicates = !plan.can_import && s.duplicates && !blocking;
  $("#app").innerHTML = heading("Import preview", `${importJob.name} · nothing has been imported yet`,
    `<div class="heading-actions"><button class="button secondary" onclick="importJob = null; go('repository')">Cancel</button><button class="button" id="confirmImport" ${plan.can_import ? "" : "disabled"}>${esc(confirmLabel)}</button></div>`) +
    `<section class="panel import-file"><div class="import-file-grid">
       <div><span class="detail-label">Sheet</span>${plan.sheets.length > 1 ? `<select id="importSheet" aria-label="Sheet">${plan.sheets.map((name) => `<option ${name === plan.sheet ? "selected" : ""}>${esc(name)}</option>`).join("")}</select>` : `<strong>${esc(plan.sheet || "—")}</strong>`}<small class="muted block">${plan.rows_read} data row${plan.rows_read === 1 ? "" : "s"}</small></div>
       <div><span class="detail-label">Template</span><span class="notice inline ${tone}">${esc(template)}</span></div>
       <div><span class="detail-label">Import into</span><strong>${plan.options.target_folder_id ? esc(plan.options.target_folder_path) : "Modules named in the Folder Path column"}</strong>${plan.options.target_folder_id ? '<small class="muted block">The module selected in the tree. Folder Path values are ignored.</small>' : ""}</div>
     </div>
     <div class="import-columns"><span class="detail-label">Columns</span>${rec.matched.map((item) => `<span class="col-chip ok">${esc(item.column)}${item.column.toLowerCase() !== item.field.toLowerCase() ? ` → ${esc(item.field)}` : ""}</span>`).join("")}${rec.ignored.map((item) => `<span class="col-chip ignored" title="${esc(item.reason)}">${esc(item.column)} · ignored</span>`).join("")}${rec.missing_required.map((column) => `<span class="col-chip missing">${esc(column)} · missing</span>`).join("")}</div>
     <div class="import-options"><label class="checkbox-filter"><input type="checkbox" id="optModules" ${importJob.create_missing_modules ? "checked" : ""} ${plan.options.target_folder_id ? "disabled" : ""}> Create modules that don't exist yet (as Draft)</label>
       <label class="checkbox-filter"><input type="checkbox" id="optDuplicates" ${importJob.allow_duplicates ? "checked" : ""}> Import duplicates anyway</label></div>
    </section>
    <div class="metrics import-metrics">
      ${[["Test cases", s.cases], ["Steps", s.steps], ["Ready", s.ready, "good"], ["Duplicates", s.duplicates, s.duplicates ? "warn" : ""], ["Errors", s.errors, s.errors ? "bad" : ""], ["New modules", s.new_modules]].map(([label, value, cls]) => `<div class="metric ${cls || ""}"><div class="metric-label">${label}</div><div class="metric-value">${value}</div></div>`).join("")}
    </div>
    ${plan.can_import ? '<div class="notice ok">Everything checks out. A backup of the repository workbooks is taken right before the import, and the file is kept in the import history.</div>' : onlyDuplicates ? '<div class="notice warn">Only duplicates are blocking this import. Remove those rows, or tick “Import duplicates anyway”.</div>' : `<div class="notice danger">Fix the ${blocking} problem${blocking === 1 ? "" : "s"} below in Excel and choose the file again. Nothing is imported while any row has an error.</div>`}
    ${plan.errors.length ? `<section class="panel"><div class="panel-head"><h2>Problems</h2><span class="muted">${plan.errors.length}</span></div><div class="table-wrap"><table><thead><tr><th>Row</th><th>Column</th><th>Problem</th></tr></thead><tbody>${plan.errors.slice(0, 300).map((error) => `<tr class="${error.duplicate ? "" : "error-row"}"><td>${error.row}</td><td>${esc(error.column || "—")}</td><td>${esc(error.message)}</td></tr>`).join("")}</tbody></table></div>${plan.errors.length > 300 ? `<p class="muted panel-note">Showing the first 300 of ${plan.errors.length}.</p>` : ""}</section>` : ""}
    ${plan.warnings.length ? `<section class="panel section-gap"><div class="panel-head"><h2>Notes</h2></div><ul class="import-notes">${plan.warnings.map((warning) => `<li>${warning.row > 1 ? `Row ${warning.row}: ` : ""}${warning.column ? `<strong>${esc(warning.column)}</strong> — ` : ""}${esc(warning.message)}</li>`).join("")}</ul></section>` : ""}
    <section class="panel section-gap"><div class="panel-head"><h2>Test cases in the file</h2><span class="muted">${s.cases}</span></div>
      <div class="table-wrap"><table class="case-table"><thead><tr><th>Row</th><th>Test case</th>${plan.recognition.has_id_column ? "<th>ID</th>" : ""}<th>Module</th><th>Steps</th><th>Priority</th><th>Status</th></tr></thead><tbody>
      ${plan.cases.map((item) => `<tr><td>${item.row}</td><td><strong>${esc(item.title)}</strong>${item.messages.length ? `<small class="block bad-text">${esc(item.messages.join(" · "))}</small>` : ""}${item.duplicate_of && item.status !== "duplicate" ? `<small class="muted block">Same title as ${esc(item.duplicate_of)}</small>` : ""}</td>${plan.recognition.has_id_column ? `<td class="case-key">${esc(item.case_key || "—")}</td>` : ""}<td>${esc(item.module_path || "—")}${item.module_new ? ' <span class="insight-chip info">new</span>' : ""}</td><td>${item.steps}</td><td>${esc(item.priority)}${item.priority_defaulted ? ' <small class="muted">(default)</small>' : ""}</td><td>${statusChip(item.status)}</td></tr>`).join("") || `<tr><td colspan="7"><div class="empty">No test cases found.</div></td></tr>`}
      </tbody></table></div>${plan.cases_truncated ? `<p class="muted panel-note">${plan.cases_truncated} more test cases are not listed here but will be imported.</p>` : ""}</section>`;
  const sheet = $("#importSheet");
  if (sheet) sheet.onchange = () => { importJob.sheet = sheet.value; previewImport(); };
  $("#optModules").onchange = (event) => { importJob.create_missing_modules = event.target.checked; previewImport(); };
  $("#optDuplicates").onchange = (event) => { importJob.allow_duplicates = event.target.checked; previewImport(); };
  $("#confirmImport").onclick = confirmImport;
}

async function confirmImport() {
  const button = $("#confirmImport");
  button.disabled = true; button.textContent = "Importing…";
  try {
    const result = await api("/import/test-cases", { method: "POST", body: JSON.stringify(importPayload()) });
    if (!result.created) {
      importJob.plan = result.preview;  // something changed since the preview (e.g. another import)
      toast("The file no longer imports cleanly; the preview has been refreshed");
      renderImportPreview();
      return;
    }
    const extra = [result.modules_created && `${result.modules_created} module${result.modules_created === 1 ? "" : "s"}`].filter(Boolean).join(" and ");
    state.importFeedback = `<div class="import-success">${result.created} test case${result.created === 1 ? "" : "s"} imported from ${esc(importJob.name)}${extra ? `, plus ${esc(extra)} created` : ""}. <button class="link" onclick="importHistoryPage()">Import history</button></div>`;
    state.highlightedCaseIds = new Set(result.case_ids || []);
    state.repoFilters = { ...state.repoFilters, q: "", status: "", priority: "", type: "", last: "" };
    importJob = null;
    toast(`${result.created} test cases imported`);
    await go("repository");
    window.setTimeout(() => { state.highlightedCaseIds.clear(); document.querySelectorAll("tr.imported-row").forEach((row) => row.classList.remove("imported-row")); }, 12000);
  } catch (error) {
    toast(error.message);
    button.disabled = false; button.textContent = "Import";
  }
}

async function importHistoryPage() {
  collapseRepoSettings();
  const { items } = await api(`/imports?project_id=${encodeURIComponent(state.project.id)}`);
  $("#app").innerHTML = heading("Import history", "Every Excel import into this project, including rejected attempts.", '<button class="button secondary" onclick="go(\'repository\')">Back to repository</button>') +
    `<section class="panel">${items.length ? `<div class="table-wrap"><table><thead><tr><th>When</th><th>File</th><th>Sheet</th><th>Result</th><th>Test cases</th><th>Steps</th><th>New modules</th><th>Problems</th><th>By</th></tr></thead><tbody>${items.map((row) => `<tr><td>${esc(fmtDate(row.imported_at))}</td>
      <td>${row.has_file ? `<a href="/api/imports/${esc(row.id)}/file" download>${esc(row.filename)}</a>` : esc(row.filename)}${row.duplicates_allowed === true ? '<small class="muted block">Duplicates allowed</small>' : ""}<small class="muted block case-key" title="SHA-256 of the uploaded file">${esc(String(row.sha256).slice(0, 12))}…</small></td>
      <td>${esc(row.sheet || "—")}</td><td><span class="badge ${row.status === "Imported" ? "approved" : "rejected"}">${esc(row.status)}</span></td><td>${esc(row.cases_created)}</td><td>${esc(row.steps_created)}</td><td>${esc(row.modules_created)}</td><td>${esc(row.error_count)}</td><td>${esc(row.imported_by_name)}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">No imports yet.</div>'}</section>`;
}

// -------------------------------------------------------------------- audit log
async function auditPage() {
  if (!can("audit.read")) { state.view = "dashboard"; return dashboard(); }
  const a = state.audit;
  const query = ["entity", "action", "actor", "date_from", "date_to", "q", "page"].filter((key) => a[key]).map((key) => `${key}=${encodeURIComponent(a[key])}`).join("&");
  const base = `project_id=${encodeURIComponent(state.project.id)}${query ? `&${query}` : ""}`;
  const [data, integrity] = await Promise.all([api(`/audit-log?${base}&page_size=50`), api("/audit-log/verify")]);
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  const select = (id, label, options, value) => `<label class="filter"><span class="visually-hidden">${esc(label)}</span><select id="${id}" aria-label="${esc(label)}"><option value="">${esc(label)}: All</option>${options.map(([optionValue, text]) => `<option value="${esc(optionValue)}" ${optionValue === value ? "selected" : ""}>${esc(text)}</option>`).join("")}</select></label>`;
  const banner = integrity.ok
    ? `<div class="notice ok"><strong>Audit trail intact.</strong> ${integrity.checked} entries verified against their checksum chain${integrity.legacy ? `; ${integrity.legacy} older entries were written before integrity tracking started` : ""}.</div>`
    : `<div class="notice danger"><strong>The audit file has been changed outside TMExpert.</strong> Entry ${integrity.broken_at.position} (${esc(integrity.broken_at.action)} ${esc(integrity.broken_at.entity)}, ${esc(fmtDate(integrity.broken_at.created_at))}) does not match the checksum chain. Restore the audit workbook from a backup and investigate.</div>`;
  const rows = data.items.map((item) => `<tr><td class="nowrap">${esc(fmtDate(item.created_at))}</td><td>${esc(item.actor_name)}</td><td>${status(item.action)}</td><td>${esc(item.entity_label)}</td>
    <td><strong>${esc(item.label)}</strong>${item.related ? `<small class="muted block">${esc(item.related)}</small>` : ""}</td>
    <td>${item.changes.length ? `<ul class="change-list">${item.changes.map((change) => `<li><span class="change-field">${esc(change.field)}</span> ${change.before ? `<span class="diff-old">${esc(change.before)}</span> → ` : ""}<span class="diff-new">${esc(change.after || "—")}</span></li>`).join("")}</ul>` : '<span class="muted">—</span>'}</td></tr>`).join("");
  return heading("Audit log", "Every change in TMExpert: who, what, when, and the values before and after.", `<a class="button secondary" href="/api/audit-log/export.xlsx?${base}" download>⇩ Export Excel</a>`) + banner +
    `<section class="panel"><div class="filter-bar" role="search">
      ${select("auditEntity", "Area", data.facets.entities.map((key) => [key, data.labels[key] || key]), a.entity)}
      ${select("auditAction", "Action", data.facets.actions.map((key) => [key, key]), a.action)}
      ${select("auditActor", "User", data.facets.actors.map((actor) => [actor.id, actor.name]), a.actor)}
      <label class="filter date-filter">From <input id="auditFrom" type="date" value="${esc(a.date_from)}" aria-label="From date"></label>
      <label class="filter date-filter">To <input id="auditTo" type="date" value="${esc(a.date_to)}" aria-label="To date"></label>
      <label class="filter search-filter"><span class="visually-hidden">Search</span><input id="auditSearch" type="search" placeholder="Search IDs and values" value="${esc(a.q)}"></label>
      <span class="muted">${data.total} entr${data.total === 1 ? "y" : "ies"}</span></div>
      <div class="table-wrap"><table class="audit-table"><thead><tr><th>When</th><th>User</th><th>Action</th><th>Area</th><th>Record</th><th>Changes</th></tr></thead><tbody>${rows || '<tr><td colspan="6"><div class="empty">No audit entries match these filters.</div></td></tr>'}</tbody></table></div>
      <div class="pager"><button class="button secondary small" ${data.page <= 1 ? "disabled" : ""} onclick="auditGo(${data.page - 1})">◀ Newer</button><span class="muted">Page ${data.page} of ${pages}</span><button class="button secondary small" ${data.page >= pages ? "disabled" : ""} onclick="auditGo(${data.page + 1})">Older ▶</button></div>
    </section>`;
}

function auditGo(page) { state.audit.page = String(page); render(); }

function bindAudit() {
  if (!$("#auditEntity")) return;
  const a = state.audit;
  const change = (key) => (event) => { a[key] = event.target.value; a.page = ""; render(); };
  $("#auditEntity").onchange = change("entity"); $("#auditAction").onchange = change("action"); $("#auditActor").onchange = change("actor");
  $("#auditFrom").onchange = change("date_from"); $("#auditTo").onchange = change("date_to");
  let timer = null;
  $("#auditSearch").oninput = (event) => { clearTimeout(timer); timer = setTimeout(() => { a.q = event.target.value; a.page = ""; render(); }, 400); };
}

// ------------------------------------------------------- history side panel
function openDrawer(title, subtitle, body) {
  closeDrawer();
  const overlay = document.createElement("div");
  overlay.className = "drawer-overlay"; overlay.id = "drawer";
  overlay.innerHTML = `<aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawerTitle"><div class="drawer-head"><div><h2 id="drawerTitle">${esc(title)}</h2>${subtitle ? `<p class="muted">${subtitle}</p>` : ""}</div><button class="icon-button" aria-label="Close" onclick="closeDrawer()">✕</button></div><div class="drawer-body">${body}</div></aside>`;
  overlay.onclick = (event) => { if (event.target === overlay) closeDrawer(); };
  document.body.append(overlay);
  document.addEventListener("keydown", drawerKey);
  overlay.querySelector(".drawer-head button").focus();
}

function drawerKey(event) { if (event.key === "Escape") closeDrawer(); }

function closeDrawer() {
  document.getElementById("drawer")?.remove();
  document.removeEventListener("keydown", drawerKey);
}

function drawerLoading(title) { openDrawer(title, "", '<div class="empty"><span class="spinner" aria-hidden="true"></span> Loading…</div>'); }

async function openCaseHistory(caseId, focusCycle = "") {
  drawerLoading("Test case history");
  let data;
  try { data = await api(`/test-cases/${encodeURIComponent(caseId)}/cycle-history`); }
  catch (error) { openDrawer("Test case history", "", `<div class="notice danger">${esc(error.message)}</div>`); return; }
  const s = data.summary;
  const cards = data.cycles.map((entry) => `<article class="hist-cycle ${entry.cycle_id === focusCycle ? "focus" : ""}" id="hc-${esc(entry.cycle_id)}">
      <div class="hist-head"><div><strong>${esc(entry.cycle_name)}</strong><small class="muted block">${esc(entry.cycle_date)}${entry.testing_type ? ` · ${esc(entry.testing_type)}` : ""}${entry.build ? ` · build ${esc(entry.build)}` : ""}${entry.environment ? ` · ${esc(entry.environment)}` : ""}${entry.cycle_status ? ` · cycle ${esc(entry.cycle_status)}` : ""}</small></div>${status(entry.status)}</div>
      <dl class="hist-facts"><div><dt>Version</dt><dd>v${esc(entry.version || "1")}</dd></div><div><dt>Assigned to</dt><dd>${esc(entry.assignee_name || "Unassigned")}</dd></div><div><dt>Last executed</dt><dd>${entry.executed_at ? `${esc(entry.executed_by_name || "—")} · ${esc(fmtDate(entry.executed_at))}` : "Not yet"}</dd></div><div><dt>Attempts</dt><dd>${esc(entry.attempt_count)}</dd></div></dl>
      ${entry.actual_result ? `<p class="prewrap"><span class="detail-label">Actual result</span>${esc(entry.actual_result)}</p>` : ""}
      ${entry.notes ? `<p class="prewrap"><span class="detail-label">Notes</span>${esc(entry.notes)}</p>` : ""}
      ${defectList(entry.defect_ids).length ? `<div class="defects">${defectLinks(entry.defect_ids)}</div>` : ""}
      ${entry.evidence.length ? `<div class="history-files">${entry.evidence.map((file) => `<a href="/api/attachments/${esc(file.id)}/file" target="_blank" rel="noopener">${esc(file.filename)}</a>`).join("")}</div>` : ""}
      ${entry.attempts.length > 1 || (entry.attempts[0]?.step_results || []).length ? `<details><summary>All ${entry.attempts.length} attempt${entry.attempts.length === 1 ? "" : "s"}</summary><ol class="history-steps">${entry.attempts.map((attempt) => `<li>${status(attempt.status)} <span class="muted">from ${esc(attempt.previous_status)} · ${esc(attempt.executed_by_name)} · ${esc(fmtDate(attempt.executed_at))}</span>${attempt.actual_result ? `<div class="prewrap">${esc(attempt.actual_result)}</div>` : ""}${(attempt.step_results || []).length ? `<div class="muted">${attempt.step_results.map((step) => `Step ${esc(step.step)}: ${esc(step.status)}`).join(" · ")}</div>` : ""}</li>`).join("")}</ol></details>` : ""}
      <div class="hist-actions"><button class="link" onclick="closeDrawer(); openExecution(${jsArg(entry.run_id)})">Open in this cycle</button></div>
    </article>`).join("");
  openDrawer(String(data.case.title || "").startsWith(data.case.case_key) ? data.case.title : `${data.case.case_key} · ${data.case.title}`, `In ${s.cycles} cycle${s.cycles === 1 ? "" : "s"} · executed ${s.executed} · ${s.passed} passed · ${s.failed} failed · ${s.blocked} blocked${s.defects.length ? ` · defects ${esc(s.defects.join(", "))}` : ""}`,
    `${cards || '<div class="empty">This test case has not been in any cycle yet.</div>'}<p class="drawer-foot"><button class="link" onclick="closeDrawer(); viewCase(${jsArg(data.case.id)})">Open the test case in the repository</button></p>`);
  if (focusCycle) document.getElementById(`hc-${focusCycle}`)?.scrollIntoView({ block: "start" });
}

function timelineHtml(data, { compact = false } = {}) {
  const events = data.events;
  if (!events.length) return '<div class="empty">Nothing has been recorded in this cycle yet.</div>';
  return `<ol class="timeline">${events.map((event) => `<li class="tl ${event.kind}"><span class="tl-dot" aria-hidden="true"></span><div>
      <div class="tl-head"><button class="link case-key" onclick="${compact ? "" : ""}openCaseHistory(${jsArg(event.case_id)}, ${jsArg(data.cycle.id)})">${esc(event.case_key || "Test case")}</button>
      ${event.kind === "result" ? `${event.previous_status ? `${status(event.previous_status)} → ` : ""}${status(event.status)}${event.attempt ? ` <span class="muted">attempt ${esc(event.attempt)}</span>` : ""}` : `<span>Assigned <strong>${esc(event.from)}</strong> → <strong>${esc(event.to)}</strong></span>`}</div>
      <small class="muted">${esc(event.by)} · ${esc(fmtDate(event.at))}${event.kind === "assignment" && event.reason ? ` · ${esc(event.reason)}` : ""}${event.source === "earlier" ? " · recorded before history tracking" : ""}</small>
      ${event.title ? `<div class="tl-title">${esc(event.title)}</div>` : ""}
      ${event.kind === "result" && event.actual_result ? `<div class="prewrap tl-text">${esc(event.actual_result)}</div>` : ""}
      ${event.kind === "result" && defectList(event.defect_ids).length ? `<div class="defects">${defectLinks(event.defect_ids)}</div>` : ""}
    </div></li>`).join("")}</ol>${data.total_events > events.length ? `<p class="muted">Showing the latest ${events.length} of ${data.total_events} events.</p>` : ""}`;
}

async function openCycleTimeline(cycleId) {
  drawerLoading("Cycle history");
  let data;
  try { data = await api(`/cycles/${encodeURIComponent(cycleId)}/timeline`); }
  catch (error) { openDrawer("Cycle history", "", `<div class="notice danger">${esc(error.message)}</div>`); return; }
  const t = data.stats, c = data.cycle;
  openDrawer(c.name, `${esc(c.testing_type || "Test cycle")} · ${esc(c.status || "Not Started")}${c.build ? ` · build ${esc(c.build)}` : ""}${c.environment ? ` · ${esc(c.environment)}` : ""}`,
    `<div class="hist-stats"><div><strong>${t.total}</strong><span>test cases</span></div><div><strong>${t.executed}</strong><span>executed (${t.completion}%)</span></div><div><strong>${t.counts.Passed}</strong><span>passed</span></div><div><strong>${t.counts.Failed}</strong><span>failed</span></div><div><strong>${t.counts.Blocked}</strong><span>blocked</span></div><div><strong>${t.executed ? `${t.pass_rate}%` : "—"}</strong><span>pass rate</span></div></div>
     <h3 class="confirm-subhead">What happened, newest first</h3>${timelineHtml(data)}
     <p class="drawer-foot"><button class="link" onclick="closeDrawer(); state.selectedCycle = ${jsArg(c.id)}; state.cycleTab = 'cases'; go('execution')">Open this cycle in Test Execution</button></p>`);
}

async function loadAssignmentHistory() {
  try {
    const data = await api(`/cycles/${encodeURIComponent(state.selectedCycle)}/timeline`);
    $("#assignHistory").innerHTML = `<div class="tab-intro muted">Every result recorded and every assignment change in this cycle. Click a test case ID for its history across all cycles.</div>${timelineHtml(data)}`;
  } catch (error) { $("#assignHistory").innerHTML = `<div class="empty">${esc(error.message)}</div>`; }
}

const PRIORITY_TONE = { Critical: "critical", High: "high", Medium: "medium", Low: "low" };

function plainTitle(testCase) {
  // Titles often start with the ID ("SUR-TC-001 – …"); the ID already has its own column.
  const key = String(testCase.case_key || "");
  const title = String(testCase.title || "");
  if (key && title.toLowerCase().startsWith(key.toLowerCase())) {
    const rest = title.slice(key.length).replace(/^\s*[–—:|-]\s*/, "").trim();
    if (rest) return rest;
  }
  return title;
}

function ago(value) {
  if (!value) return "—";
  const seconds = (Date.now() - new Date(value).getTime()) / 1000;
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(value).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

function caseRowsHtml(cases) {
  const canEdit = can("repository.write");
  const colspan = canEdit ? 8 : 7;
  if (!cases.length) {
    const filtered = state.cases.length && (state.repoFilters.q || state.repoFilters.status || state.repoFilters.priority || state.repoFilters.type || state.repoFilters.last);
    return `<tr><td colspan="${colspan}"><div class="empty repo-empty">${filtered ? 'No test cases match these filters. <button class="link" onclick="clearRepoFilters()">Clear filters</button>' : state.selectedFolder ? "No test cases in this module yet." : canEdit ? "No test cases in this project yet. Import an Excel file or create your first test case." : "No test cases in this project yet."}</div></td></tr>`;
  }
  const paths = new Map(state.folders.map((folder) => [folder.id, folder.path || folder.name]));
  const sorted = [...cases].sort((a, b) => String(paths.get(a.folder_id) || "").localeCompare(String(paths.get(b.folder_id) || "")) || String(a.case_key || "").localeCompare(String(b.case_key || ""), undefined, { numeric: true }));
  const counts = new Map();
  sorted.forEach((testCase) => counts.set(testCase.folder_id, (counts.get(testCase.folder_id) || 0) + 1));
  let group = null;
  return sorted.map((testCase) => {
    let header = "";
    if (testCase.folder_id !== group) {
      group = testCase.folder_id;
      const path = paths.get(group) || "No module";
      const parts = path.split("/").filter(Boolean);
      header = `<tr class="section-row"><td colspan="${colspan}"><span class="section-icon" aria-hidden="true">▰</span>${parts.length > 1 ? `<span class="section-parent">${esc(parts.slice(0, -1).join(" / "))} / </span>` : ""}<strong>${esc(parts.at(-1) || path)}</strong><span class="section-count">${counts.get(group)}</span></td></tr>`;
    }
    const last = testCase.last_status || "Not Run";
    const tags = Array.isArray(testCase.tags) ? testCase.tags : String(testCase.tags || "").split(",").map((tag) => tag.trim()).filter(Boolean);
    const priority = testCase.priority || "";
    return `${header}<tr data-case-row="${esc(testCase.id)}" class="case-row ${state.highlightedCaseIds.has(testCase.id) ? "imported-row" : ""} ${state.selectedCaseIds.has(testCase.id) ? "row-selected" : ""}">
      ${canEdit ? `<td class="check-col"><input type="checkbox" class="case-check" data-check-case="${esc(testCase.id)}" ${state.selectedCaseIds.has(testCase.id) ? "checked" : ""} aria-label="Select ${esc(testCase.title)}"></td>` : ""}
      <td class="case-id-cell"><button class="case-id" onclick="viewCase(${jsArg(testCase.id)})">${esc(caseLabel(testCase))}</button></td>
      <td class="case-title-cell"><button class="case-title" title="${esc(testCase.title)}" onclick="viewCase(${jsArg(testCase.id)})">${esc(plainTitle(testCase))}</button><span class="title-meta">${esc(testCase.test_type || "")}${testCase.test_type ? " · " : ""}v${esc(testCase.version)}</span>${tags.length ? `<span class="tag-list">${tags.slice(0, 4).map((tag) => `<span class="tag">${esc(tag)}</span>`).join("")}${tags.length > 4 ? `<span class="tag more">+${tags.length - 4}</span>` : ""}</span>` : ""}</td>
      <td>${priority ? `<span class="priority ${PRIORITY_TONE[priority] || ""}"><i aria-hidden="true"></i>${esc(priority)}</span>` : '<span class="muted">—</span>'}</td>
      <td class="type-cell">${esc(testCase.test_type || "—")}</td>
      <td>${canEdit ? `<select class="last-status-select status-${esc(last.toLowerCase().replaceAll(" ", "-"))}" aria-label="Last result for ${esc(testCase.title)}" data-last-status-case="${esc(testCase.id)}" data-previous-status="${esc(last)}">${RUN_STATES.map((item) => `<option ${item === last ? "selected" : ""}>${item}</option>`).join("")}</select>` : status(last)}</td>
      <td class="num-cell">v${esc(testCase.version)}</td>
      <td class="date-cell" title="${esc(fmtDate(testCase.updated_at))}">${esc(ago(testCase.updated_at))}</td>
    </tr>`;
  }).join("");
}

async function repository() {
  const projectId = encodeURIComponent(state.project.id);
  [state.folders, state.cases] = await Promise.all([
    apiAll(`/folders?project_id=${projectId}`),
    apiAll(`/test_cases?project_id=${projectId}`),
  ]);
  const known = new Set(state.cases.map((testCase) => testCase.id));
  state.selectedCaseIds = new Set([...state.selectedCaseIds].filter((id) => known.has(id)));
  if (state.selectedFolder && !state.folders.some((folder) => folder.id === state.selectedFolder)) state.selectedFolder = "";
  const canEdit = can("repository.write");
  const f = state.repoFilters;
  const types = [...new Set(["Manual", "Automated", ...state.cases.map((testCase) => testCase.test_type).filter(Boolean)])];
  const selectedFolder = state.folders.find((folder) => folder.id === state.selectedFolder);
  const collapsed = state.repoSidebarCollapsed;
  const visible = filteredCases();
  const filtersOn = Boolean(f.q || f.status || f.priority || f.type || f.last);
  const crumbs = selectedFolder ? (selectedFolder.path || selectedFolder.name).split("/").filter(Boolean) : [];
  const actions = canEdit ? `<div class="repo-actions">
      <button class="button secondary small" onclick="triggerCaseImport()">⇪ Import</button>
      <div class="repo-settings"><button type="button" class="button secondary small repo-settings-btn ${state.repoSettingsOpen ? "open" : ""}" aria-haspopup="menu" aria-expanded="${state.repoSettingsOpen ? "true" : "false"}" aria-controls="repoSettingsMenu" onclick="toggleRepoSettings(event)">⇩ Export ▾</button>
        <div id="repoSettingsMenu" class="repo-settings-menu" role="menu" ${state.repoSettingsOpen ? "" : "hidden"}><a class="folder-menu-item" href="/api/export/test-cases.xlsx?project_id=${projectId}" download="TMExpert_Test_Cases.xlsx" onclick="collapseRepoSettings()">Export to Excel</a><a class="folder-menu-item" href="/api/export/test-cases.xlsx?project_id=${projectId}&include_ids=true" download="TMExpert_Test_Cases_with_IDs.xlsx" onclick="collapseRepoSettings()">Export to Excel with test case IDs</a><a class="folder-menu-item" href="/api/test-case-import-template.xlsx?project_id=${projectId}" download="TMExpert_Test_Case_Import_Template.xlsx" onclick="collapseRepoSettings()">Download import template</a><button type="button" class="folder-menu-item" onclick="importHistoryPage()">Import history</button></div>
        <input id="caseImport" class="visually-hidden" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></div>
      <button class="button secondary small" onclick="newFolder()">+ Module</button>
      <button class="button small" onclick="newCase()">+ Test case</button></div>` : "";
  return `<div class="repo-head">
      <div><nav class="crumbs" aria-label="Breadcrumb"><span>${esc(state.project.key)}</span><span aria-hidden="true">›</span><button class="link-plain" onclick="state.selectedFolder=''; render()">Test Repository</button>${crumbs.map((part) => `<span aria-hidden="true">›</span><span>${esc(part)}</span>`).join("")}</nav>
        <h1>${esc(selectedFolder ? selectedFolder.name : "Test Repository")}</h1>
        <p class="repo-stats"><span><strong>${state.cases.length}</strong> test cases</span><span><strong>${state.folders.length}</strong> modules</span></p></div>
      ${actions}</div>
    <div class="repo repo-v2 ${collapsed ? "repo-collapsed" : ""}">
      ${collapsed ? "" : `<aside class="panel tree"><div class="tree-header"><strong>Modules</strong><div class="tree-actions">${canEdit ? '<button class="icon-button" title="Add module" aria-label="Add module" onclick="newFolder()">+</button>' : ""}<button class="icon-button" title="Hide modules" aria-label="Hide modules" onclick="toggleRepoSidebar()">◀</button></div></div>
        <div class="tree-search"><input id="moduleSearch" type="search" placeholder="Filter modules" aria-label="Filter modules" value="${esc(state.moduleQuery || "")}"></div>
        <div class="tree-content">${folderTree(state.folders, state.cases)}</div></aside>`}
      <section class="panel repository-cases">
        <div class="filter-bar repo-filters" role="search">
          ${collapsed ? `<button class="icon-button" title="Show modules" aria-label="Show modules" onclick="toggleRepoSidebar()">▶</button>` : ""}
          <label class="filter search-filter"><span class="visually-hidden">Search test cases</span><input id="repoSearch" type="search" placeholder="Search ID, title or tag…" value="${esc(f.q)}"></label>
          ${filterSelect("repoPriority", "Priority", PRIORITIES, f.priority)}
          ${filterSelect("repoType", "Type", types, f.type)}
          ${filterSelect("repoLast", "Last result", RUN_STATES, f.last)}
          <label class="filter checkbox-filter"><input id="repoIncludeSub" type="checkbox" aria-label="Include submodules" ${f.includeSub ? "checked" : ""}> Submodules</label>
          <span class="repo-count"><strong id="repoCount">${visible.length}</strong> of ${state.cases.length}${filtersOn ? ' · <button class="link" onclick="clearRepoFilters()">Clear</button>' : ""}</span>
        </div>
        <div id="importFeedback" class="import-feedback" aria-live="polite">${state.importing ? `<div class="import-loading"><span class="spinner" aria-hidden="true"></span><span>Importing “${esc(state.importingFile)}” — reading Excel and adding cases. Please wait…</span></div>` : state.importFeedback}</div>
        <div class="table-wrap repo-table-wrap"><table class="case-table repo-table"><thead><tr>${canEdit ? '<th class="check-col"><input type="checkbox" id="selectAllCases" aria-label="Select all visible cases"></th>' : ""}<th class="id-col">ID</th><th>Title</th><th class="pri-col">Priority</th><th class="type-col">Type</th><th class="status-col">Last result</th><th class="num-col">Ver.</th><th class="date-col">Updated</th></tr></thead>
          <tbody id="caseRows">${caseRowsHtml(visible)}</tbody>
        </table></div>
        ${canEdit ? `<div id="bulkBar" class="bulk-bar" role="toolbar" aria-label="Bulk actions" hidden></div>` : ""}
      </section>
    </div>`;
}

function bindRepositoryExtras() {
  const search = $("#moduleSearch");
  if (search) {
    const apply = () => {
      state.moduleQuery = search.value;
      const needle = search.value.trim().toLowerCase();
      const byId = new Map(state.folders.map((folder) => [folder.id, folder]));
      const keep = new Set();
      state.folders.forEach((folder) => {
        if (needle && !String(folder.name).toLowerCase().includes(needle)) return;
        for (let current = folder; current; current = byId.get(current.parent_id)) keep.add(current.id);
      });
      document.querySelectorAll(".tree .tree-branch").forEach((branch) => {
        const id = branch.querySelector(".tree-node[data-folder]")?.dataset.folder;
        branch.hidden = Boolean(needle) && !keep.has(id);
      });
    };
    search.oninput = apply;
    if (search.value) apply();
  }
  document.querySelectorAll("tr.case-row[data-case-row]").forEach((row) => {  // repository rows only
    row.onclick = (event) => {
      if (event.target.closest("button, select, input, a, label")) return;
      viewCase(row.dataset.caseRow);
    };
  });
}

// Status order puts Blocked between Passed and Failed (easier to tell apart for colour-blind users).
const RUN_SEGMENTS = [["Passed", "pass"], ["Blocked", "block"], ["Failed", "fail"], ["Not Applicable", "na"], ["In Progress", "prog"], ["Not Run", "open"]];

function runCounts(runs) {
  const counts = Object.fromEntries(RUN_STATES.map((item) => [item, 0]));
  runs.forEach((run) => { counts[run.status || "Not Run"] = (counts[run.status || "Not Run"] || 0) + 1; });
  return counts;
}

function stackedBar(counts, total, { thin = false, label = "Results" } = {}) {
  const parts = RUN_SEGMENTS.filter(([key]) => counts[key]).map(([key, cls]) => `<span class="seg ${cls}" style="flex-grow:${counts[key]}" title="${esc(key)}: ${counts[key]}"></span>`).join("");
  return `<div class="stack ${thin ? "thin" : ""}" role="img" aria-label="${esc(label)}: ${RUN_SEGMENTS.map(([key]) => `${counts[key] || 0} ${key}`).join(", ")} of ${total}">${parts || '<span class="seg open" style="flex-grow:1"></span>'}</div>`;
}

function progressLegend(counts) {
  return `<ul class="stack-legend">${RUN_SEGMENTS.map(([key, cls]) => `<li><i class="dot ${cls}" aria-hidden="true"></i>${esc(key)} <strong>${counts[key] || 0}</strong></li>`).join("")}</ul>`;
}

function shortModule(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  return parts.length ? `<span class="module-short" title="${esc(path)}">${parts.length > 1 ? '<span class="muted">… / </span>' : ""}${esc(parts.at(-1))}</span>` : '<span class="muted">—</span>';
}

function priorityTag(priority) {
  return priority ? `<span class="priority ${PRIORITY_TONE[priority] || ""}"><i aria-hidden="true"></i>${esc(priority)}</span>` : '<span class="muted">—</span>';
}

async function execution() {
  const projectId = encodeURIComponent(state.project.id);
  const [runs, cycles, cases, plans] = await Promise.all([
    apiAll(`/test_runs?project_id=${projectId}`),
    apiAll(`/test_cycles?project_id=${projectId}`),
    apiAll(`/test_cases?project_id=${projectId}`),
    apiAll(`/test_plans?project_id=${projectId}`),
  ]);
  state.planNames = Object.fromEntries(plans.map((plan) => [plan.id, plan.name]));
  const manage = can("cycles.write");
  if (state.selectedCycle && !cycles.some((cycle) => cycle.id === state.selectedCycle)) state.selectedCycle = "";
  const caseById = new Map(cases.map((testCase) => [testCase.id, testCase]));
  const cycleById = new Map(cycles.map((cycle) => [cycle.id, cycle]));
  state.cycleNames = Object.fromEntries(cycles.map((item) => [item.id, item.name]));
  const cycle = cycleById.get(state.selectedCycle);
  let summary = null;
  cycleData = null;
  if (cycle) [summary, cycleData] = await Promise.all([api(`/cycles/${encodeURIComponent(cycle.id)}/summary`), api(`/cycles/${encodeURIComponent(cycle.id)}/assignments`)]);
  const ordered = [...cycles].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const allCounts = runCounts(runs);
  const cycleItems = ordered.map((item) => {
    const mineRuns = runs.filter((run) => run.cycle_id === item.id);
    const counts = runCounts(mineRuns);
    const executed = mineRuns.length - counts["Not Run"] - counts["In Progress"];
    const mine = mineRuns.filter((run) => run.assignee_id === state.user.id && ["Not Run", "In Progress"].includes(run.status || "Not Run")).length;
    return `<button type="button" class="cycle-item ${state.selectedCycle === item.id ? "selected" : ""}" data-cycle="${esc(item.id)}" aria-label="${esc(item.name)}, ${executed} of ${mineRuns.length} executed${mine ? `, ${mine} open for you` : ""}">
        <span class="cycle-item-top"><span class="cycle-item-name" title="${esc(item.name)}">${esc(item.name)}</span>${mine ? `<span class="module-chip mine">${mine} mine</span>` : ""}</span>
        <span class="cycle-item-meta">${status(item.status || "Not Started")}${item.testing_type ? `<span>${esc(item.testing_type)}</span>` : ""}<span class="cycle-item-count">${executed}/${mineRuns.length}</span></span>
        ${stackedBar(counts, mineRuns.length, { thin: true, label: item.name })}
      </button>`;
  }).join("");
  const sidebar = `<aside class="panel cycle-list"><div class="tree-header"><strong>Test cycles</strong><div class="tree-actions">${manage ? '<button class="icon-button" title="Create test cycle" aria-label="Create test cycle" onclick="cycleWizard()">+</button>' : ""}<button class="icon-button" title="Hide cycles" aria-label="Hide cycles" onclick="toggleExecutionSidebar()">◀</button></div></div>
      <div class="cycle-items"><button type="button" class="cycle-item all ${state.selectedCycle ? "" : "selected"}" data-cycle="" aria-label="All test runs, ${runs.length}"><span class="cycle-item-top"><span class="cycle-item-name">All test runs</span><span class="cycle-item-count">${runs.length}</span></span><span class="cycle-item-meta"><span>${cycles.length} cycle${cycles.length === 1 ? "" : "s"}</span></span></button>
      ${cycleItems || `<div class="tree-empty">No cycles yet.${manage ? " Create one to start testing." : ""}</div>`}</div></aside>`;
  const collapsed = state.executionSidebarCollapsed;
  let main;
  if (cycle) {
    main = cycleOverview(cycle, summary, manage) + cycleTabs(cycle);
  } else {
    const sorted = [...runs].sort((a, b) => String(a.case_key || "").localeCompare(String(b.case_key || ""), undefined, { numeric: true }));
    const executed = runs.length - allCounts["Not Run"] - allCounts["In Progress"];
    const rows = sorted.map((run) => {
      const testCase = caseById.get(run.test_case_id);
      const key = run.case_key || (testCase ? caseLabel(testCase) : "—");
      const title = plainTitle({ case_key: key, title: run.case_title || testCase?.title || "Test case not visible to you" });
      return `<tr><td class="case-id-cell"><button class="case-id" onclick="openExecution(${jsArg(run.id)})">${esc(key)}</button></td><td class="case-title-cell"><button class="case-title" onclick="openExecution(${jsArg(run.id)})">${esc(title)}</button></td><td>${esc(cycleById.get(run.cycle_id)?.name || "—")}</td><td>${ownerChip(run)}</td><td>${runStatusSelect(run, key)}</td></tr>`;
    }).join("");
    main = `<section class="panel run-summary"><div class="run-summary-head"><div><h2>All test runs</h2><p class="muted">${runs.length} test case${runs.length === 1 ? "" : "s"} across ${cycles.length} cycle${cycles.length === 1 ? "" : "s"}. Select a cycle on the left to assign and execute.</p></div>
        <div class="kpis"><div><strong>${runs.length ? Math.round(executed * 100 / runs.length) : 0}%</strong><span>executed</span></div><div><strong>${executed}</strong><span>of ${runs.length}</span></div></div></div>
        ${stackedBar(allCounts, runs.length, { label: "All test runs" })}${progressLegend(allCounts)}</section>
      <section class="panel"><div class="table-wrap"><table class="case-table exec-table"><thead><tr><th class="id-col">ID</th><th>Test case</th><th>Cycle</th><th>Assigned to</th><th class="status-col">Status</th></tr></thead><tbody>${rows || `<tr><td colspan="5"><div class="empty">${manage ? (cases.length ? 'No test cycles yet. <button class="link" onclick="cycleWizard()">Create a test cycle</button> to start testing.' : "Add and approve test cases in the repository, then create a test cycle.") : "No test cycles yet. Your admin creates cycles from the repository."}</div></td></tr>`}</tbody></table></div></section>`;
  }
  return `<div class="repo-head"><div><nav class="crumbs" aria-label="Breadcrumb"><span>${esc(state.project.key)}</span><span aria-hidden="true">›</span><button class="link-plain" onclick="state.selectedCycle=''; render()">Test Execution</button>${cycle ? `<span aria-hidden="true">›</span><span>${esc(cycle.name)}</span>` : ""}</nav><h1>Test Execution</h1></div>
      ${manage ? '<div class="repo-actions"><button class="button small" onclick="cycleWizard()">+ Create test cycle</button></div>' : ""}</div>
    <div class="exec-v2 ${collapsed ? "collapsed" : ""}">${collapsed ? `<button class="icon-button show-cycles" title="Show cycles" aria-label="Show cycles" onclick="toggleExecutionSidebar()">▶</button>` : sidebar}<div class="execution-main">${main}</div></div>`;
}

function cycleOverview(cycle, summary, manage) {
  const counts = summary.counts;
  const meta = [cycle.environment && ["Environment", cycle.environment], cycle.build && ["Build", cycle.build], (cycle.start_date || cycle.end_date) && ["Dates", `${cycle.start_date || "?"} → ${cycle.end_date || "?"}`]].filter(Boolean);
  const planLink = cycle.plan_id && state.planNames?.[cycle.plan_id] ? `<span><span class="muted">Plan</span> <button class="link" onclick="openPlan(${jsArg(cycle.plan_id)})">${esc(state.planNames[cycle.plan_id])}</button></span>` : `<span class="muted">No plan${manage ? ` · <button class="link" onclick="editCycle(${jsArg(cycle.id)})">Add to a plan</button>` : ""}</span>`;
  const sourceLink = cycle.source_cycle_id ? `<span><span class="muted">Retest of</span> <button class="link" onclick="openCycle(${jsArg(cycle.source_cycle_id)})">${esc(state.cycleNames?.[cycle.source_cycle_id] || "previous cycle")}</button></span>` : "";
  const retestable = (counts.Failed || 0) + (counts.Blocked || 0);
  return `<section class="panel cycle-head">
      <div class="cycle-head-top"><div class="cycle-title"><h2>${esc(cycle.name)}</h2>${cycle.testing_type ? `<span class="type-pill">${esc(cycle.testing_type)}</span>` : ""}${manage ? `<select class="cycle-status" aria-label="Cycle status" onchange="setCycleStatus(${jsArg(cycle.id)}, this)">${["Not Started", "In Progress", "Completed"].map((item) => `<option ${item === (cycle.status || "Not Started") ? "selected" : ""}>${item}</option>`).join("")}</select>` : status(cycle.status || "Not Started")}</div>
        ${manage ? `<div class="heading-actions">${retestable ? `<button class="button secondary small" onclick="retestCycle(${jsArg(cycle.id)})" title="Create a Retesting cycle from failed and blocked results">↻ Retest ${retestable}</button>` : ""}<button class="button secondary small" onclick="editCycle(${jsArg(cycle.id)})">Edit details</button>${cycle.status === "Completed" ? "" : `<button class="button small" onclick="cycleWizard(${jsArg(cycle.id)})">+ Add test cases</button>`}</div>` : ""}</div>
      <p class="cycle-meta">${planLink}${sourceLink}${meta.length ? meta.map(([label, value]) => `<span><span class="muted">${label}</span> ${esc(value)}</span>`).join("") : `<span class="muted">No environment, build or dates set${manage ? ' · <button class="link" onclick="editCycle(' + esc(JSON.stringify(cycle.id)) + ')">Add them</button>' : ""}</span>`}</p>
      ${cycle.description ? `<p class="prewrap cycle-desc">${esc(cycle.description)}</p>` : ""}
      <div class="kpis">
        <div><strong>${summary.completion}%</strong><span>complete · ${summary.executed} of ${summary.total} executed</span></div>
        <div><strong>${summary.executed ? `${summary.pass_rate}%` : "—"}</strong><span>pass rate</span></div>
        <div><strong id="statUnassigned" class="${cycleData.unassigned ? "warn-text" : ""}">${cycleData.unassigned}</strong><span>unassigned</span></div>
      </div>
      ${stackedBar(counts, summary.total, { label: cycle.name })}${progressLegend(counts)}
    </section>`;
}

function cycleRunRows() {
  const manage = can("assignments.manage");
  const removable = can("cycles.write");
  const rows = filteredCycleRuns();
  state.wsList = rows.map((run) => run.id);
  if (!rows.length) return `<tr><td colspan="${manage ? 9 : 7}"><div class="empty">${cycleData.runs.length ? "No test cases match these filters." : "This cycle has no test cases yet."}</div></td></tr>`;
  return rows.map((run) => `<tr class="${state.runSelection.has(run.id) ? "row-selected" : ""}">
    ${manage ? `<td class="check-col"><input type="checkbox" class="run-check" data-run="${esc(run.id)}" ${state.runSelection.has(run.id) ? "checked" : ""} aria-label="Select ${esc(run.case_key)}"></td>` : ""}
    <td class="case-id-cell"><button class="case-id" onclick="openExecution(${jsArg(run.id)})">${esc(run.case_key || "—")}</button></td>
    <td class="case-title-cell"><button class="case-title" title="${esc(run.case_title || "")}" onclick="openExecution(${jsArg(run.id)})">${esc(plainTitle({ case_key: run.case_key, title: run.case_title || "—" }))}</button></td>
    <td>${shortModule(run.module_path)}</td><td>${priorityTag(run.priority)}</td><td>${ownerChip(run)}</td><td>${runStatusSelect(run, run.case_key)}</td>
    ${manage ? `<td class="row-actions">${removable && (run.status || "Not Run") === "Not Run" ? `<button class="icon-button remove-run" title="Remove from this cycle" aria-label="Remove ${esc(run.case_key)} from this cycle" onclick="removeRun(${jsArg(run.id)}, ${jsArg(run.case_key)})">✕</button>` : ""}</td>` : ""}
  </tr>`).join("");
}

// ------------------------------------------------------------------ test plans
const PLAN_STATUSES = ["Draft", "Active", "Completed"];
const CRITERIA_FIELDS = [["min_execution_pct", "Planned test cases executed", "%", "≥"], ["min_pass_rate", "Pass rate of executed", "%", "≥"], ["max_open_critical", "Open Critical defects", "", "≤"], ["max_open_high", "Open High defects", "", "≤"], ["max_blocked", "Blocked test cases", "", "≤"]];

function verdictBadge(verdict) {
  const tone = { Ready: "approved", "Not ready": "failed", "Not started": "not-run", "No test cases": "not-run" }[verdict] || "not-run";
  return `<span class="badge ${tone} verdict">${esc(verdict)}</span>`;
}

async function plansPage() {
  if (state.planId) return planDetail(state.planId);
  const { items } = await api(`/plans?project_id=${encodeURIComponent(state.project.id)}`);
  const manage = can("plans.write");
  const rows = items.map((plan) => `<tr class="case-row" data-plan="${esc(plan.id)}">
      <td><button class="case-title" onclick="openPlan(${jsArg(plan.id)})">${esc(plan.name)}</button>${plan.release ? `<span class="type-pill">Release ${esc(plan.release)}</span>` : ""}<small class="muted block">${esc([plan.start_date, plan.end_date].filter(Boolean).join(" → ") || "No dates")}</small></td>
      <td>${status(plan.status || "Draft")}</td><td class="num">${plan.cycles}</td>
      <td class="plan-progress">${stackedBar(plan.counts, plan.planned, { thin: true, label: plan.name })}<small>${plan.executed} of ${plan.planned} executed · ${plan.execution_pct}%</small></td>
      <td>${plan.executed ? `${plan.pass_rate}%` : "—"}</td><td class="${plan.open_defects ? "bad-text" : ""}">${plan.open_defects}</td><td>${verdictBadge(plan.verdict)}</td></tr>`).join("");
  return `<div class="repo-head"><div><nav class="crumbs"><span>${esc(state.project.key)}</span><span aria-hidden="true">›</span><span>Test Plans</span></nav><h1>Test Plans</h1>
      <p class="repo-stats"><span>A plan is one release or sprint: its cycles, scope, progress and readiness.</span></p></div>
      ${manage ? '<div class="repo-actions"><button class="button small" onclick="editPlan()">+ New test plan</button></div>' : ""}</div>
    <section class="panel">${items.length ? `<div class="table-wrap"><table class="case-table plans-table"><thead><tr><th>Plan</th><th>Status</th><th class="num">Cycles</th><th>Progress (latest result per test case)</th><th>Pass rate</th><th>Open defects</th><th>Readiness</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="empty">No test plans yet.${manage ? ' <button class="link" onclick="editPlan()">Create the first plan</button> for your next release or sprint.' : ""}</div>`}</section>`;
}

function openPlan(planId) { state.planId = planId; state.keepPlan = true; go("plans"); }

async function editPlan(planId = "") {
  const plan = planId ? (await api(`/plans/${encodeURIComponent(planId)}/report`)).plan : { status: "Draft", exit_criteria: { min_execution_pct: 100, min_pass_rate: 95, max_open_critical: 0, max_open_high: 0, max_blocked: 0 } };
  const criteria = plan.exit_criteria || {};
  const values = await openDialog({ title: planId ? "Edit test plan" : "New test plan", confirmLabel: planId ? "Save" : "Create plan", body: `<div class="form-grid">
      <div class="field full"><label>Plan name *</label><input name="name" required maxlength="150" value="${esc(plan.name || "")}" placeholder="e.g. Release 2.5"></div>
      <div class="field"><label>Release / sprint</label><input name="release" maxlength="80" value="${esc(plan.release || "")}" placeholder="2.5"></div>
      <div class="field"><label>Status</label><select name="status">${PLAN_STATUSES.map((item) => `<option ${item === (plan.status || "Draft") ? "selected" : ""}>${item}</option>`).join("")}</select></div>
      <div class="field"><label>Start date</label><input name="start_date" type="date" value="${esc(plan.start_date || "")}"></div>
      <div class="field"><label>End date</label><input name="end_date" type="date" value="${esc(plan.end_date || "")}"></div>
      <div class="field full"><label>Scope</label><textarea name="scope" maxlength="4000" placeholder="What is in and out of scope for this release">${esc(plan.scope || "")}</textarea></div>
      <fieldset class="field full criteria"><legend>Exit criteria — when is this release ready?</legend>${CRITERIA_FIELDS.map(([key, label, unit, op]) => `<label class="criterion">${esc(label)} <span class="muted">${op}</span><input name="crit_${key}" type="number" min="0" ${unit ? 'max="100"' : ""} step="1" value="${esc(criteria[key] ?? "")}">${unit}</label>`).join("")}</fieldset>
    </div>` });
  if (!values) return;
  const payload = { name: values.name, release: values.release, status: values.status, start_date: values.start_date, end_date: values.end_date, scope: values.scope,
    exit_criteria: Object.fromEntries(CRITERIA_FIELDS.map(([key]) => [key, Number(values[`crit_${key}`] || 0)])) };
  try {
    const saved = planId ? await api(`/test_plans/${encodeURIComponent(planId)}`, { method: "PUT", body: JSON.stringify(payload) })
      : await api("/plans", { method: "POST", body: JSON.stringify({ project_id: state.project.id, ...payload }) });
    toast(planId ? "Plan updated" : `${saved.name} created`);
    state.planId = saved.id; state.keepPlan = true; await go("plans");
  } catch (error) { toast(error.message); }
}

async function planDetail(planId) {
  let report;
  try { report = await api(`/plans/${encodeURIComponent(planId)}/report`); }
  catch (error) { state.planId = ""; return plansPage(); }
  const p = report.plan, manage = can("plans.write"), cycles = can("cycles.write");
  const pct = (part, whole) => (whole ? Math.round(part * 100 / whole) : 0);
  const cycleRows = report.cycles.map((cycle) => `<tr class="case-row"><td><button class="case-title" onclick="openCycle(${jsArg(cycle.id)})">${esc(cycle.name)}</button>${cycle.source_cycle_id ? '<small class="muted block">Retest cycle</small>' : ""}</td>
      <td><span class="type-pill">${esc(cycle.testing_type || "—")}</span></td><td>${esc(cycle.build || "—")}</td><td>${status(cycle.status || "Not Started")}</td>
      <td class="plan-progress">${stackedBar(cycle.counts, cycle.total, { thin: true, label: cycle.name })}<small>${cycle.executed}/${cycle.total} · ${cycle.completion}%</small></td><td>${cycle.executed ? `${cycle.pass_rate}%` : "—"}</td></tr>`).join("");
  const people = report.testers.map((t) => `<tr><td>${esc(t.name)}</td><td class="num">${t.total}</td><td class="plan-progress">${progressBar(pct(t.executed, t.total), t.name)}<small>${t.executed}/${t.total}</small></td><td class="num">${t.passed}</td><td class="num ${t.failed ? "bad-text" : ""}">${t.failed}</td><td class="num ${t.blocked ? "warn-text" : ""}">${t.blocked}</td></tr>`).join("");
  const modules = report.modules.map((m) => `<tr><td>${shortModule(m.path)}</td><td class="num">${m.total}</td><td class="plan-progress">${progressBar(pct(m.executed, m.total), m.path)}<small>${m.executed}/${m.total}</small></td><td class="num">${m.passed}</td><td class="num ${m.failed ? "bad-text" : ""}">${m.failed}</td><td class="num ${m.blocked ? "warn-text" : ""}">${m.blocked}</td></tr>`).join("");
  const openDefects = report.defects.items.filter((d) => d.is_open);
  const defectRows = report.defects.items.map((d) => `<tr class="case-row" onclick="openDefect(${jsArg(d.id)})"><td class="case-id-cell"><span class="case-id">${esc(d.key)}</span></td><td>${esc(d.summary)}</td><td>${severityTag(d.severity)}</td><td>${status(d.status)}</td><td>${d.retest ? esc(d.retest) : '<span class="muted">—</span>'}</td></tr>`).join("");
  const r = report.readiness;
  return `<div class="repo-head"><div><nav class="crumbs"><span>${esc(state.project.key)}</span><span aria-hidden="true">›</span><button class="link-plain" onclick="state.planId=''; render()">Test Plans</button><span aria-hidden="true">›</span><span>${esc(p.name)}</span></nav>
      <h1>${esc(p.name)} ${p.release ? `<span class="type-pill">Release ${esc(p.release)}</span>` : ""} ${status(p.status || "Draft")}</h1>
      <p class="repo-stats"><span>${esc([p.start_date, p.end_date].filter(Boolean).join(" → ") || "No dates set")}</span><span><strong>${report.cycles.length}</strong> cycles</span><span><strong>${report.planned}</strong> test cases planned</span></p></div>
      <div class="repo-actions">${can("reports.export") ? `<a class="button secondary small" href="/api/plans/${esc(p.id)}/export.xlsx" download>⇩ Export Excel</a>` : ""}${manage ? `<button class="button secondary small" onclick="editPlan(${jsArg(p.id)})">Edit plan</button>` : ""}${cycles ? `<button class="button small" onclick="cycleWizard('', { planId: ${esc(JSON.stringify(p.id))} })">+ Create cycle in this plan</button>` : ""}</div></div>
    ${p.scope ? `<section class="panel plan-scope"><span class="detail-label">Scope</span><p class="prewrap">${esc(p.scope)}</p></section>` : ""}
    <div class="plan-top">
      <section class="panel readiness ${r.verdict === "Ready" ? "ready" : r.verdict === "Not ready" ? "not-ready" : ""}"><div class="readiness-head"><div><span class="detail-label">Release readiness</span><strong class="verdict-text">${esc(r.verdict)}</strong></div>${manage ? `<button class="link" onclick="editPlan(${jsArg(p.id)})">Edit exit criteria</button>` : ""}</div>
        <table class="criteria-table"><tbody>${r.checks.map((check) => `<tr><td><span class="check ${check.ok ? "ok" : "no"}" aria-label="${check.ok ? "Met" : "Not met"}">${check.ok ? "✓" : "✗"}</span></td><td>${esc(check.label)}</td><td class="muted">${esc(check.target)}</td><td><strong>${esc(check.actual)}</strong></td></tr>`).join("")}</tbody></table></section>
      <section class="panel cycle-head"><div class="kpis"><div><strong>${report.execution_pct}%</strong><span>executed · ${report.executed} of ${report.planned}</span></div><div><strong>${report.executed ? `${report.pass_rate}%` : "—"}</strong><span>pass rate</span></div><div><strong class="${openDefects.length ? "bad-text" : ""}">${openDefects.length}</strong><span>open defects</span></div></div>
        ${stackedBar(report.counts, report.planned, { label: p.name })}${progressLegend(report.counts)}<p class="muted panel-foot">Each test case counts once, with its latest result across all cycles in this plan.</p></section>
    </div>
    <section class="panel section-gap"><div class="panel-head"><h2>Cycles</h2>${cycles ? `<button class="button secondary small" onclick="cycleWizard('', { planId: ${esc(JSON.stringify(p.id))} })">+ Create cycle</button>` : ""}</div>
      ${report.cycles.length ? `<div class="table-wrap"><table class="case-table exec-table"><thead><tr><th>Cycle</th><th>Type</th><th>Build</th><th>Status</th><th>Progress</th><th>Pass rate</th></tr></thead><tbody>${cycleRows}</tbody></table></div>` : '<div class="empty">No cycles in this plan yet. Create a Smoke, Sanity or Regression cycle to start.</div>'}</section>
    <div class="dash-grid two section-gap">
      <section class="panel"><div class="panel-head"><h2>Testers</h2></div>${people ? `<div class="table-wrap"><table class="progress-table"><thead><tr><th>Tester</th><th class="num">Assigned</th><th>Executed</th><th class="num">Passed</th><th class="num">Failed</th><th class="num">Blocked</th></tr></thead><tbody>${people}</tbody></table></div>` : '<div class="empty">No assignments yet.</div>'}</section>
      <section class="panel"><div class="panel-head"><h2>Modules</h2></div>${modules ? `<div class="table-wrap"><table class="progress-table"><thead><tr><th>Module</th><th class="num">Cases</th><th>Executed</th><th class="num">Passed</th><th class="num">Failed</th><th class="num">Blocked</th></tr></thead><tbody>${modules}</tbody></table></div>` : '<div class="empty">No test cases yet.</div>'}</section>
    </div>
    <section class="panel section-gap"><div class="panel-head"><h2>Defects found in this plan</h2><span class="muted">${openDefects.length} open of ${report.defects.items.length}${report.defects.unregistered.length ? ` · ${report.defects.unregistered.length} unregistered reference${report.defects.unregistered.length === 1 ? "" : "s"} (${esc(report.defects.unregistered.slice(0, 5).join(", "))})` : ""}</span></div>
      ${defectRows ? `<div class="table-wrap"><table class="case-table"><thead><tr><th class="id-col">Defect</th><th>Summary</th><th>Severity</th><th>Status</th><th>Retest</th></tr></thead><tbody>${defectRows}</tbody></table></div>` : '<div class="empty">No defects linked to this plan\'s executions.</div>'}</section>`;
}

function openCycle(cycleId) { state.selectedCycle = cycleId; state.cycleTab = "cases"; go("execution"); }

async function retestCycle(cycleId) {
  const cycle = await api(`/test_cycles/${encodeURIComponent(cycleId)}`);
  const values = await openDialog({ title: "Retest failed and blocked test cases", confirmLabel: "Create retest cycle", body: `<p class="muted">Creates a Retesting cycle in the same plan with the selected test cases. Each keeps its tester; results in “${esc(cycle.name)}” are not changed.</p>
      <div class="form-grid"><div class="field full"><label>Retest these results</label><span class="check-row"><label><input type="checkbox" name="failed" checked> Failed</label><label><input type="checkbox" name="blocked" checked> Blocked</label></span></div>
      <div class="field"><label>New cycle name</label><input name="name" maxlength="150" placeholder="${esc(cycle.name)} – Retest"></div><div class="field"><label>Build with the fix</label><input name="build" maxlength="80" value="${esc(cycle.build || "")}"></div></div>` });
  if (!values) return;
  const statuses = [values.failed && "Failed", values.blocked && "Blocked"].filter(Boolean);
  if (!statuses.length) { toast("Choose Failed and/or Blocked"); return; }
  try {
    const result = await api(`/cycles/${encodeURIComponent(cycleId)}/retest`, { method: "POST", body: JSON.stringify({ statuses, name: values.name, build: values.build }) });
    toast(`${result.cycle.name} created with ${result.added} test case${result.added === 1 ? "" : "s"}`);
    openCycle(result.cycle.id);
  } catch (error) { toast(error.message); }
}

// ------------------------------------------------------------------ test suites
async function suitesPage() {
  const { items } = await api(`/suites?project_id=${encodeURIComponent(state.project.id)}`);
  const manage = can("plans.write"), cycles = can("cycles.write");
  const rows = items.map((suite) => `<tr>
      <td><strong>${esc(suite.name)}</strong>${suite.description ? `<small class="muted block">${esc(suite.description)}</small>` : ""}</td>
      <td>${suite.modules.length ? suite.modules.map((path) => `<span class="tag" title="${esc(path)}">${esc(path.split("/").filter(Boolean).at(-1) || path)}</span>`).join(" ") : '<span class="muted">—</span>'}${suite.case_ids.length ? `<small class="muted block">+ ${suite.case_ids.length} picked test case${suite.case_ids.length === 1 ? "" : "s"}</small>` : ""}</td>
      <td class="num"><strong>${suite.eligible}</strong>${Object.keys(suite.excluded).length ? `<small class="muted block" title="Archived or deprecated">${Object.values(suite.excluded).reduce((a, b) => a + b, 0)} not eligible</small>` : ""}</td>
      <td>${suite.cycles.length ? suite.cycles.map((c) => `<button class="link" onclick="openCycle(${jsArg(c.id)})">${esc(c.name)}</button>`).join(", ") : '<span class="muted">Not used yet</span>'}</td>
      <td class="row-actions">${cycles ? `<button class="button small" onclick="cycleWizard('', { suite: ${esc(JSON.stringify({ id: suite.id, module_ids: suite.module_ids, case_ids: suite.case_ids }))} })">Create cycle</button>` : ""}${manage ? `<button class="button secondary small" onclick="suiteWizard(${jsArg(suite.id)})">Edit</button><button class="icon-button remove-run" title="Delete suite" aria-label="Delete suite ${esc(suite.name)}" onclick="deleteSuite(${jsArg(suite.id)}, ${jsArg(suite.name)})">✕</button>` : ""}</td></tr>`).join("");
  return `<div class="repo-head"><div><nav class="crumbs"><span>${esc(state.project.key)}</span><span aria-hidden="true">›</span><span>Test Suites</span></nav><h1>Test Suites</h1>
      <p class="repo-stats"><span>Reusable sets of modules and test cases. Use one to build a Smoke, Sanity or Regression cycle in any release; new test cases added to its modules are included automatically.</span></p></div>
      ${manage ? '<div class="repo-actions"><button class="button small" onclick="suiteWizard()">+ New suite</button></div>' : ""}</div>
    <section class="panel">${items.length ? `<div class="table-wrap"><table class="case-table"><thead><tr><th>Suite</th><th>Contents</th><th class="num">Eligible test cases</th><th>Used in cycles</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="empty">No suites yet.${manage ? ' <button class="link" onclick="suiteWizard()">Create a suite</button>, e.g. “Payment smoke” or “Full regression”.' : ""}</div>`}</section>`;
}

async function suiteWizard(suiteId = "") {
  const suite = suiteId ? (await api(`/suites?project_id=${encodeURIComponent(state.project.id)}`)).items.find((item) => item.id === suiteId) : null;
  wiz = { mode: "suite", suite, cycle: null, plans: [], step: 2, info: {}, modules: new Set(suite?.module_ids || []), include: new Set(suite?.case_ids || []), exclude: new Set(),
    includeUnapproved: can("approvals.decide"), data: null, focus: "", moduleQuery: "", caseQuery: "", collapsed: new Set(), preview: null, suiteRefs: [] };
  renderWizard();
}

async function saveSuite() {
  const fixed = wiz.exclude.size > 0;
  const selected = wiz.data.cases.filter(wizIsSelected).map((testCase) => testCase.id);
  const values = await openDialog({ title: wiz.suite ? "Save suite" : "Save as suite", confirmLabel: "Save suite", body: `<div class="field"><label>Suite name *</label><input name="name" required maxlength="150" value="${esc(wiz.suite?.name || "")}" placeholder="e.g. Payment smoke"></div><div class="field"><label>Description</label><input name="description" maxlength="2000" value="${esc(wiz.suite?.description || "")}"></div>
      ${fixed ? `<p class="notice warn">You unticked some test cases inside a ticked module, so this suite will be saved as a fixed list of the ${selected.length} selected test cases. New test cases added to those modules later won't be included.</p>` : `<p class="muted">Ticked modules stay live: test cases approved in them later are included automatically.</p>`}` });
  if (!values) return;
  const body = { project_id: state.project.id, name: values.name, description: values.description, module_ids: fixed ? [] : [...wiz.modules], case_ids: fixed ? selected : [...wiz.include] };
  try {
    if (wiz.suite) await api(`/test_suites/${encodeURIComponent(wiz.suite.id)}`, { method: "PUT", body: JSON.stringify(body) });
    else await api("/suites", { method: "POST", body: JSON.stringify(body) });
    toast(`Suite “${values.name}” saved`);
    if (wiz.mode === "suite") { wiz = null; await go("suites"); }
  } catch (error) { toast(error.message); }
}

async function deleteSuite(suiteId, name) {
  const confirmed = await openDialog({ title: `Delete suite “${name}”?`, confirmLabel: "Delete", danger: true, body: "<p>The suite is archived. Cycles already created from it are not affected.</p>" });
  if (!confirmed) return;
  try { await api(`/test_suites/${encodeURIComponent(suiteId)}`, { method: "DELETE" }); toast("Suite deleted"); await render(); }
  catch (error) { toast(error.message); }
}

async function applySuiteToWizard(suiteId) {
  const suite = wiz.suites.find((item) => item.id === suiteId);
  if (!suite) return;
  suite.module_ids.forEach((id) => wiz.modules.add(id));
  suite.case_ids.forEach((id) => { wiz.include.add(id); wiz.exclude.delete(id); });
  if (!wiz.suiteRefs.includes(suite.id)) wiz.suiteRefs.push(suite.id);
  toast(`Added suite “${suite.name}”`);
  wizRedraw();
}

// ---------------------------------------------------------------------- defects
const SEVERITIES = ["Critical", "High", "Medium", "Low"];
function severityTag(severity) { return `<span class="priority ${PRIORITY_TONE[severity] || ""}"><i aria-hidden="true"></i>${esc(severity || "—")}</span>`; }

async function defectsPage() {
  const f = state.defectFilter;
  const data = await api(`/defects?project_id=${encodeURIComponent(state.project.id)}&status=${encodeURIComponent(f.status)}&severity=${encodeURIComponent(f.severity)}&q=${encodeURIComponent(f.q)}`);
  state.defectMeta = data;
  const rows = data.items.map((d) => `<tr class="case-row" onclick="openDefect(${jsArg(d.id)})"><td class="case-id-cell"><span class="case-id">${esc(d.key)}</span>${d.external_key ? `<small class="muted block">${esc(d.external_key)}</small>` : ""}</td>
      <td><button class="case-title" onclick="openDefect(${jsArg(d.id)})">${esc(d.summary)}</button></td><td>${severityTag(d.severity)}</td><td>${status(d.status)}</td>
      <td>${d.executions.length ? `${d.executions.length} · <span class="muted">${esc(d.executions.at(-1).case_key)}</span>` : '<span class="muted">—</span>'}</td>
      <td>${esc(d.found_in_cycle || "—")}</td><td>${esc(d.assignee_name || "—")}</td><td>${d.retest ? `<span class="retest ${d.retest === "Retest passed" ? "ok" : d.retest === "Retest failed" ? "no" : ""}">${esc(d.retest)}</span>` : '<span class="muted">—</span>'}</td></tr>`).join("");
  const select = (id, label, options, value) => `<label class="filter"><span class="visually-hidden">${label}</span><select id="${id}" aria-label="${label}">${options.map(([v, t]) => `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></label>`;
  return `<div class="repo-head"><div><nav class="crumbs"><span>${esc(state.project.key)}</span><span aria-hidden="true">›</span><span>Defects</span></nav><h1>Defects</h1>
      <p class="repo-stats"><span><strong>${data.summary.open}</strong> open</span><span><strong>${data.summary.fixed}</strong> fixed, awaiting verification</span><span><strong>${data.summary.total}</strong> total</span></p></div>
      ${can("execution.update") ? '<div class="repo-actions"><button class="button small" onclick="newDefect()">+ Log defect</button></div>' : ""}</div>
    ${data.unregistered.length ? `<section class="panel notice-panel"><strong>${data.unregistered.length} defect reference${data.unregistered.length === 1 ? "" : "s"} typed on executions ${data.unregistered.length === 1 ? "has" : "have"} no defect record yet.</strong> Register them to track status and retest. <span class="ref-list">${data.unregistered.map((u) => `<span class="ref">${esc(u.reference)} <small class="muted">${u.executions}×</small>${can("execution.update") ? ` <button class="link" onclick="registerDefect(${jsArg(u.reference)})">Register</button>` : ""}</span>`).join("")}</span></section>` : ""}
    <section class="panel"><div class="filter-bar">
      <label class="filter search-filter"><span class="visually-hidden">Search defects</span><input id="defectSearch" type="search" placeholder="Search key, Jira key or summary" value="${esc(f.q)}"></label>
      ${select("defectStatus", "Status", [["__open", "Open (all open states)"], ["", "All statuses"], ...data.statuses.map((s) => [s, s])], f.status)}
      ${select("defectSeverity", "Severity", [["", "All severities"], ...data.severities.map((s) => [s, s])], f.severity)}
      <span class="repo-count"><strong>${data.items.length}</strong> shown</span></div>
      <div class="table-wrap"><table class="case-table exec-table"><thead><tr><th class="id-col">Defect</th><th>Summary</th><th>Severity</th><th>Status</th><th>Executions</th><th>Found in</th><th>Assigned to</th><th>Retest</th></tr></thead><tbody>${rows || `<tr><td colspan="8"><div class="empty">No defects match. Defects are logged from a failed test case on the execution screen, or with “+ Log defect”.</div></td></tr>`}</tbody></table></div></section>`;
}

function bindDefects() {
  const search = $("#defectSearch");
  if (!search) return;
  let timer = null;
  search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { state.defectFilter.q = search.value; render().then(() => { const s = $("#defectSearch"); if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); } }); }, 400); };
  $("#defectStatus").onchange = (event) => { state.defectFilter.status = event.target.value; render(); };
  $("#defectSeverity").onchange = (event) => { state.defectFilter.severity = event.target.value; render(); };
}

function defectFields(values = {}) {
  const options = (list, value) => list.map((item) => `<option ${item === value ? "selected" : ""}>${item}</option>`).join("");
  return `<div class="form-grid">
    <div class="field full"><label>Summary *</label><input name="summary" required maxlength="200" value="${esc(values.summary || "")}"></div>
    <div class="field"><label>Severity</label><select name="severity">${options(SEVERITIES, values.severity || "Medium")}</select></div>
    <div class="field"><label>Priority</label><select name="priority">${options(SEVERITIES, values.priority || "Medium")}</select></div>
    <div class="field"><label>External key (e.g. Jira)</label><input name="external_key" maxlength="120" value="${esc(values.external_key || "")}" placeholder="PAY-412"></div>
    <div class="field"><label>External link</label><input name="external_url" maxlength="500" value="${esc(values.external_url || "")}" placeholder="https://…"></div>
    <div class="field full"><label>Description / steps to reproduce</label><textarea name="description" maxlength="8000">${esc(values.description || "")}</textarea></div></div>`;
}

async function newDefect(runId = "", prefill = {}) {
  const values = await openDialog({ title: runId ? "Log defect for this test case" : "Log defect", confirmLabel: "Log defect", body: defectFields(prefill) });
  if (!values) return null;
  try {
    const defect = await api("/defects", { method: "POST", body: JSON.stringify({ project_id: state.project.id, run_id: runId, ...values }) });
    toast(`${defect.key} logged`);
    if (!runId) await render();
    return defect;
  } catch (error) { toast(error.message); return null; }
}

async function registerDefect(reference) {
  try { const defect = await api("/defects/register", { method: "POST", body: JSON.stringify({ project_id: state.project.id, reference }) }); toast(`${reference} registered as ${defect.key}`); await render(); }
  catch (error) { toast(error.message); }
}

async function openDefect(defectId) {
  drawerLoading("Defect");
  let d;
  try { d = await api(`/defects/${encodeURIComponent(defectId)}`); }
  catch (error) { openDrawer("Defect", "", `<div class="notice danger">${esc(error.message)}</div>`); return; }
  const edit = can("execution.update");
  const statuses = ["Open", "In Progress", "Fixed", "Verified", "Closed", "Reopened", "Rejected"];
  const members = state.users.filter((user) => user.status === "Active");
  openDrawer(`${d.key} · ${d.summary}`, `${status(d.status)} ${severityTag(d.severity)} · reported by ${esc(d.reporter_name || "—")} · ${esc(fmtDate(d.created_at))}${d.external_key ? ` · ${d.external_url ? `<a href="${esc(d.external_url)}" target="_blank" rel="noopener noreferrer">${esc(d.external_key)}</a>` : esc(d.external_key)}` : ""}`,
    `${d.retest ? `<div class="notice ${d.retest === "Retest passed" ? "ok" : d.retest === "Retest failed" ? "danger" : "warn"}"><strong>${esc(d.retest)}.</strong> ${d.retest === "Retest passed" ? "You can mark this defect Verified." : d.retest === "Awaiting retest" ? "Create a retest cycle to check the fix." : "The fix did not hold; consider reopening."}</div>` : ""}
     ${edit ? `<form id="defectForm" class="dialog-form">${defectFields(d)}<div class="form-grid">
        <div class="field"><label>Status</label><select name="status">${statuses.map((s) => `<option ${s === d.status ? "selected" : ""}>${s}</option>`).join("")}</select></div>
        <div class="field"><label>Assigned to</label><select name="assignee_id"><option value="">Unassigned</option>${members.map((user) => `<option value="${esc(user.id)}" ${user.id === d.assignee_id ? "selected" : ""}>${esc(user.name)}</option>`).join("")}</select></div>
        <div class="field"><label>Found in build</label><input name="build" maxlength="80" value="${esc(d.build || "")}"></div>
        <div class="field"><label>Fixed in build</label><input name="fixed_in_build" maxlength="80" value="${esc(d.fixed_in_build || "")}"></div></div>
        <p class="form-error" role="alert"></p><div class="form-actions">${can("cycles.write") && d.executions.length ? `<button type="button" class="button secondary" onclick="retestDefect(${jsArg(d.id)})">Create retest cycle</button>` : ""}<button class="button">Save defect</button></div></form>`
      : `<p class="prewrap">${esc(d.description || "No description.")}</p>`}
     <h3 class="confirm-subhead">Linked executions (${d.executions.length})</h3>
     ${d.executions.length ? `<ul class="failure-list">${d.executions.map((e) => `<li><button class="link case-key" onclick="closeDrawer(); openExecution(${jsArg(e.run_id)})">${esc(e.case_key)}</button> ${status(e.status)} <span class="muted">in ${esc(e.cycle)}${e.executed_at ? ` · ${esc(fmtDate(e.executed_at))}` : ""}</span><div>${esc(plainTitle({ case_key: e.case_key, title: e.title }))}</div></li>`).join("")}</ul>` : '<p class="muted">Not linked to any execution.</p>'}
     <p class="muted">Found in: ${esc(d.found_in_cycle || "—")} · environment ${esc(d.environment || "—")}${d.resolved_at ? ` · fixed ${esc(fmtDate(d.resolved_at))}` : ""}${d.closed_at ? ` · closed ${esc(fmtDate(d.closed_at))}` : ""}</p>`);
  const form = $("#defectForm");
  if (form) form.onsubmit = async (event) => {
    event.preventDefault();
    try { await api(`/defects/${encodeURIComponent(d.id)}`, { method: "PUT", body: JSON.stringify(Object.fromEntries(new FormData(form))) }); toast(`${d.key} saved`); closeDrawer(); await render(); }
    catch (error) { form.querySelector(".form-error").textContent = error.message; }
  };
}

async function retestDefect(defectId) {
  try { const result = await api(`/defects/${encodeURIComponent(defectId)}/retest`, { method: "POST", body: "{}" }); closeDrawer(); toast(`${result.cycle.name} created with ${result.added} test case${result.added === 1 ? "" : "s"}`); openCycle(result.cycle.id); }
  catch (error) { toast(error.message); }
}

async function logDefectFromExecution(runId, caseKey, title) {
  const defect = await newDefect(runId, { summary: `${caseKey} – ${plainTitle({ case_key: caseKey, title })} fails`.slice(0, 200), description: $("#execActual")?.value || "", severity: "Medium" });
  if (defect) addDefectToExecution(defect.key);
}

async function linkDefectFromExecution(runId) {
  const { items } = await api(`/defects?project_id=${encodeURIComponent(state.project.id)}&status=__open`);
  if (!items.length) { toast("There are no open defects to link. Use Log defect."); return; }
  const values = await openDialog({ title: "Link an existing defect", confirmLabel: "Link", body: `<div class="field"><label>Open defect</label><select name="defect_id">${items.map((d) => `<option value="${esc(d.id)}">${esc(d.key)} · ${esc(d.summary)}</option>`).join("")}</select></div>` });
  if (!values) return;
  try { const defect = await api(`/defects/${encodeURIComponent(values.defect_id)}/link`, { method: "POST", body: JSON.stringify({ run_id: runId }) }); toast(`${defect.key} linked`); addDefectToExecution(defect.key); }
  catch (error) { toast(error.message); }
}

function addDefectToExecution(key) {
  const input = $("#execDefects");
  if (!input) return;
  const list = defectList(input.value);
  if (!list.map((item) => item.toLowerCase()).includes(key.toLowerCase())) input.value = [...list, key].join(", ");
}

async function rebuildIndex() {
  try {
    const result = await api("/rebuild-index", { method: "POST" });
    toast(`Search index rebuilt (${result.indexed_records} records)`);
  } catch (error) { toast(error.message); }
}

async function searchRecords(event) {
  if (event.key !== "Enter" || !state.project) return;
  state.repoFilters.q = event.target.value.trim();
  state.selectedFolder = "";
  event.target.value = "";
  await go("repository");
  $("#repoSearch")?.focus();
}

document.querySelectorAll(".nav").forEach((item) => { item.onclick = () => go(item.dataset.view); });
$("#search").onkeydown = searchRecords;
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("#search").focus();
  }
  if (event.key === "Escape") { hideFolderMenu(); hideConfirmModal(); }
});
document.addEventListener("click", (event) => {
  const menu = $("#folderContextMenu");
  if (menu && !menu.hidden && !menu.contains(event.target)) hideFolderMenu();
  const userMenu = $("#userMenuList");
  if (userMenu && !userMenu.hidden && !$("#userMenu").contains(event.target)) userMenu.hidden = true;
});
$("#searchShortcut").textContent = isMac ? "⌘ K" : "Ctrl K";
init();
