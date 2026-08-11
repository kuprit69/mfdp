const API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:8765" : "";
const AUTH_TOKEN_KEY = "lungPrometheusAuthToken";

const state = {
  token: localStorage.getItem(AUTH_TOKEN_KEY) || "",
  user: null,
  studies: [],
  selectedStudyId: null,
  selectedStudy: null,
  slices: [],
  currentSlice: 0,
  detections: [],
  zoom: 1
};

const el = {};

document.addEventListener("DOMContentLoaded", () => {
  for (const id of [
    "authPanel", "appLayout", "sessionPanel", "authUser", "balanceLabel",
    "requestPriceLabel", "topUpForm", "topUpAmount", "logoutButton",
    "showLoginButton", "showRegisterButton", "loginForm", "loginUsername",
    "loginPassword", "registerForm", "registerUsername", "registerPassword",
    "authMessage", "refreshButton", "studyCount", "studyList", "emptyState", "viewerPanel",
    "viewerTitle", "studyInfo", "reportButton", "viewer", "imageCanvas",
    "overlayCanvas", "modelStatus", "prevButton", "nextButton", "sliceSlider",
    "sliceLabel", "zoomOutButton", "zoomSlider", "zoomInButton", "zoomLabel",
    "findingCount", "findingsList", "reportPanel", "reportText",
    "patientForm", "patientName", "birthDate", "fileInput", "folderInput"
  ]) {
    el[id] = document.getElementById(id);
  }

  el.showLoginButton.addEventListener("click", () => setAuthMode("login"));
  el.showRegisterButton.addEventListener("click", () => setAuthMode("register"));
  el.loginForm.addEventListener("submit", handleLogin);
  el.registerForm.addEventListener("submit", handleRegister);
  el.topUpForm.addEventListener("submit", handleTopUp);
  el.logoutButton.addEventListener("click", logout);
  el.refreshButton.addEventListener("click", loadStudies);
  el.fileInput.addEventListener("change", event => loadFiles(event.target.files));
  el.folderInput.addEventListener("change", event => loadFiles(event.target.files));
  el.prevButton.addEventListener("click", () => setSlice(state.currentSlice - 1));
  el.nextButton.addEventListener("click", () => setSlice(state.currentSlice + 1));
  el.sliceSlider.addEventListener("input", event => setSlice(Number(event.target.value)));
  el.zoomSlider.addEventListener("input", event => setZoom(Number(event.target.value) / 100));
  el.zoomOutButton.addEventListener("click", () => adjustZoom(-0.1));
  el.zoomInButton.addEventListener("click", () => adjustZoom(0.1));
  el.reportButton.addEventListener("click", generateReport);
  el.viewer.addEventListener("wheel", handleWheel, { passive: false });
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
    await loadStudies();
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
    await loadStudies();
  } catch (error) {
    setAuthMessage(error.message);
  }
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
    alert(error.message);
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
  state.studies = [];
  state.selectedStudyId = null;
  state.selectedStudy = null;
}

function logout() {
  clearAuth();
  showAuth();
}

function setAuthMessage(message) {
  el.authMessage.textContent = message;
}

async function loadStudies() {
  if (!state.user) return;
  const result = await api("/api/studies");
  state.studies = result.studies;
  el.studyCount.textContent = state.studies.length;

  if (!state.studies.length) {
    el.studyList.innerHTML = '<div class="empty-line">Пока пусто</div>';
    resetViewer();
    return;
  }

  el.studyList.innerHTML = state.studies.map(study => `
    <div class="study-row">
      <button class="study-item ${study.id === state.selectedStudyId ? "active" : ""}" data-id="${study.id}" type="button">
        <strong>${escapeHtml(study.patient_name)}</strong>
        <span>${formatBirthDate(study.birth_date)} · ${study.finding_count} находок</span>
        <small>${escapeHtml(study.status)}</small>
      </button>
      <button class="delete-study" data-delete-id="${study.id}" type="button" title="Удалить" aria-label="Удалить запрос ${escapeHtml(study.patient_name)}">🗑</button>
    </div>
  `).join("");

  for (const button of el.studyList.querySelectorAll("[data-id]")) {
    button.addEventListener("click", () => selectStudy(button.dataset.id));
  }
  for (const button of el.studyList.querySelectorAll("[data-delete-id]")) {
    button.addEventListener("click", () => deleteStudy(button.dataset.deleteId));
  }
}

async function selectStudy(id) {
  state.selectedStudyId = id;
  const result = await api(`/api/studies/${id}`);
  state.selectedStudy = result.study;
  state.slices = [];
  state.detections = result.study.findings.map(finding => ({
    title: finding.title,
    confidence: finding.confidence,
    sliceIndex: 0,
    x: 0,
    y: 0,
    width: 0,
    height: 0
  }));
  el.patientName.value = result.study.patient_name || "";
  el.birthDate.value = result.study.birth_date || "";
  renderStudyListSelection();
  showHistoryDetails(result.study);
}

function showHistoryDetails(study) {
  el.emptyState.classList.add("hidden");
  el.viewerPanel.classList.remove("hidden");
  el.viewerTitle.textContent = study.patient_name;
  el.studyInfo.textContent = `Дата рождения: ${formatBirthDate(study.birth_date)}. Файл из истории не хранится в браузере.`;
  el.modelStatus.textContent = "Для просмотра снимков загрузите файл справа.";
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
  } else {
    el.reportPanel.classList.add("hidden");
    el.reportText.value = "";
  }
}

