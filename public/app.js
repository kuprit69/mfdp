const API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:8765" : "";
const AUTH_TOKEN_KEY = "lungPrometheusAuthToken";

const state = {
  token: localStorage.getItem(AUTH_TOKEN_KEY) || "",
  user: null,
  patients: [],
  patientQuery: "",
  patientPage: 0,
  expandedPatientId: null,
  patientStudiesCache: {},
  selectedStudyId: null,
  selectedStudy: null,
  slices: [],
  currentSlice: 0,
  detections: [],
  zoom: 1,
  pan: { x: 0, y: 0 },
  isPanning: false,
  panStart: null,
  panOrigin: null,
  // null = use each slice's own window/level (from DICOM tags or auto pixel
  // min/max); { center, width } = manual override applied to every slice.
  windowOverride: null,
  // A file has been parsed into slices but no study/patient exists for it
  // yet - the "file first, then patient card" upload flow lives in these:
  // pendingUpload holds the parsed slices, pendingMatch/pendingPatientInfo
  // track the duplicate-detection step, and pendingTargetPatientId is set
  // when the user chose "+ Добавить исследование" on an existing patient's
  // card, so the very next dropped file skips the form/dedup step entirely.
  pendingUpload: null,
  pendingMatch: null,
  pendingPatientInfo: null,
  pendingTargetPatientId: null
};

const PATIENTS_PER_PAGE = 8;
let patientSearchDebounceTimer = null;
let defaultDropZoneHint = "";

const WINDOW_PRESETS = {
  // HU center/width presets, standard-ish radiology conventions.
  lung: { center: -600, width: 1500 },
  bone: { center: 400, width: 1800 },
  soft: { center: 40, width: 400 }
};

const el = {};

document.addEventListener("DOMContentLoaded", () => {
  for (const id of [
    "authPanel", "appLayout", "sessionPanel", "authUser", "balanceLabel",
    "requestPriceLabel", "topUpForm", "topUpAmount", "logoutButton",
    "showLoginButton", "showRegisterButton", "loginForm", "loginUsername",
    "loginPassword", "registerForm", "registerUsername", "registerPassword",
    "authMessage", "refreshButton", "patientCount", "patientList", "emptyState", "viewerPanel",
    "viewerTitle", "studyInfo", "reportButton", "viewer", "imageCanvas",
    "overlayCanvas", "modelStatus", "prevButton", "nextButton", "sliceSlider",
    "sliceLabel", "zoomOutButton", "zoomSlider", "zoomInButton", "zoomLabel",
    "findingCount", "findingsList", "reportPanel", "reportText", "reportSourceBadge",
    "patientForm", "patientName", "birthDate", "fileInput", "folderInput", "toastContainer",
    "dropZone", "dropZoneHint",
    "patientSearch", "patientPagination", "patientPrevPageButton", "patientPageLabel", "patientNextPageButton",
    "patientNameError", "birthDateError", "analysisProgress", "analysisProgressBar",
    "wlLungButton", "wlBoneButton", "wlSoftButton", "wlAutoButton",
    "wlCenterSlider", "wlWidthSlider", "wlCenterLabel", "wlWidthLabel", "resetViewButton",
    "pendingUploadPanel", "pendingUploadSummary", "addStudyButton", "cancelPendingUploadButton",
    "backToPendingUploadButton", "patientMatchPrompt", "patientMatchText",
    "useExistingPatientButton", "createNewPatientButton",
    "loadingOverlay", "loadingCanvas", "loadingCaptionText"
  ]) {
    el[id] = document.getElementById(id);
  }

  defaultDropZoneHint = el.dropZoneHint ? el.dropZoneHint.textContent : "";

  el.showLoginButton.addEventListener("click", () => setAuthMode("login"));
  el.showRegisterButton.addEventListener("click", () => setAuthMode("register"));
  el.loginForm.addEventListener("submit", handleLogin);
  el.registerForm.addEventListener("submit", handleRegister);
  el.topUpForm.addEventListener("submit", handleTopUp);
  el.logoutButton.addEventListener("click", logout);
  el.refreshButton.addEventListener("click", () => loadPatients());
  el.fileInput.addEventListener("change", event => loadFiles(event.target.files));
  el.folderInput.addEventListener("change", event => loadFiles(event.target.files));
  el.patientSearch.addEventListener("input", () => {
    clearTimeout(patientSearchDebounceTimer);
    const query = el.patientSearch.value.trim();
    patientSearchDebounceTimer = setTimeout(() => loadPatients(query), 200);
  });
  el.patientPrevPageButton.addEventListener("click", () => {
    state.patientPage = Math.max(0, state.patientPage - 1);
    renderPatientList();
  });
  el.patientNextPageButton.addEventListener("click", () => {
    state.patientPage += 1;
    renderPatientList();
  });
  setupDropZone();
  el.patientName.addEventListener("input", () => setFieldError(el.patientName, el.patientNameError, ""));
  el.birthDate.addEventListener("input", () => setFieldError(el.birthDate, el.birthDateError, ""));
  el.patientForm.addEventListener("submit", handlePatientFormSubmit);
  el.addStudyButton.addEventListener("click", showPatientCardForm);
  el.cancelPendingUploadButton.addEventListener("click", cancelPendingUpload);
  el.backToPendingUploadButton.addEventListener("click", showPendingUploadCta);
  el.useExistingPatientButton.addEventListener("click", handleUseExistingPatient);
  el.createNewPatientButton.addEventListener("click", handleCreateNewPatient);
  el.prevButton.addEventListener("click", () => setSlice(state.currentSlice - 1));
  el.nextButton.addEventListener("click", () => setSlice(state.currentSlice + 1));
  el.sliceSlider.addEventListener("input", event => setSlice(Number(event.target.value)));
  el.zoomSlider.addEventListener("input", event => setZoom(Number(event.target.value) / 100));
  el.zoomOutButton.addEventListener("click", () => adjustZoom(-0.1));
  el.zoomInButton.addEventListener("click", () => adjustZoom(0.1));
  el.resetViewButton.addEventListener("click", resetView);
  el.reportButton.addEventListener("click", generateReport);
  el.viewer.addEventListener("wheel", handleWheel, { passive: false });
  el.viewer.addEventListener("pointerdown", startPan);
  el.wlLungButton.addEventListener("click", () => applyWindowPreset("lung"));
  el.wlBoneButton.addEventListener("click", () => applyWindowPreset("bone"));
  el.wlSoftButton.addEventListener("click", () => applyWindowPreset("soft"));
  el.wlAutoButton.addEventListener("click", () => applyWindowPreset("auto"));
  el.wlCenterSlider.addEventListener("input", handleWindowSliderInput);
  el.wlWidthSlider.addEventListener("input", handleWindowSliderInput);
  window.addEventListener("keydown", handleKeydown);
  window.addEventListener("resize", render);
  initializeAuth();
});

async function initializeAuth() {
  if (!state.token) {
    showAuth();
    return;
  }

  try {
    const result = await api("/api/auth/me");
    setUser(result.user);
    showApp();
    await loadPatients();
  } catch (error) {
    clearAuth();
    showAuth();
  }
}

async function handleLogin(event) {
  event.preventDefault();
  await authenticate("/api/auth/login", {
    username: el.loginUsername.value.trim(),
    password: el.loginPassword.value
  });
}

async function handleRegister(event) {
  event.preventDefault();
  await authenticate("/api/auth/register", {
    username: el.registerUsername.value.trim(),
    password: el.registerPassword.value
  });
}

async function authenticate(path, body) {
  setAuthMessage("");
  try {
    const result = await api(path, { method: "POST", body });
    state.token = result.token;
    localStorage.setItem(AUTH_TOKEN_KEY, state.token);
    setUser(result.user);
    showApp();
    await loadPatients();
  } catch (error) {
    setAuthMessage(error.message);
  }
}

function showToast(message, type = "error", duration = 5000) {
  if (!el.toastContainer) {
    // Toast container missing for some reason (older cached page) - don't
    // silently swallow the message.
    console.error(message);
    return;
  }
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  el.toastContainer.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("visible"));

  let dismissed = false;
  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    toast.classList.remove("visible");
    setTimeout(() => toast.remove(), 200);
  };
  const timer = setTimeout(dismiss, duration);
  toast.addEventListener("click", () => {
    clearTimeout(timer);
    dismiss();
  });
}

async function handleTopUp(event) {
  event.preventDefault();
  const amount = el.topUpAmount.value;
  if (!amount) return;
  try {
    const result = await api("/api/balance/top-up", {
      method: "POST",
      body: { amount }
    });
    setUser(result.user);
    el.topUpAmount.value = "";
  } catch (error) {
    showToast(error.message);
  }
}

function setAuthMode(mode) {
  const isLogin = mode === "login";
  el.loginForm.classList.toggle("hidden", !isLogin);
  el.registerForm.classList.toggle("hidden", isLogin);
  el.showLoginButton.classList.toggle("active", isLogin);
  el.showRegisterButton.classList.toggle("active", !isLogin);
  setAuthMessage("");
}

function setUser(user) {
  state.user = user;
  el.authUser.textContent = user.username;
  el.balanceLabel.textContent = formatRubles(user.balance);
  el.requestPriceLabel.textContent = `Запрос: ${formatRubles(user.request_price)}`;
}

function showAuth() {
  el.authPanel.classList.remove("hidden");
  el.appLayout.classList.add("hidden");
  el.sessionPanel.classList.add("hidden");
}

function showApp() {
  el.authPanel.classList.add("hidden");
  el.appLayout.classList.remove("hidden");
  el.sessionPanel.classList.remove("hidden");
}

function clearAuth() {
  state.token = "";
  state.user = null;
  localStorage.removeItem(AUTH_TOKEN_KEY);
  resetViewer();
  cancelPendingUpload();
  state.patients = [];
  state.patientQuery = "";
  state.expandedPatientId = null;
  state.patientStudiesCache = {};
  state.selectedStudyId = null;
  state.selectedStudy = null;
}

async function logout() {
  // Invalidate the token server-side first (rotates it so it can't be reused
  // if it leaked) - best-effort, since the user should still be able to log
  // out locally even if the request fails (offline, server restarting, etc).
  try {
    if (state.token) {
      await api("/api/auth/logout", { method: "POST" });
    }
  } catch (error) {
    console.warn("Не удалось уведомить сервер о выходе:", error.message);
  }
  clearAuth();
  showAuth();
}

function setAuthMessage(message) {
  el.authMessage.textContent = message;
}

async function loadPatients(query) {
  if (!state.user) return;
  if (typeof query === "string") state.patientQuery = query;
  const result = await api(`/api/patients?query=${encodeURIComponent(state.patientQuery || "")}`);
  state.patients = result.patients;
  state.patientPage = 0;
  renderPatientList();
}

async function loadPatientStudies(patientId) {
  try {
    const result = await api(`/api/patients/${patientId}`);
    state.patientStudiesCache[patientId] = { studies: result.patient.studies };
  } catch (error) {
    state.patientStudiesCache[patientId] = { studies: [] };
  }
}

async function togglePatientExpand(patientId) {
  if (state.expandedPatientId === patientId) {
    state.expandedPatientId = null;
    renderPatientList();
    return;
  }
  state.expandedPatientId = patientId;
  renderPatientList();
  if (!state.patientStudiesCache[patientId]) {
    await loadPatientStudies(patientId);
    renderPatientList();
  }
}

function armAddStudyForPatient(patientId, patientName) {
  state.pendingTargetPatientId = patientId;
  if (el.dropZoneHint) {
    el.dropZoneHint.textContent = `Перетащите файл — он будет добавлен пациенту «${patientName}»`;
  }
  showToast(`Загрузите файл — он будет добавлен в карту пациента «${patientName}»`, "success", 4500);
}

function renderPatientList() {
  el.patientCount.textContent = state.patients.length;

  if (!state.patients.length) {
    el.patientList.innerHTML = `<div class="empty-line">${state.patientQuery ? "Ничего не найдено" : "Пока пусто"}</div>`;
    el.patientPagination.classList.add("hidden");
    if (!state.patientQuery) resetViewer();
    return;
  }

  const pageCount = Math.max(1, Math.ceil(state.patients.length / PATIENTS_PER_PAGE));
  state.patientPage = Math.max(0, Math.min(state.patientPage, pageCount - 1));
  const start = state.patientPage * PATIENTS_PER_PAGE;
  const pageItems = state.patients.slice(start, start + PATIENTS_PER_PAGE);

  el.patientList.innerHTML = pageItems.map(renderPatientCard).join("");

  for (const button of el.patientList.querySelectorAll("[data-patient-toggle]")) {
    button.addEventListener("click", () => togglePatientExpand(button.dataset.patientToggle));
  }
  for (const button of el.patientList.querySelectorAll("[data-add-to-patient]")) {
    button.addEventListener("click", event => {
      event.stopPropagation();
      armAddStudyForPatient(button.dataset.addToPatient, button.dataset.patientName);
    });
  }
  for (const button of el.patientList.querySelectorAll("[data-study-id]")) {
    button.addEventListener("click", event => {
      event.stopPropagation();
      selectStudy(button.dataset.studyId);
    });
  }
  for (const button of el.patientList.querySelectorAll("[data-delete-id]")) {
    button.addEventListener("click", event => {
      event.stopPropagation();
      handleDeleteClick(button, button.dataset.deleteId);
    });
  }

  el.patientPagination.classList.toggle("hidden", pageCount <= 1);
  el.patientPageLabel.textContent = `Стр. ${state.patientPage + 1} из ${pageCount}`;
  el.patientPrevPageButton.disabled = state.patientPage <= 0;
  el.patientNextPageButton.disabled = state.patientPage >= pageCount - 1;
}

function renderPatientCard(patient) {
  const expanded = state.expandedPatientId === patient.id;
  let studiesHtml = "";

  if (expanded) {
    const cache = state.patientStudiesCache[patient.id];
    if (!cache) {
      studiesHtml = '<p class="muted">Загрузка...</p>';
    } else if (!cache.studies.length) {
      studiesHtml = '<p class="muted">Исследований пока нет.</p>';
    } else {
      studiesHtml = cache.studies.map(study => `
        <div class="study-row">
          <button class="study-item ${study.id === state.selectedStudyId ? "active" : ""}" data-study-id="${study.id}" type="button">
            <strong>${escapeHtml(study.description || "Исследование")}</strong>
            <span>${formatDateTime(study.created_at)} · ${study.finding_count} находок</span>
            <small>${escapeHtml(study.status)}</small>
          </button>
          <button class="delete-study" data-delete-id="${study.id}" type="button" title="Удалить" aria-label="Удалить исследование">🗑</button>
        </div>
      `).join("");
    }
  }

  return `
    <div class="patient-card">
      <button class="patient-item ${expanded ? "active" : ""}" data-patient-toggle="${patient.id}" type="button">
        <strong>${escapeHtml(patient.full_name)}</strong>
        <span>${formatBirthDate(patient.birth_date)} · ${patient.study_count} исслед.</span>
      </button>
      ${expanded ? `
        <div class="patient-studies">
          ${studiesHtml}
          <button class="add-study-to-patient compact" data-add-to-patient="${patient.id}" data-patient-name="${escapeHtml(patient.full_name)}" type="button">+ Добавить исследование</button>
        </div>
      ` : ""}
    </div>
  `;
}

async function refreshPatientsAndExpandedCard() {
  if (state.expandedPatientId) {
    delete state.patientStudiesCache[state.expandedPatientId];
    await loadPatientStudies(state.expandedPatientId);
  }
  await loadPatients();
}

async function selectStudy(id) {
  state.selectedStudyId = id;
  const result = await api(`/api/studies/${id}`);
  state.selectedStudy = result.study;
  state.detections = result.study.findings
    .filter(finding => finding.slice_index != null)
    .map(finding => ({
      title: finding.title,
      confidence: finding.confidence,
      sliceIndex: finding.slice_index,
      x: finding.x || 0,
      y: finding.y || 0,
      width: finding.width || 0,
      height: finding.height || 0
    }));
  if (result.study.patient_record_id && result.study.patient_record_id !== state.expandedPatientId) {
    state.expandedPatientId = result.study.patient_record_id;
    if (!state.patientStudiesCache[state.expandedPatientId]) {
      await loadPatientStudies(state.expandedPatientId);
    }
  }
  renderPatientList();

  if (result.study.has_slices) {
    await loadStoredSlices(result.study);
  } else {
    state.slices = [];
    showHistoryDetails(result.study);
  }
}