function renderStudyListSelection() {
  for (const button of el.studyList.querySelectorAll("[data-id]")) {
    button.classList.toggle("active", button.dataset.id === state.selectedStudyId);
  }
}

async function deleteStudy(id) {
  const study = state.studies.find(item => item.id === id);
  const name = study ? study.patient_name : "запрос";
  if (!confirm(`Удалить из истории: ${name}?`)) return;

  try {
    await api(`/api/studies/${id}`, { method: "DELETE" });
    if (state.selectedStudyId === id) {
      state.selectedStudyId = null;
      state.selectedStudy = null;
      resetViewer();
    }
    await loadStudies();
  } catch (error) {
    alert(error.message);
  }
}

function resetViewer() {
  clearOldUrls();
  state.slices = [];
  state.currentSlice = 0;
  state.detections = [];
  state.zoom = 1;
  el.emptyState.classList.remove("hidden");
  el.viewerPanel.classList.add("hidden");
  el.reportPanel.classList.add("hidden");
  el.reportText.value = "";
  if (el.imageCanvas && el.overlayCanvas) {
    el.imageCanvas.getContext("2d").clearRect(0, 0, el.imageCanvas.width, el.imageCanvas.height);
    el.overlayCanvas.getContext("2d").clearRect(0, 0, el.overlayCanvas.width, el.overlayCanvas.height);
  }
}

async function loadFiles(fileList) {
  try {
    if (!state.user) {
      throw new Error("требуется вход");
    }
    const files = Array.from(fileList || []);
    if (!files.length) return;

    const patientName = el.patientName.value.trim();
    const birthDate = el.birthDate.value.trim();
    if (!patientName || !birthDate) {
      alert("Заполните ФИО и дату рождения перед загрузкой файла.");
      return;
    }

    clearOldUrls();
    state.slices = await filesToSlices(files);
    if (!state.slices.length) {
      throw new Error("Не найден поддерживаемый файл. Выберите .mhd + .raw, .dcm/.dicom или изображение.");
    }

    const study = await createStudyForUpload(patientName, birthDate, files.length);
    state.selectedStudyId = study.id;
    state.selectedStudy = study;
    state.currentSlice = Math.floor((state.slices.length - 1) / 2);
    state.detections = [];
    state.zoom = 1;

    el.emptyState.classList.add("hidden");
    el.viewerPanel.classList.remove("hidden");
    el.viewerTitle.textContent = patientName;
    el.studyInfo.textContent = `${state.slices.length} срез(ов) · ${state.slices[0].format} · ${formatBirthDate(birthDate)}`;
    el.modelStatus.textContent = "Модель анализирует...";
    el.reportButton.classList.add("hidden");
    el.reportPanel.classList.add("hidden");
    el.reportText.value = "";

    hydrateControls();
    render();
    await analyzeWithPython();
    await loadStudies();
  } catch (error) {
    alert(error.message);
  } finally {
    el.fileInput.value = "";
    el.folderInput.value = "";
  }
}

async function createStudyForUpload(patientName, birthDate, filesCount) {
  const result = await api("/api/studies", {
    method: "POST",
    body: {
      patient_name: patientName,
      birth_date: birthDate,
      description: `Загружено файлов: ${filesCount}`
    }
  });
  if (result.user) setUser(result.user);
  return result.study;
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

async function analyzeWithPython() {
  try {
    const result = await api("/api/viewer/analyze", {
      method: "POST",
      body: {
        slices: state.slices.map(slice => ({
          name: slice.name,
          width: slice.width,
          height: slice.height
        }))
      }
    });
    state.detections = result.detections || [];
    await saveDetectionsAsFindings();
    const first = state.detections[0];
    if (first) {
      state.currentSlice = first.sliceIndex;
      el.modelStatus.textContent = `Найден объект: ${Math.round(first.confidence * 100)}%`;
      el.reportButton.classList.remove("hidden");
    } else {
      el.modelStatus.textContent = "Патологий не найдено";
      el.reportButton.classList.remove("hidden");
    }
    hydrateControls();
    render();
    renderFindings();
  } catch (error) {
    el.modelStatus.textContent = `Ошибка модели: ${error.message}`;
  }
}

async function saveDetectionsAsFindings() {
  if (!state.selectedStudyId) return;
  for (const detection of state.detections) {
    await api(`/api/studies/${state.selectedStudyId}/findings`, {
      method: "POST",
      body: {
        title: detection.title,
        diameter_mm: Math.max(1, (Number(detection.width) + Number(detection.height)) / 2),
        confidence: detection.confidence,
        source: "model"
      }
    });
  }
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
        patient_name: el.patientName.value.trim(),
        birth_date: el.birthDate.value.trim(),
        slices_count: state.slices.length,
        detections: state.detections
      }
    });
    el.reportPanel.classList.remove("hidden");
    el.reportText.value = result.report;
    if (result.study) state.selectedStudy = result.study;
    await loadStudies();
  } catch (error) {
    alert(`Не удалось создать заключение: ${error.message}`);
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
  const center = Number(slice.windowCenter ?? ((slice.pixelMin + slice.pixelMax) / 2));
  const width = Math.max(1, Number(slice.windowWidth ?? (slice.pixelMax - slice.pixelMin)));
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

  for (const canvas of [el.imageCanvas, el.overlayCanvas]) {
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
  }
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