async function loadStoredSlices(study) {
  clearOldUrls();
  state.currentSlice = 0;
  state.zoom = 1;
  state.pan = { x: 0, y: 0 };
  state.windowOverride = null;
  el.modelStatus.textContent = "Загружаю сохранённый снимок...";

  let slices = [];
  try {
    const result = await api(`/api/studies/${study.id}/slices`);
    slices = (result.slices || []).map(decodeStoredSlice);
  } catch (error) {
    slices = [];
  }

  if (!slices.length) {
    state.slices = [];
    showHistoryDetails(study);
    return;
  }

  state.slices = slices;
  state.currentSlice = state.detections.length
    ? state.detections[0].sliceIndex
    : Math.floor((slices.length - 1) / 2);

  el.emptyState.classList.add("hidden");
  el.viewerPanel.classList.remove("hidden");
  el.viewerTitle.textContent = study.patient_name;
  el.studyInfo.textContent = `${slices.length} срез(ов) · ${formatBirthDate(study.birth_date)}`;
  el.modelStatus.textContent = state.detections.length
    ? `Находок: ${state.detections.length}`
    : "Патологий не найдено";
  el.reportButton.classList.toggle("hidden", !study.findings.length);
  el.reportPanel.classList.toggle("hidden", !study.report);
  el.reportText.value = study.report || "";
  setReportSourceBadge(study.report_source);

  hydrateControls();
  render();
  renderFindings();
}

function setReportSourceBadge(source) {
  if (source === "ollama") {
    el.reportSourceBadge.textContent = "сгенерировано ИИ (Ollama)";
    el.reportSourceBadge.classList.remove("hidden", "fallback");
    el.reportSourceBadge.classList.add("ollama");
  } else if (source === "fallback") {
    el.reportSourceBadge.textContent = "шаблон (ИИ недоступен)";
    el.reportSourceBadge.classList.remove("hidden", "ollama");
    el.reportSourceBadge.classList.add("fallback");
  } else {
    el.reportSourceBadge.classList.add("hidden");
    el.reportSourceBadge.textContent = "";
  }
}

function showHistoryDetails(study) {
  el.emptyState.classList.add("hidden");
  el.viewerPanel.classList.remove("hidden");
  el.viewerTitle.textContent = study.patient_name;
  el.studyInfo.textContent = `Дата рождения: ${formatBirthDate(study.birth_date)}. Снимок для этого исследования не сохранён — загрузите файл заново, чтобы посмотреть его.`;
  el.modelStatus.textContent = "Снимок недоступен.";
  el.reportButton.classList.toggle("hidden", !study.findings.length);
  el.findingCount.textContent = study.findings.length;
  el.findingsList.innerHTML = study.findings.length
    ? study.findings.map(finding => `
        <div class="finding readonly">
          <strong>${escapeHtml(finding.title)}</strong>
          <span>${Math.round(finding.confidence * 100)}% · ${escapeHtml(finding.source)}</span>
        </div>
      `).join("")
    : "<p>Находок нет.</p>";

  const context = el.imageCanvas.getContext("2d");
  context.clearRect(0, 0, el.imageCanvas.width, el.imageCanvas.height);
  el.overlayCanvas.getContext("2d").clearRect(0, 0, el.overlayCanvas.width, el.overlayCanvas.height);

  if (study.report) {
    el.reportPanel.classList.remove("hidden");
    el.reportText.value = study.report;
    setReportSourceBadge(study.report_source);
  } else {
    el.reportPanel.classList.add("hidden");
    el.reportText.value = "";
  }
}

function decodeStoredSlice(raw) {
  const TypedArrayCtor = TYPED_ARRAY_CTORS[(raw.pixelData || {}).dtype] || Int16Array;
  const bytes = base64ToUint8Array(raw.pixelData.data);
  const typed = new TypedArrayCtor(
    bytes.buffer,
    bytes.byteOffset,
    Math.floor(bytes.byteLength / TypedArrayCtor.BYTES_PER_ELEMENT)
  );
  const slope = Number(raw.rescaleSlope || 1);
  const intercept = Number(raw.rescaleIntercept || 0);
  const scaled = new Float32Array(typed.length);
  for (let i = 0; i < typed.length; i += 1) {
    scaled[i] = typed[i] * slope + intercept;
  }
  const { min, max } = minMax(scaled, scaled.length);

  return {
    format: "STORED",
    name: raw.name,
    width: raw.width || raw.columns,
    height: raw.height || raw.rows,
    pixelData: scaled,
    pixelMin: min,
    pixelMax: max,
    windowCenter: min + (max - min) / 2,
    windowWidth: Math.max(1, max - min),
    spacing: raw.pixelSpacing
  };
}

const TYPED_ARRAY_CTORS = {
  Int8Array,
  Uint8Array,
  Int16Array,
  Uint16Array,
  Int32Array,
  Uint32Array,
  Float32Array,
  Float64Array
};

function base64ToUint8Array(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

const DELETE_CONFIRM_TIMEOUT_MS = 4000;

function handleDeleteClick(button, id) {
  if (button.dataset.confirming === "true") {
    clearTimeout(Number(button.dataset.confirmTimer));
    deleteStudy(id, button);
    return;
  }
  // Only one row should be in the "are you sure" state at a time.
  for (const other of el.patientList.querySelectorAll("[data-delete-id]")) {
    if (other !== button) resetDeleteButton(other);
  }
  button.dataset.confirming = "true";
  button.classList.add("confirming");
  button.textContent = "Точно?";
  button.title = "Нажмите ещё раз, чтобы удалить";
  const timer = setTimeout(() => resetDeleteButton(button), DELETE_CONFIRM_TIMEOUT_MS);
  button.dataset.confirmTimer = String(timer);
}

function resetDeleteButton(button) {
  clearTimeout(Number(button.dataset.confirmTimer));
  button.dataset.confirming = "false";
  button.classList.remove("confirming");
  button.textContent = "🗑";
  button.title = "Удалить";
}

async function deleteStudy(id, button) {
  try {
    if (button) button.disabled = true;
    await api(`/api/studies/${id}`, { method: "DELETE" });
    if (state.selectedStudyId === id) {
      state.selectedStudyId = null;
      state.selectedStudy = null;
      resetViewer();
    }
    await refreshPatientsAndExpandedCard();
  } catch (error) {
    showToast(error.message);
    if (button) {
      button.disabled = false;
      resetDeleteButton(button);
    }
  }
}

function resetViewer() {
  clearOldUrls();
  state.slices = [];
  state.currentSlice = 0;
  state.detections = [];
  state.zoom = 1;
  state.pan = { x: 0, y: 0 };
  state.windowOverride = null;
  el.emptyState.classList.remove("hidden");
  el.viewerPanel.classList.add("hidden");
  el.reportPanel.classList.add("hidden");
  el.reportText.value = "";
  setReportSourceBadge("");
  if (el.imageCanvas && el.overlayCanvas) {
    el.imageCanvas.getContext("2d").clearRect(0, 0, el.imageCanvas.width, el.imageCanvas.height);
    el.overlayCanvas.getContext("2d").clearRect(0, 0, el.overlayCanvas.width, el.overlayCanvas.height);
  }
}

function setFieldError(inputEl, errorEl, message) {
  inputEl.classList.toggle("invalid", Boolean(message));
  if (errorEl) errorEl.textContent = message || "";
}

function setupDropZone() {
  const zone = el.dropZone;
  if (!zone) return;

  // Without this, dropping a file anywhere the browser doesn't expect it
  // navigates the tab to that file instead of doing nothing.
  window.addEventListener("dragover", event => event.preventDefault());
  window.addEventListener("drop", event => event.preventDefault());

  ["dragenter", "dragover"].forEach(type => {
    zone.addEventListener(type, event => {
      event.preventDefault();
      zone.classList.add("drag-active");
    });
  });
  zone.addEventListener("dragleave", event => {
    if (zone.contains(event.relatedTarget)) return;
    zone.classList.remove("drag-active");
  });
  zone.addEventListener("drop", handleDrop);
}

async function handleDrop(event) {
  event.preventDefault();
  el.dropZone.classList.remove("drag-active");
  if (!state.user) {
    showToast("требуется вход");
    return;
  }

  const items = event.dataTransfer?.items;
  let files;
  if (items && items.length && typeof items[0].webkitGetAsEntry === "function") {
    files = await collectFilesFromDataTransferItems(items);
  } else {
    files = Array.from(event.dataTransfer?.files || []);
  }
  if (files.length) await loadFiles(files);
}

async function collectFilesFromDataTransferItems(items) {
  const entries = Array.from(items)
    .map(item => (typeof item.webkitGetAsEntry === "function" ? item.webkitGetAsEntry() : null))
    .filter(Boolean);
  const files = [];
  await Promise.all(entries.map(entry => walkFileSystemEntry(entry, "", files)));
  return files;
}

function walkFileSystemEntry(entry, pathPrefix, files) {
  return new Promise(resolve => {
    if (entry.isFile) {
      entry.file(file => {
        // A dropped folder's files don't carry webkitRelativePath the way
        // an <input webkitdirectory> selection does - set it manually so
        // the .mhd/.raw companion-file lookup (which matches on relative
        // path) works the same way regardless of how the files arrived.
        const relativePath = `${pathPrefix}${entry.name}`;
        if (relativePath !== file.name) {
          try {
            Object.defineProperty(file, "webkitRelativePath", { value: relativePath });
          } catch (error) {
            // Read-only in some browsers - matching falls back to file.name.
          }
        }
        files.push(file);
        resolve();
      }, resolve);
      return;
    }
    if (entry.isDirectory) {
      const reader = entry.createReader();
      const readNextBatch = () => {
        reader.readEntries(async batch => {
          if (!batch.length) {
            resolve();
            return;
          }
          await Promise.all(batch.map(child => walkFileSystemEntry(child, `${pathPrefix}${entry.name}/`, files)));
          readNextBatch();
        }, resolve);
      };
      readNextBatch();
      return;
    }
    resolve();
  });
}

function validatePatientForm() {
  const patientName = el.patientName.value.trim();
  const birthDate = el.birthDate.value.trim();
  let firstInvalid = null;

  setFieldError(el.patientName, el.patientNameError, patientName ? "" : "Укажите ФИО пациента");
  if (!patientName) firstInvalid = firstInvalid || el.patientName;

  if (!birthDate) {
    setFieldError(el.birthDate, el.birthDateError, "Укажите дату рождения");
    firstInvalid = firstInvalid || el.birthDate;
  } else if (new Date(birthDate) > new Date()) {
    setFieldError(el.birthDate, el.birthDateError, "Дата рождения не может быть в будущем");
    firstInvalid = firstInvalid || el.birthDate;
  } else {
    setFieldError(el.birthDate, el.birthDateError, "");
  }

  if (firstInvalid) firstInvalid.focus();
  return { valid: !firstInvalid, patientName, birthDate };
}

async function loadFiles(fileList) {
  try {
    if (!state.user) {
      throw new Error("требуется вход");
    }
    const files = Array.from(fileList || []);
    if (!files.length) return;

    clearOldUrls();
    const slices = await filesToSlices(files);
    if (!slices.length) {
      throw new Error("Не найден поддерживаемый файл. Выберите .mhd + .raw, .dcm/.dicom или изображение.");
    }

    state.pendingUpload = { slices, filesCount: files.length, format: slices[0].format };

    // Preview the file right away, before the patient card exists, so the
    // person can confirm this is the right scan before filling anything in.
    state.slices = slices;
    state.selectedStudyId = null;
    state.selectedStudy = null;
    state.currentSlice = Math.floor((slices.length - 1) / 2);
    state.detections = [];
    state.zoom = 1;
    state.pan = { x: 0, y: 0 };
    state.windowOverride = null;

    el.emptyState.classList.add("hidden");
    el.viewerPanel.classList.remove("hidden");
    el.viewerTitle.textContent = "Новый файл";
    el.studyInfo.textContent = `${slices.length} срез(ов) · ${slices[0].format} · пациент ещё не выбран`;
    el.modelStatus.textContent = "Файл загружен.";
    el.reportButton.classList.add("hidden");
    el.reportPanel.classList.add("hidden");
    el.reportText.value = "";
    el.findingCount.textContent = "0";
    el.findingsList.innerHTML = "<p>Пока нет анализа.</p>";

    hydrateControls();
    render();

    if (state.pendingTargetPatientId) {
      // "+ Добавить исследование" was clicked on a specific patient's card
      // first - we already know who this file is for, skip the form/dedup
      // step and attach it straight to that patient.
      const targetPatientId = state.pendingTargetPatientId;
      state.pendingTargetPatientId = null;
      if (el.dropZoneHint) el.dropZoneHint.textContent = defaultDropZoneHint;
      await finalizeUploadWithExistingPatient(targetPatientId);
    } else {
      showPendingUploadCta();
    }
  } catch (error) {
    showToast(error.message);
  } finally {
    el.fileInput.value = "";
    el.folderInput.value = "";
  }
}

function showPendingUploadCta() {
  el.pendingUploadPanel.classList.remove("hidden");
  el.patientForm.classList.add("hidden");
  el.patientMatchPrompt.classList.add("hidden");
  const pending = state.pendingUpload;
  el.pendingUploadSummary.textContent = pending
    ? `Загружено: ${pending.slices.length} срез(ов), формат ${pending.format}`
    : "";
}

function showPatientCardForm() {
  if (!state.pendingUpload) return;
  el.pendingUploadPanel.classList.add("hidden");
  el.patientMatchPrompt.classList.add("hidden");
  el.patientForm.classList.remove("hidden");
  el.patientName.value = "";
  el.birthDate.value = "";
  setFieldError(el.patientName, el.patientNameError, "");
  setFieldError(el.birthDate, el.birthDateError, "");
  el.patientName.focus();
}

function resetUploadFlow() {
  state.pendingUpload = null;
  state.pendingMatch = null;
  state.pendingPatientInfo = null;
  state.pendingTargetPatientId = null;
  if (el.dropZoneHint) el.dropZoneHint.textContent = defaultDropZoneHint;
  el.pendingUploadPanel.classList.add("hidden");
  el.patientForm.classList.add("hidden");
  el.patientMatchPrompt.classList.add("hidden");
  el.patientName.value = "";
  el.birthDate.value = "";
  setFieldError(el.patientName, el.patientNameError, "");
  setFieldError(el.birthDate, el.birthDateError, "");
}

function cancelPendingUpload() {
  resetUploadFlow();
  resetViewer();
}

async function handlePatientFormSubmit(event) {
  event.preventDefault();
  if (!state.pendingUpload) {
    showToast("Сначала загрузите файл");
    return;
  }
  const { valid, patientName, birthDate } = validatePatientForm();
  if (!valid) return;

  state.pendingPatientInfo = { patientName, birthDate };
  try {
    const result = await api("/api/patients/match", {
      method: "POST",
      body: { full_name: patientName, birth_date: birthDate }
    });
    if (result.match) {
      showPatientMatchPrompt(result.match);
    } else {
      await handleCreateNewPatient();
    }
  } catch (error) {
    showToast(error.message);
  }
}

function showPatientMatchPrompt(match) {
  state.pendingMatch = match;
  el.patientForm.classList.add("hidden");
  el.patientMatchPrompt.classList.remove("hidden");
  el.patientMatchText.textContent =
    `Пациент «${match.full_name}» (${formatBirthDate(match.birth_date)}) уже есть в базе, ` +
    `${match.study_count} исслед. Добавить это исследование в его карту или завести нового пациента?`;
}

async function handleUseExistingPatient() {
  if (!state.pendingMatch) return;
  await finalizeUploadWithExistingPatient(state.pendingMatch.id);
}

async function handleCreateNewPatient() {
  const info = state.pendingPatientInfo;
  if (!info) return;
  try {
    const result = await api("/api/patients", {
      method: "POST",
      body: { full_name: info.patientName, birth_date: info.birthDate }
    });
    await finalizeUploadWithExistingPatient(result.patient.id);
  } catch (error) {
    showToast(error.message);
  }
}

async function finalizeUploadWithExistingPatient(patientId) {
  const pending = state.pendingUpload;
  if (!pending) return;
  try {
    const result = await api(`/api/patients/${patientId}/studies`, {
      method: "POST",
      body: { description: `Загружено файлов: ${pending.filesCount}` }
    });
    if (result.user) setUser(result.user);
    const study = result.study;

    state.slices = pending.slices;
    state.selectedStudyId = study.id;
    state.selectedStudy = study;
    state.currentSlice = Math.floor((pending.slices.length - 1) / 2);
    state.detections = [];
    state.zoom = 1;
    state.pan = { x: 0, y: 0 };
    state.windowOverride = null;

    el.emptyState.classList.add("hidden");
    el.viewerPanel.classList.remove("hidden");
    el.viewerTitle.textContent = study.patient_name;
    el.studyInfo.textContent = `${pending.slices.length} срез(ов) · ${pending.format} · ${formatBirthDate(study.birth_date)}`;
    el.modelStatus.textContent = "Модель анализирует...";
    el.reportButton.classList.add("hidden");
    el.reportPanel.classList.add("hidden");
    el.reportText.value = "";

    hydrateControls();
    render();

    resetUploadFlow();
    state.expandedPatientId = patientId;
    await runAnalysisJob();
    await refreshPatientsAndExpandedCard();
  } catch (error) {
    showToast(error.message);
  }
}

async function filesToSlices(files) {
  const byExtension = groupByExtension(files);
  if (byExtension.mhd.length) {
    return parseMhdStudy(files, byExtension.mhd[0]);
  }
  if (byExtension.dcm.length || byExtension.dicom.length) {
    const dicomFiles = [...byExtension.dcm, ...byExtension.dicom];
    const slices = await Promise.all(dicomFiles.map(parseDicomFile));
    return slices.sort(compareMedicalSlices);
  }

  const imageFiles = files.filter(file => file.type.startsWith("image/"));
  if (imageFiles.length) {
    return Promise.all(imageFiles.map(loadImageFile));
  }
  return [];
}

function groupByExtension(files) {
  const grouped = { mhd: [], raw: [], dcm: [], dicom: [] };
  for (const file of files) {
    const ext = extensionOf(file.name);
    if (grouped[ext]) grouped[ext].push(file);
  }
  return grouped;
}

async function loadImageFile(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      resolve({
        format: "IMAGE",
        name: file.name,
        url,
        image,
        width: image.naturalWidth,
        height: image.naturalHeight
      });
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error(`Не удалось открыть файл: ${file.name}`));
    };
    image.src = url;
  });
}

async function parseMhdStudy(files, mhdFile) {
  const header = parseMhdHeader(await mhdFile.text());
  const dims = numbers(header.dimsize);
  if (dims.length < 2) throw new Error("В .mhd не найден DimSize.");

  const width = dims[0];
  const height = dims[1];
  const depth = dims[2] || 1;
  const rawName = header.elementdatafile;
  if (!rawName || rawName.toUpperCase() === "LOCAL") {
    throw new Error("Поддерживается .mhd, который ссылается на отдельный .raw файл.");
  }

  const rawFile = findRelatedRaw(files, rawName);
  if (!rawFile) {
    throw new Error(`Не найден RAW-файл из MHD: ${rawName}. Выберите .mhd и .raw вместе или всю папку.`);
  }

  const littleEndian = !isTrue(header.binarydatabyteordermsb || header.elementbyteordermsb);
  const offset = Number(header.headersize || 0) > 0 ? Number(header.headersize) : 0;
  const rawBuffer = (await rawFile.arrayBuffer()).slice(offset);
  const data = readMetaImagePixels(rawBuffer, header.elementtype, littleEndian);
  const pixelsPerSlice = width * height;
  const expected = pixelsPerSlice * depth;
  if (data.length < expected) {
    throw new Error(`RAW короче ожидаемого размера: нужно ${expected}, есть ${data.length}.`);
  }

  const { min, max } = minMax(data, expected);
  const spacing = numbers(header.elementspacing || header.elementsize);
  const windowWidth = max > min ? max - min : 1;
  const windowCenter = min + windowWidth / 2;
  const slices = [];

  for (let z = 0; z < depth; z += 1) {
    const start = z * pixelsPerSlice;
    slices.push({
      format: "MHD/RAW",
      name: `${mhdFile.name} · ${z + 1}`,
      width,
      height,
      pixelData: data.subarray(start, start + pixelsPerSlice),
      pixelMin: min,
      pixelMax: max,
      windowCenter,
      windowWidth,
      spacing,
      instanceNumber: z + 1
    });
  }

  return slices;
}

function parseMhdHeader(text) {
  const header = {};
  for (const line of text.split(/\r?\n/)) {
    const clean = line.split("#")[0].trim();
    if (!clean || !clean.includes("=")) continue;
    const [key, ...rest] = clean.split("=");
    header[key.trim().toLowerCase()] = rest.join("=").trim();
  }
  return header;
}

function findRelatedRaw(files, rawName) {
  const wanted = basename(rawName).toLowerCase();
  return files.find(file => file.name.toLowerCase() === wanted)
    || files.find(file => basename(file.webkitRelativePath || file.name).toLowerCase() === wanted);
}

function readMetaImagePixels(buffer, elementType = "MET_SHORT", littleEndian = true) {
  const type = String(elementType || "MET_SHORT").toUpperCase();
  const constructors = {
    MET_CHAR: Int8Array,
    MET_UCHAR: Uint8Array,
    MET_SHORT: Int16Array,
    MET_USHORT: Uint16Array,
    MET_INT: Int32Array,
    MET_UINT: Uint32Array,
    MET_FLOAT: Float32Array,
    MET_DOUBLE: Float64Array
  };

  if (!constructors[type]) {
    throw new Error(`Тип MHD не поддерживается: ${elementType}`);
  }

  if (littleEndian || type === "MET_CHAR" || type === "MET_UCHAR") {
    return new constructors[type](buffer);
  }

  const bytes = constructors[type].BYTES_PER_ELEMENT;
  const length = Math.floor(buffer.byteLength / bytes);
  const view = new DataView(buffer);
  const output = new constructors[type](length);
  for (let i = 0; i < length; i += 1) {
    const offset = i * bytes;
    if (type === "MET_SHORT") output[i] = view.getInt16(offset, false);
    if (type === "MET_USHORT") output[i] = view.getUint16(offset, false);
    if (type === "MET_INT") output[i] = view.getInt32(offset, false);
    if (type === "MET_UINT") output[i] = view.getUint32(offset, false);
    if (type === "MET_FLOAT") output[i] = view.getFloat32(offset, false);
    if (type === "MET_DOUBLE") output[i] = view.getFloat64(offset, false);
  }
  return output;
}

async function parseDicomFile(file) {
  const image = parseDicom(await file.arrayBuffer(), file.name);
  return {
    format: "DICOM",
    name: file.name,
    width: image.columns,
    height: image.rows,
    pixelData: image.pixelData,
    pixelMin: image.pixelMin,
    pixelMax: image.pixelMax,
    windowCenter: image.windowCenter ?? (image.pixelMin + image.pixelMax) / 2,
    windowWidth: image.windowWidth ?? Math.max(1, image.pixelMax - image.pixelMin),
    rescaleSlope: image.rescaleSlope,
    rescaleIntercept: image.rescaleIntercept,
    instanceNumber: image.instanceNumber,
    imagePosition: image.imagePosition,
    sliceLocation: image.sliceLocation
  };
}

function parseDicom(buffer, filename) {
  const view = new DataView(buffer);
  let offset = buffer.byteLength > 132 && readAscii(view, 128, 4) === "DICM" ? 132 : 0;
  const elements = new Map();
  let transferSyntax = "1.2.840.10008.1.2.1";
  let explicitVR = true;
  let littleEndian = true;
  let pixelOffset = -1;
  let pixelLength = 0;

  while (offset + 8 <= view.byteLength) {
    const group = view.getUint16(offset, true);
    const element = view.getUint16(offset + 2, true);
    const tag = tagKey(group, element);
    offset += 4;

    if (group !== 0x0002) {
      explicitVR = transferSyntax !== "1.2.840.10008.1.2";
      littleEndian = transferSyntax !== "1.2.840.10008.1.2.2";
    }
    if (!littleEndian) throw new Error(`${filename}: Big Endian DICOM не поддерживается.`);

    let vr = "UN";
    let length = 0;
    if (explicitVR || group === 0x0002) {
      vr = readAscii(view, offset, 2);
      offset += 2;
      if (["OB", "OD", "OF", "OL", "OV", "OW", "SQ", "SV", "UC", "UR", "UT", "UN", "UV"].includes(vr)) {
        offset += 2;
        length = view.getUint32(offset, true);
        offset += 4;
      } else {
        length = view.getUint16(offset, true);
        offset += 2;
      }
    } else {
      vr = implicitVRForTag(tag);
      length = view.getUint32(offset, true);
      offset += 4;
    }

    if (length === 0xffffffff) {
      if (tag === "7fe0,0010") {
        throw new Error(`${filename}: сжатый DICOM не поддерживается.`);
      }
      break;
    }
    if (offset + length > view.byteLength) break;

    if (tag === "7fe0,0010") {
      pixelOffset = offset;
      pixelLength = length;
      break;
    }

    if (isUsefulDicomTag(tag)) {
      elements.set(tag, readDicomValue(view, offset, length, vr));
      if (tag === "0002,0010") {
        transferSyntax = String(elements.get(tag)).replace(/\0/g, "").trim();
        if (!["1.2.840.10008.1.2", "1.2.840.10008.1.2.1"].includes(transferSyntax)) {
          throw new Error(`${filename}: Transfer Syntax ${transferSyntax} не поддерживается.`);
        }
      }
    }

    offset += length + (length % 2);
  }

  if (pixelOffset < 0) throw new Error(`${filename}: PixelData не найден.`);

  const rows = numberValue(elements.get("0028,0010"));
  const columns = numberValue(elements.get("0028,0011"));
  const bitsAllocated = numberValue(elements.get("0028,0100")) || 16;
  const pixelRepresentation = numberValue(elements.get("0028,0103")) || 0;
  if (!rows || !columns) throw new Error(`${filename}: не найдены Rows/Columns.`);
  if (bitsAllocated !== 16 && bitsAllocated !== 8) {
    throw new Error(`${filename}: BitsAllocated ${bitsAllocated} не поддерживается.`);
  }

  const pixelCount = rows * columns;
  let pixelData;
  if (bitsAllocated === 16) {
    const source = new DataView(buffer, pixelOffset, Math.min(pixelLength, pixelCount * 2));
    pixelData = pixelRepresentation === 1 ? new Int16Array(pixelCount) : new Uint16Array(pixelCount);
    for (let i = 0; i < pixelCount; i += 1) {
      pixelData[i] = pixelRepresentation === 1
        ? source.getInt16(i * 2, true)
        : source.getUint16(i * 2, true);
    }
  } else {
    const source = new Uint8Array(buffer, pixelOffset, Math.min(pixelLength, pixelCount));
    pixelData = new Uint16Array(pixelCount);
    for (let i = 0; i < pixelCount; i += 1) pixelData[i] = source[i];
  }

  const rescaleSlope = numberValue(elements.get("0028,1053")) || 1;
  const rescaleIntercept = numberValue(elements.get("0028,1052")) ?? 0;
  const scaled = new Float32Array(pixelData.length);
  for (let i = 0; i < pixelData.length; i += 1) {
    scaled[i] = pixelData[i] * rescaleSlope + rescaleIntercept;
  }
  const { min, max } = minMax(scaled, scaled.length);

  return {
    rows,
    columns,
    pixelData: scaled,
    pixelMin: min,
    pixelMax: max,
    windowCenter: firstNumber(elements.get("0028,1050")),
    windowWidth: firstNumber(elements.get("0028,1051")),
    rescaleIntercept,
    rescaleSlope,
    instanceNumber: numberValue(elements.get("0020,0013")),
    imagePosition: numbers(elements.get("0020,0032")),
    sliceLocation: numberValue(elements.get("0020,1041"))
  };
}

async function runAnalysisJob() {
  if (!state.selectedStudyId) return;
  el.modelStatus.textContent = "Модель анализирует...";
  showAnalysisProgress();
  try {
    const started = await api(`/api/studies/${state.selectedStudyId}/analyze`, {
      method: "POST",
      body: { slices: state.slices.map(serializeSliceForModel) }
    });
    const job = await pollJob(started.job.id, setAnalysisProgress);
    if (job.status === "failed") {
      throw new Error(job.error || "не удалось проанализировать снимок");
    }

    const findings = (job.result && job.result.findings) || [];
    state.detections = findings.map(normalizeFinding);
    const modelInfo = (job.result && job.result.model) || {};
    const first = state.detections[0];
    if (first) {
      state.currentSlice = first.sliceIndex;
      el.modelStatus.textContent = `Вероятность ${Math.round(first.confidence * 100)}% · ${modelInfo.name || "модель"}`;
    } else {
      const probability = Number(modelInfo.probability || 0);
      el.modelStatus.textContent = probability
        ? `Вероятность ${Math.round(probability * 100)}%, ниже порога`
        : "Патологий не найдено";
    }
    el.reportButton.classList.remove("hidden");
    hydrateControls();
    render();
    renderFindings();
  } catch (error) {
    el.modelStatus.textContent = `Ошибка модели: ${error.message}`;
    showToast(`Анализ не удался: ${error.message}`);
  } finally {
    hideAnalysisProgress();
  }
}

function showAnalysisProgress() {
  el.analysisProgress.classList.remove("hidden");
  setAnalysisProgress(0);
  startLoadingAnimation();
}

function setAnalysisProgress(fraction) {
  el.analysisProgressBar.style.width = `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%`;
}

function hideAnalysisProgress() {
  el.analysisProgress.classList.add("hidden");
  setAnalysisProgress(0);
  stopLoadingAnimation();
}

const LOADING_ANIMATION_FRAME_MS = 90;
let loadingAnimationTimer = null;
let loadingAnimationIndex = 0;

function startLoadingAnimation(caption) {
  if (!el.loadingOverlay || !el.loadingCanvas) return;
  el.loadingCaptionText.textContent = caption || "Модель анализирует срезы";
  el.loadingOverlay.classList.remove("hidden");
  // Force layout before adding "visible" so the opacity transition actually
  // plays instead of jumping straight to the end state.
  void el.loadingOverlay.offsetWidth;
  el.loadingOverlay.classList.add("visible");

  loadingAnimationIndex = 0;
  stepLoadingAnimation();
  clearInterval(loadingAnimationTimer);
  loadingAnimationTimer = setInterval(stepLoadingAnimation, LOADING_ANIMATION_FRAME_MS);
}

function stepLoadingAnimation() {
  if (!el.loadingCanvas || !state.slices.length) return;
  const slice = state.slices[loadingAnimationIndex % state.slices.length];
  loadingAnimationIndex += 1;

  const context = el.loadingCanvas.getContext("2d");
  el.loadingCanvas.width = slice.width;
  el.loadingCanvas.height = slice.height;
  if (slice.image) {
    context.clearRect(0, 0, slice.width, slice.height);
    context.drawImage(slice.image, 0, 0);
  } else {
    renderPixelSlice(context, slice);
  }
}

function stopLoadingAnimation() {
  clearInterval(loadingAnimationTimer);
  loadingAnimationTimer = null;
  if (!el.loadingOverlay) return;
  el.loadingOverlay.classList.remove("visible");
  setTimeout(() => {
    if (!loadingAnimationTimer) el.loadingOverlay.classList.add("hidden");
  }, 260);
}

async function pollJob(jobId, onProgress) {
  const maxAttempts = 150;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const result = await api(`/api/jobs/${jobId}`);
    const job = result.job;
    if (job.status === "done" || job.status === "failed") {
      if (onProgress) onProgress(1);
      return job;
    }
    // The real remaining time is unknown, so the bar approaches - but never
    // quite reaches - 100% while waiting, then jumps to 100% on completion.
    if (onProgress) onProgress(Math.min(0.92, attempt / maxAttempts));
    await sleep(400);
  }
  throw new Error("анализ занял слишком много времени");
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function serializeSliceForModel(slice) {
  const serialized = {
    name: slice.name,
    width: slice.width,
    height: slice.height,
    rows: slice.height,
    columns: slice.width,
    format: slice.format,
    pixelSpacing: slice.spacing || slice.pixelSpacing,
    sliceThickness: slice.sliceThickness,
    rescaleSlope: 1,
    rescaleIntercept: 0
  };

  if (slice.pixelData) {
    serialized.pixelData = encodePixelDataForModel(slice.pixelData);
  }

  return serialized;
}

function encodePixelDataForModel(pixelData) {
  if (pixelData instanceof Float32Array || pixelData instanceof Float64Array) {
    const quantized = new Int16Array(pixelData.length);
    for (let i = 0; i < pixelData.length; i += 1) {
      const value = Math.round(pixelData[i]);
      quantized[i] = Math.max(-32768, Math.min(32767, value));
    }
    return { dtype: "Int16Array", data: arrayBufferToBase64(quantized.buffer) };
  }

  return {
    dtype: pixelData.constructor?.name || "Int16Array",
    data: arrayBufferToBase64(pixelData.buffer, pixelData.byteOffset, pixelData.byteLength)
  };
}

function arrayBufferToBase64(buffer, byteOffset = 0, byteLength = buffer.byteLength) {
  const bytes = new Uint8Array(buffer, byteOffset, byteLength);
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const chunk = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

function normalizeFinding(finding) {
  return {
    id: finding.id,
    title: finding.title || "Подозрительный объект",
    confidence: Number(finding.confidence || 0),
    diameterMm: finding.diameter_mm,
    sliceIndex: Math.max(0, Math.min(state.slices.length - 1, Number(finding.slice_index ?? 0))),
    x: Number(finding.x || 0),
    y: Number(finding.y || 0),
    width: Number(finding.width || 0),
    height: Number(finding.height || 0)
  };
}

async function generateReport() {
  if (!state.selectedStudyId) return;
  el.reportButton.disabled = true;
  el.reportButton.textContent = "Создаю...";
  try {
    const result = await api("/api/reports/generate", {
      method: "POST",
      body: {
        study_id: state.selectedStudyId,
        patient_name: (state.selectedStudy && state.selectedStudy.patient_name) || "",
        birth_date: (state.selectedStudy && state.selectedStudy.birth_date) || "",
        slices_count: state.slices.length,
        detections: state.detections
      }
    });
    el.reportPanel.classList.remove("hidden");
    el.reportText.value = result.report;
    setReportSourceBadge(result.source);
    if (result.study) state.selectedStudy = result.study;
    await refreshPatientsAndExpandedCard();
  } catch (error) {
    showToast(`Не удалось создать заключение: ${error.message}`);
  } finally {
    el.reportButton.disabled = false;
    el.reportButton.textContent = "Создать заключение";
  }
}

function hydrateControls() {
  const last = Math.max(0, state.slices.length - 1);
  el.sliceSlider.max = String(last);
  el.sliceSlider.value = String(state.currentSlice);
  el.prevButton.disabled = state.currentSlice <= 0;
  el.nextButton.disabled = state.currentSlice >= last;
  el.sliceLabel.textContent = state.slices.length
    ? `Срез ${state.currentSlice + 1} из ${state.slices.length}`
    : "Срез 0 из 0";
  el.zoomSlider.value = String(Math.round(state.zoom * 100));
  el.zoomLabel.textContent = `${Math.round(state.zoom * 100)}%`;
  updateWindowControls();
}

function currentSliceWindow() {
  const slice = state.slices[state.currentSlice];
  if (state.windowOverride) return state.windowOverride;
  if (!slice) return { center: 0, width: 1 };
  return {
    center: Number(slice.windowCenter ?? (slice.pixelMin + slice.pixelMax) / 2 ?? 0),
    width: Math.max(1, Number(slice.windowWidth ?? (slice.pixelMax - slice.pixelMin) ?? 1))
  };
}

function updateWindowControls() {
  const slice = state.slices[state.currentSlice];
  // Only slices with real HU pixel data (DICOM/MHD) can be windowed - a
  // plain image (PNG/JPEG fallback) has no HU values to remap.
  const hasPixelData = Boolean(slice && slice.pixelData && !slice.image);
  for (const control of [el.wlLungButton, el.wlBoneButton, el.wlSoftButton, el.wlAutoButton, el.wlCenterSlider, el.wlWidthSlider]) {
    control.disabled = !hasPixelData;
  }
  if (!hasPixelData) {
    el.wlCenterLabel.textContent = "—";
    el.wlWidthLabel.textContent = "—";
    return;
  }
  const { center, width } = currentSliceWindow();
  el.wlCenterSlider.value = String(Math.round(center));
  el.wlWidthSlider.value = String(Math.round(width));
  el.wlCenterLabel.textContent = `${Math.round(center)} HU`;
  el.wlWidthLabel.textContent = `${Math.round(width)} HU`;
}

function applyWindowPreset(name) {
  state.windowOverride = name === "auto" ? null : { ...WINDOW_PRESETS[name] };
  updateWindowControls();
  render();
}

function handleWindowSliderInput() {
  state.windowOverride = {
    center: Number(el.wlCenterSlider.value),
    width: Math.max(1, Number(el.wlWidthSlider.value))
  };
  el.wlCenterLabel.textContent = `${state.windowOverride.center} HU`;
  el.wlWidthLabel.textContent = `${state.windowOverride.width} HU`;
  render();
}

function setSlice(index) {
  if (!state.slices.length) return;
  state.currentSlice = Math.max(0, Math.min(state.slices.length - 1, index));
  hydrateControls();
  render();
}

function setZoom(value) {
  state.zoom = Math.max(0.8, Math.min(3, value));
  hydrateControls();
  if (state.slices.length) fitCanvases(state.slices[state.currentSlice]);
}

function adjustZoom(delta) {
  setZoom(Math.round((state.zoom + delta) * 10) / 10);
}

function render() {
  if (!state.slices.length) return;

  const slice = state.slices[state.currentSlice];
  const imageCanvas = el.imageCanvas;
  const overlayCanvas = el.overlayCanvas;
  const imageContext = imageCanvas.getContext("2d");

  imageCanvas.width = slice.width;
  imageCanvas.height = slice.height;
  overlayCanvas.width = slice.width;
  overlayCanvas.height = slice.height;
  imageContext.clearRect(0, 0, slice.width, slice.height);

  if (slice.image) {
    imageContext.drawImage(slice.image, 0, 0);
  } else {
    renderPixelSlice(imageContext, slice);
  }

  fitCanvases(slice);
  drawDetections();
}

function renderPixelSlice(context, slice) {
  const imageData = context.createImageData(slice.width, slice.height);
  const override = state.windowOverride;
  const center = Number(override?.center ?? slice.windowCenter ?? ((slice.pixelMin + slice.pixelMax) / 2));
  const width = Math.max(1, Number(override?.width ?? slice.windowWidth ?? (slice.pixelMax - slice.pixelMin)));
  const low = center - width / 2;

  for (let i = 0; i < slice.pixelData.length; i += 1) {
    let value = ((slice.pixelData[i] - low) / width) * 255;
    value = Math.max(0, Math.min(255, value));
    const offset = i * 4;
    imageData.data[offset] = value;
    imageData.data[offset + 1] = value;
    imageData.data[offset + 2] = value;
    imageData.data[offset + 3] = 255;
  }

  context.putImageData(imageData, 0, 0);
}

function fitCanvases(slice) {
  const rect = el.viewer.getBoundingClientRect();
  const scale = Math.min(rect.width / slice.width, rect.height / slice.height, 1) * state.zoom;
  const width = Math.max(1, Math.floor(slice.width * scale));
  const height = Math.max(1, Math.floor(slice.height * scale));

  state.pan = clampPan(state.pan, width, height, rect.width, rect.height);

  for (const canvas of [el.imageCanvas, el.overlayCanvas]) {
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    canvas.style.transform = `translate(${state.pan.x}px, ${state.pan.y}px)`;
  }
}

function clampPan(pan, contentWidth, contentHeight, viewportWidth, viewportHeight) {
  // The image is centered in the viewer, so it can be dragged until its edge
  // reaches the viewer's edge (plus a little slack) - no further.
  const slack = 40;
  const maxX = Math.max(0, (contentWidth - viewportWidth) / 2 + slack);
  const maxY = Math.max(0, (contentHeight - viewportHeight) / 2 + slack);
  return {
    x: Math.max(-maxX, Math.min(maxX, pan.x)),
    y: Math.max(-maxY, Math.min(maxY, pan.y))
  };
}

function resetView() {
  state.zoom = 1;
  state.pan = { x: 0, y: 0 };
  hydrateControls();
  if (state.slices.length) fitCanvases(state.slices[state.currentSlice]);
}

function startPan(event) {
  if (event.button !== 0 || !state.slices.length) return;
  // Ignore drags that start on a control sitting on top of the viewer.
  if (event.target.closest("button, input, .model-status, .analysis-progress")) return;
  state.isPanning = true;
  state.panStart = { x: event.clientX, y: event.clientY };
  state.panOrigin = { ...state.pan };
  el.viewer.classList.add("panning");
  window.addEventListener("pointermove", handlePanMove);
  window.addEventListener("pointerup", stopPan, { once: true });
  event.preventDefault();
}

function handlePanMove(event) {
  if (!state.isPanning) return;
  const dx = event.clientX - state.panStart.x;
  const dy = event.clientY - state.panStart.y;
  state.pan = { x: state.panOrigin.x + dx, y: state.panOrigin.y + dy };
  if (state.slices.length) fitCanvases(state.slices[state.currentSlice]);
}

function stopPan() {
  state.isPanning = false;
  el.viewer.classList.remove("panning");
  window.removeEventListener("pointermove", handlePanMove);
}

function drawDetections() {
  const canvas = el.overlayCanvas;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);

  const visible = state.detections.filter(item => item.sliceIndex === state.currentSlice);
  for (const item of visible) {
    context.save();
    context.strokeStyle = "#ff3b30";
    context.lineWidth = Math.max(4, canvas.width / 180);
    context.shadowColor = "rgba(255, 59, 48, 0.5)";
    context.shadowBlur = 12;
    context.strokeRect(item.x, item.y, item.width, item.height);
    context.fillStyle = "rgba(255, 59, 48, 0.16)";
    context.fillRect(item.x, item.y, item.width, item.height);
    context.shadowBlur = 0;
    context.fillStyle = "#ff3b30";
    context.font = `${Math.max(18, canvas.width / 32)}px sans-serif`;
    context.fillText(
      `${item.title} ${Math.round(item.confidence * 100)}%`,
      item.x,
      Math.max(24, item.y - 10)
    );
    context.restore();
  }
}

function renderFindings() {
  el.findingCount.textContent = state.detections.length;
  if (!state.detections.length) {
    el.findingsList.innerHTML = "<p>Патологий не найдено.</p>";
    return;
  }

  el.findingsList.innerHTML = state.detections.map(item => `
    <button class="finding" type="button" data-slice="${item.sliceIndex}">
      <strong>${escapeHtml(item.title)}</strong>
      <span>Срез ${item.sliceIndex + 1} · ${Math.round(item.confidence * 100)}%</span>
    </button>
  `).join("");

  for (const button of el.findingsList.querySelectorAll("[data-slice]")) {
    button.addEventListener("click", () => setSlice(Number(button.dataset.slice)));
  }
}

function handleWheel(event) {
  if (!state.slices.length) return;
  event.preventDefault();
  if (event.ctrlKey || event.metaKey) {
    adjustZoom(event.deltaY > 0 ? -0.1 : 0.1);
    return;
  }
  setSlice(state.currentSlice + (event.deltaY > 0 ? 1 : -1));
}

function handleKeydown(event) {
  if (event.key === "ArrowLeft" || event.key === "ArrowUp") setSlice(state.currentSlice - 1);
  if (event.key === "ArrowRight" || event.key === "ArrowDown") setSlice(state.currentSlice + 1);
  if (event.key === "+" || event.key === "=") adjustZoom(0.1);
  if (event.key === "-") adjustZoom(-0.1);
}

async function api(path, options = {}) {
  const headers = {};
  if (options.body) headers["Content-Type"] = "application/json";
  if (state.token) headers["X-Auth-Token"] = state.token;
  const response = await fetch(`${API_BASE}${path}`, {
    method: options.method || "GET",
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined
  });
  const result = await response.json();
  if (!response.ok || !result.ok) {
    if (response.status === 401 && !path.startsWith("/api/auth")) {
      clearAuth();
      showAuth();
    }
    throw new Error(result.error || "API error");
  }
  return result;
}

function clearOldUrls() {
  for (const slice of state.slices) {
    if (slice.url) URL.revokeObjectURL(slice.url);
  }
}

function compareMedicalSlices(a, b) {
  const az = Array.isArray(a.imagePosition) ? Number(a.imagePosition[2]) : null;
  const bz = Array.isArray(b.imagePosition) ? Number(b.imagePosition[2]) : null;
  if (Number.isFinite(az) && Number.isFinite(bz) && az !== bz) return az - bz;
  if (Number.isFinite(a.sliceLocation) && Number.isFinite(b.sliceLocation) && a.sliceLocation !== b.sliceLocation) {
    return a.sliceLocation - b.sliceLocation;
  }
  return (a.instanceNumber || 0) - (b.instanceNumber || 0);
}

function minMax(data, length = data.length) {
  let min = Infinity;
  let max = -Infinity;
  for (let i = 0; i < length; i += 1) {
    const value = Number(data[i]);
    if (value < min) min = value;
    if (value > max) max = value;
  }
  return { min: Number.isFinite(min) ? min : 0, max: Number.isFinite(max) ? max : 1 };
}

function isUsefulDicomTag(tag) {
  return [
    "0002,0010", "0020,0013", "0020,0032", "0020,1041",
    "0028,0010", "0028,0011", "0028,0100", "0028,0103",
    "0028,1050", "0028,1051", "0028,1052", "0028,1053"
  ].includes(tag);
}

function implicitVRForTag(tag) {
  const vrMap = {
    "0028,0010": "US",
    "0028,0011": "US",
    "0028,0100": "US",
    "0028,0103": "US",
    "0020,0013": "IS"
  };
  return vrMap[tag] || "LO";
}

function readDicomValue(view, offset, length, vr) {
  if (length <= 0) return "";
  if (vr === "US") return view.getUint16(offset, true);
  if (vr === "SS") return view.getInt16(offset, true);
  if (vr === "UL") return view.getUint32(offset, true);
  if (vr === "SL") return view.getInt32(offset, true);
  if (vr === "FL") return view.getFloat32(offset, true);
  if (vr === "FD") return view.getFloat64(offset, true);
  return readAscii(view, offset, length).replace(/\0/g, "").trim();
}

function readAscii(view, offset, length) {
  const bytes = new Uint8Array(view.buffer, view.byteOffset + offset, length);
  let output = "";
  for (const byte of bytes) output += String.fromCharCode(byte);
  return output;
}

function tagKey(group, element) {
  return `${group.toString(16).padStart(4, "0")},${element.toString(16).padStart(4, "0")}`;
}

function firstNumber(value) {
  return numberValue(value);
}

function numberValue(value) {
  if (value == null || value === "") return null;
  if (typeof value === "number") return value;
  const parsed = Number(String(value).split("\\")[0].trim());
  return Number.isFinite(parsed) ? parsed : null;
}

function numbers(value) {
  if (value == null || value === "") return [];
  if (typeof value === "number") return [value];
  return String(value)
    .split(/[\\\s]+/)
    .map(part => Number(part.trim()))
    .filter(Number.isFinite);
}

function isTrue(value) {
  return ["true", "1", "yes"].includes(String(value || "").trim().toLowerCase());
}

function extensionOf(name) {
  const clean = basename(name).toLowerCase();
  const dot = clean.lastIndexOf(".");
  return dot >= 0 ? clean.slice(dot + 1) : "";
}

function basename(path) {
  return String(path || "").split(/[\\/]/).pop();
}

function formatBirthDate(value) {
  if (!value) return "дата рождения не указана";
  const parts = String(value).split("-");
  if (parts.length === 3) return `${parts[2]}.${parts[1]}.${parts[0]}`;
  return value;
}

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function formatRubles(value) {
  const amount = Number(value || 0);
  return `${amount.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
