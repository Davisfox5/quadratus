/* ── State ─────────────────────────────────────────────────────────── */
let currentProject = null;
let markIn = null;
let markOut = null;
let filterPresets = [];
let selectedPresetId = "";
let applyingPreset = false;
let projectViewVersion = 0;
let bulkPreview = null;
let bulkPreviewGeneration = 0;
let bulkApplyPending = null;
// The control keepBulkFocusInside() parked focus on while a request was in
// flight, or null when focus was left where the user put it.
let bulkAutoFocused = null;
let projectSortDir = "";
const BULK_APPLY_STATUS = "Applying… wait for the current batch";

/* ── DOM refs ─────────────────────────────────────────────────────── */
const $projectListScreen = document.getElementById("project-list-screen");
const $taggingScreen     = document.getElementById("tagging-screen");
const $projectsContainer = document.getElementById("projects-container");
const $projectSearch       = document.getElementById("project-search");
const $projectSearchCount  = document.getElementById("project-search-count");
const $projectSearchEmpty  = document.getElementById("project-search-empty");
const $projectsEmpty       = document.getElementById("projects-empty");
const $btnProjectSearchClear = document.getElementById("btn-project-search-clear");
const $btnProjectSort = document.getElementById("btn-project-sort");
const $btnProjectSortReset = document.getElementById("btn-project-sort-reset");
const $projectSortStatus = document.getElementById("project-sort-status");
const $newProjectName    = document.getElementById("new-project-name");
const $projectTitle      = document.getElementById("project-title");

const $video        = document.getElementById("game-video");
const $noVideoMsg   = document.getElementById("no-video-msg");
const $videoUpload  = document.getElementById("video-upload");

const $btnMarkIn    = document.getElementById("btn-mark-in");
const $btnMarkOut   = document.getElementById("btn-mark-out");
const $markInDisp   = document.getElementById("mark-in-display");
const $markOutDisp  = document.getElementById("mark-out-display");
const $clipTagType  = document.getElementById("clip-tag-type");
const $clipLabel    = document.getElementById("clip-label");
const $clipNotes    = document.getElementById("clip-notes");
const $btnSaveClip  = document.getElementById("btn-save-clip");

const $timeline     = document.getElementById("timeline");
const $playhead     = document.getElementById("timeline-playhead");

const $filterType   = document.getElementById("filter-tag-type");
const $filterSearch = document.getElementById("filter-search");
const $clipsList    = document.getElementById("clips-list");
const $clipCount    = document.getElementById("clip-count-num");

const $filterPlayer = document.getElementById("filter-player");
const $playerChecks = document.getElementById("player-checkboxes");

const $presetSelect = document.getElementById("filter-preset-select");
const $presetName   = document.getElementById("filter-preset-name");
const $presetStatus = document.getElementById("preset-status");
const $btnPresetSave   = document.getElementById("btn-preset-save");
const $btnPresetRename = document.getElementById("btn-preset-rename");
const $btnPresetDelete = document.getElementById("btn-preset-delete");

const $bulkModal      = document.getElementById("bulk-modal");
const $bulkTagType    = document.getElementById("bulk-tag-type");
const $bulkPlayer     = document.getElementById("bulk-player");
const $bulkAffected   = document.getElementById("bulk-affected");
const $bulkPreviewWrap = document.getElementById("bulk-preview-wrap");
const $bulkPreviewBody = document.getElementById("bulk-preview-body");
const $bulkStatus     = document.getElementById("bulk-status");
const $btnBulkEdit    = document.getElementById("btn-bulk-edit");
const $btnBulkPreview = document.getElementById("btn-bulk-preview");
const $btnBulkConfirm = document.getElementById("btn-bulk-confirm");
const $btnBulkRefresh = document.getElementById("btn-bulk-refresh");
const $btnBulkCancel  = document.getElementById("btn-bulk-cancel");

const $tagModal     = document.getElementById("tag-modal");
const $tagTypeList  = document.getElementById("tag-type-list");

const $playerModal  = document.getElementById("player-modal");
const $playerList   = document.getElementById("player-list");

/* ── Utilities ────────────────────────────────────────────────────── */
function fmt(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...opts.headers },
    ...opts,
  });
  return res.json();
}

function getTagColor(typeName) {
  if (!currentProject) return "#555";
  const tt = currentProject.tag_types.find(t => t.name === typeName);
  return tt ? tt.color : "#555";
}

/* ── Project List ─────────────────────────────────────────────────── */
function readProjectSearchHash() {
  const hash = String((window.location && window.location.hash) || "");
  const m = hash.match(/^#q=(.*)$/);
  if (!m) return "";
  try {
    return decodeURIComponent(m[1]);
  } catch {
    return m[1];
  }
}

function applyProjectSearch() {
  const q = $projectSearch.value.trim().toLowerCase();
  const cards = $projectsContainer.querySelectorAll(".project-card");
  let shown = 0;
  cards.forEach(card => {
    const hide = q !== "" && !(card.dataset.name || "").includes(q);
    card.hidden = hide;
    if (!hide) shown++;
  });
  const total = cards.length;
  $projectSearchCount.textContent = q ? `${shown} of ${total} projects` : "";
  $projectSearchEmpty.hidden = !(q && total > 0 && shown === 0);
  $projectsEmpty.hidden = total !== 0;
}

function setTextIfChanged(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

function applyProjectSort() {
  if (projectSortDir === "") {
    setTextIfChanged($btnProjectSort, "Sort A→Z");
    setTextIfChanged($projectSortStatus, "Original order");
    return;
  }
  if (projectSortDir === "original") {
    const cards = Array.from($projectsContainer.querySelectorAll(".project-card"));
    cards.sort((a, b) => Number(a.dataset.index) - Number(b.dataset.index));
    cards.forEach(card => $projectsContainer.appendChild(card));
    $projectsContainer.dataset.sort = "original";
    setTextIfChanged($btnProjectSort, "Sort A→Z");
    setTextIfChanged($projectSortStatus, "Library order");
    return;
  }
  const cards = Array.from($projectsContainer.querySelectorAll(".project-card"));
  cards.sort((a, b) => {
    const an = a.dataset.name;
    const bn = b.dataset.name;
    if (an === bn) return Number(a.dataset.index) - Number(b.dataset.index);
    // Code-unit `<`/`>` on the already-lowercased name. Not localeCompare.
    const cmp = an < bn ? -1 : 1;
    return projectSortDir === "desc" ? -cmp : cmp;
  });
  cards.forEach(card => $projectsContainer.appendChild(card));
  $projectsContainer.dataset.sort = projectSortDir;
  setTextIfChanged($btnProjectSort, projectSortDir === "asc" ? "Sort Z→A" : "Sort A→Z");
  setTextIfChanged(
    $projectSortStatus,
    projectSortDir === "asc" ? "Sorted by name A→Z" : "Sorted by name Z→A"
  );
}

function syncProjectSearchHash() {
  const trimmed = $projectSearch.value.trim();
  const desired = trimmed ? `#q=${encodeURIComponent(trimmed)}` : "";
  if (window.history && window.history.replaceState) {
    const loc = window.location || {};
    window.history.replaceState(null, "", desired || `${loc.pathname || ""}${loc.search || ""}`);
  } else if (window.location) {
    window.location.hash = desired;
  }
}

function clearProjectSearch() {
  $projectSearch.value = "";
  applyProjectSearch();
  syncProjectSearchHash();
  $projectSearch.focus();
}

async function loadProjects() {
  const projects = await api("/api/projects");
  $projectsContainer.innerHTML = "";
  projects.forEach((p, i) => {
    const card = document.createElement("div");
    card.className = "project-card";
    card.dataset.name = String(p.name || "").toLowerCase();
    card.dataset.index = String(i);
    card.innerHTML = `
      <div>
        <div class="name">${esc(p.name)}</div>
        <div class="meta">${p.clips.length} clips &middot; ${p.video_filename ? "Video loaded" : "No video"}</div>
      </div>
      <button class="delete-btn" data-id="${p.id}" title="Delete project">&times;</button>
    `;
    card.addEventListener("click", (e) => {
      if (e.target.classList.contains("delete-btn")) return;
      openProject(p.id);
    });
    card.querySelector(".delete-btn").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (confirm(`Delete project "${p.name}"?`)) {
        await api(`/api/projects/${p.id}`, { method: "DELETE" });
        loadProjects();
      }
    });
    $projectsContainer.appendChild(card);
  });
  applyProjectSort();
  applyProjectSearch();
}

document.getElementById("btn-create-project").addEventListener("click", async () => {
  const name = $newProjectName.value.trim();
  if (!name) return;
  await api("/api/projects", { method: "POST", body: JSON.stringify({ name }) });
  $newProjectName.value = "";
  $projectSearch.value = "";
  syncProjectSearchHash();
  loadProjects();
});

$newProjectName.addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("btn-create-project").click();
});

$projectSearch.addEventListener("input", () => {
  applyProjectSearch();
  syncProjectSearchHash();
});
$projectSearch.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    e.preventDefault();
    clearProjectSearch();
  }
});
$btnProjectSearchClear.addEventListener("click", clearProjectSearch);

$btnProjectSort.addEventListener("click", () => {
  projectSortDir = projectSortDir === "asc" ? "desc" : "asc";
  applyProjectSort();
});

function resetProjectSort() {
  projectSortDir = "original";
  applyProjectSort();
  $projectSearch.focus();
}
$btnProjectSortReset.addEventListener("click", resetProjectSort);

/* ── Open / Close Project ─────────────────────────────────────────── */
async function openProject(id) {
  const version = ++projectViewVersion;
  $bulkModal.classList.remove("active");
  $taggingScreen.inert = false;
  invalidateBulkPreview();
  const project = await api(`/api/projects/${id}`);
  if (version !== projectViewVersion) return;
  currentProject = project;
  closeImportPreview();
  $projectTitle.textContent = currentProject.name;
  $projectListScreen.classList.remove("active");
  $taggingScreen.classList.add("active");

  // Video
  if (currentProject.video_filename) {
    $video.src = `/videos/${currentProject.video_filename}`;
    $video.classList.add("visible");
    $noVideoMsg.classList.add("hidden");
  } else {
    $video.src = "";
    $video.classList.remove("visible");
    $noVideoMsg.classList.remove("hidden");
  }

  markIn = null;
  markOut = null;
  $markInDisp.textContent = "--:--";
  $markOutDisp.textContent = "--:--";

  if (!currentProject.players) currentProject.players = [];

  // Never leak the previous project's filters or selected preset.
  filterPresets = [];
  selectedPresetId = "";
  applyingPreset = false;
  $filterType.value = "";
  $filterPlayer.value = "";
  $filterSearch.value = "";
  $presetName.value = "";
  $presetSelect.innerHTML = '<option value="">No saved filters</option>';
  setPresetStatus("");

  populateTagSelectors();
  populatePlayerSelectors();
  $filterType.value = "";
  $filterPlayer.value = "";
  renderClips();
  loadFilterPresets();
}

function closeProject() {
  projectViewVersion++;
  $bulkModal.classList.remove("active");
  $taggingScreen.inert = false;
  invalidateBulkPreview();
  exitAnnotationMode();
  closeImportPreview();
  currentProject = null;
  filterPresets = [];
  selectedPresetId = "";
  $filterType.value = "";
  $filterPlayer.value = "";
  $filterSearch.value = "";
  $presetName.value = "";
  $presetSelect.innerHTML = '<option value="">No saved filters</option>';
  setPresetStatus("");
  $video.pause();
  $video.src = "";
  $taggingScreen.classList.remove("active");
  $projectListScreen.classList.add("active");
  loadProjects();
}

document.getElementById("btn-back").addEventListener("click", closeProject);

/* ── Video Upload ─────────────────────────────────────────────────── */
$videoUpload.addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file || !currentProject) return;
  const fd = new FormData();
  fd.append("video", file);
  const res = await fetch(`/api/projects/${currentProject.id}/video`, {
    method: "POST",
    body: fd,
  });
  const data = await res.json();
  if (data.filename) {
    currentProject.video_filename = data.filename;
    $video.src = `/videos/${data.filename}`;
    $video.classList.add("visible");
    $noVideoMsg.classList.add("hidden");
  }
});

/* ── Mark In / Out ────────────────────────────────────────────────── */
$btnMarkIn.addEventListener("click", () => {
  markIn = $video.currentTime;
  $markInDisp.textContent = fmt(markIn);
});

$btnMarkOut.addEventListener("click", () => {
  markOut = $video.currentTime;
  $markOutDisp.textContent = fmt(markOut);
});

/* ── Save Clip ────────────────────────────────────────────────────── */
$btnSaveClip.addEventListener("click", async () => {
  if (markIn === null || markOut === null) return alert("Set both IN and OUT marks first.");
  if (markOut <= markIn) return alert("OUT mark must be after IN mark.");
  if (!$clipTagType.value) return alert("Select a tag type.");

  const clip = await api(`/api/projects/${currentProject.id}/clips`, {
    method: "POST",
    body: JSON.stringify({
      tag_type: $clipTagType.value,
      start: markIn,
      end: markOut,
      label: $clipLabel.value.trim(),
      notes: $clipNotes.value.trim(),
      players: getSelectedPlayerIds(),
    }),
  });

  if (clip.error) return alert(clip.error);

  currentProject.clips.push(clip);
  $clipLabel.value = "";
  $clipNotes.value = "";
  clearPlayerSelection();
  markIn = null;
  markOut = null;
  $markInDisp.textContent = "--:--";
  $markOutDisp.textContent = "--:--";
  renderClips();
});

function ensureSelectValue(select, value, missingLabel) {
  if (value == null) value = "";
  if (value !== "" && !Array.from(select.options).some(o => o.value === value)) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = missingLabel;
    select.appendChild(opt);
  }
  select.value = value;
}

/* ── Populate Tag Selectors ───────────────────────────────────────── */
function populateTagSelectors() {
  const prevFilter = $filterType.value;
  $clipTagType.innerHTML = '<option value="">-- Tag Type --</option>';
  $filterType.innerHTML = '<option value="">All Types</option>';
  currentProject.tag_types.forEach(tt => {
    $clipTagType.innerHTML += `<option value="${esc(tt.name)}">${esc(tt.name)}</option>`;
    $filterType.innerHTML  += `<option value="${esc(tt.name)}">${esc(tt.name)}</option>`;
  });
  ensureSelectValue($filterType, prevFilter, prevFilter ? `${prevFilter} (missing)` : "");
}

/* ── Player Selectors ─────────────────────────────────────────────── */
function populatePlayerSelectors() {
  const players = currentProject.players || [];

  // Player checkboxes for tagging
  $playerChecks.innerHTML = "";
  if (players.length === 0) {
    $playerChecks.innerHTML = '<span class="no-players-msg">No players added yet</span>';
  } else {
    players.forEach(p => {
      const chip = document.createElement("label");
      chip.className = "player-chip";
      const display = p.number ? `#${esc(p.number)} ${esc(p.name)}` : esc(p.name);
      chip.innerHTML = `<input type="checkbox" value="${p.id}"> ${display}`;
      chip.addEventListener("click", () => {
        setTimeout(() => {
          const cb = chip.querySelector("input");
          chip.classList.toggle("selected", cb.checked);
        }, 0);
      });
      $playerChecks.appendChild(chip);
    });
  }

  // Player filter dropdown — keep a stale player id explicit so an old
  // preset never silently falls back to "All Players".
  const prevPlayer = $filterPlayer.value;
  $filterPlayer.innerHTML = '<option value="">All Players</option>';
  players.forEach(p => {
    const display = p.number ? `#${p.number} ${p.name}` : p.name;
    $filterPlayer.innerHTML += `<option value="${p.id}">${esc(display)}</option>`;
  });
  ensureSelectValue($filterPlayer, prevPlayer, prevPlayer ? `Missing player ${prevPlayer}` : "");
}

function getSelectedPlayerIds() {
  const ids = [];
  $playerChecks.querySelectorAll("input[type=checkbox]:checked").forEach(cb => {
    ids.push(cb.value);
  });
  return ids;
}

function clearPlayerSelection() {
  $playerChecks.querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.checked = false;
    cb.closest(".player-chip").classList.remove("selected");
  });
}

function getPlayerDisplay(playerId) {
  const p = (currentProject.players || []).find(x => x.id === playerId);
  if (!p) return playerId;
  return p.number ? `#${p.number} ${p.name}` : p.name;
}

/* ── Render Clips ─────────────────────────────────────────────────── */
function filteredClips() {
  const typeFilter = $filterType.value;
  const playerFilter = $filterPlayer.value;
  const search = $filterSearch.value.trim().toLowerCase();
  return currentProject.clips.filter(c => {
    if (typeFilter && c.tag_type !== typeFilter) return false;
    if (playerFilter && !(c.players || []).includes(playerFilter)) return false;
    if (search && !c.label.toLowerCase().includes(search) && !c.notes.toLowerCase().includes(search) && !c.tag_type.toLowerCase().includes(search)) return false;
    return true;
  });
}

function renderClips() {
  invalidateBulkPreview();
  const clips = filteredClips();
  $clipCount.textContent = clips.length;
  $clipsList.innerHTML = "";

  // Sort by start time
  clips.sort((a, b) => a.start - b.start);

  clips.forEach(c => {
    const color = getTagColor(c.tag_type);
    const card = document.createElement("div");
    card.className = "clip-card";
    card.style.borderLeftColor = color;
    card.innerHTML = `
      <div class="clip-header">
        <span class="clip-type" style="background:${color}">${esc(c.tag_type)}</span>
        <span class="clip-times">${fmt(c.start)} - ${fmt(c.end)}</span>
      </div>
      ${c.label ? `<div class="clip-label">${esc(c.label)}</div>` : ""}
      ${c.notes ? `<div class="clip-notes">${esc(c.notes)}</div>` : ""}
      ${(c.players && c.players.length) ? `<div class="clip-players">${c.players.map(pid => `<span class="player-tag">${esc(getPlayerDisplay(pid))}</span>`).join("")}</div>` : ""}
      <div class="clip-actions">
        <button class="play-btn">Play</button>
        <button class="annotate-btn">Annotate${(c.annotations && c.annotations.length) ? ` (${c.annotations.length})` : ""}</button>
        <button class="rec-list-btn">Recordings${(c.recordings && c.recordings.length) ? ` (${c.recordings.length})` : ""}</button>
        <button class="del-btn" data-id="${c.id}">Delete</button>
      </div>
    `;

    card.querySelector(".play-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      playClip(c);
    });

    card.querySelector(".annotate-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      $video.currentTime = c.start;
      $video.pause();
      enterAnnotationMode(c);
      renderAnnList();
    });

    card.querySelector(".rec-list-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      openRecordingsModal(c);
    });

    card.querySelector(".del-btn").addEventListener("click", async (e) => {
      e.stopPropagation();
      await api(`/api/projects/${currentProject.id}/clips/${c.id}`, { method: "DELETE" });
      currentProject.clips = currentProject.clips.filter(x => x.id !== c.id);
      renderClips();
    });

    // Click card to seek to start
    card.addEventListener("click", () => {
      $video.currentTime = c.start;
    });

    $clipsList.appendChild(card);
  });

  renderTimeline();
}

function onFilterChanged() {
  if (!applyingPreset) {
    selectedPresetId = "";
    if ($presetSelect) $presetSelect.value = "";
  }
  renderClips();
}

$filterType.addEventListener("change", onFilterChanged);
$filterPlayer.addEventListener("change", onFilterChanged);
$filterSearch.addEventListener("input", onFilterChanged);

/* ── Play Clip ────────────────────────────────────────────────────── */
let clipEndHandler = null;

function playClip(clip) {
  if (!$video.src) return;
  $video.currentTime = clip.start;
  $video.play();

  // Stop at clip end
  if (clipEndHandler) $video.removeEventListener("timeupdate", clipEndHandler);
  clipEndHandler = () => {
    if ($video.currentTime >= clip.end) {
      $video.pause();
      $video.removeEventListener("timeupdate", clipEndHandler);
      clipEndHandler = null;
    }
  };
  $video.addEventListener("timeupdate", clipEndHandler);
}

/* ── Timeline ─────────────────────────────────────────────────────── */
function renderTimeline() {
  // Remove old clip bars
  $timeline.querySelectorAll(".clip-bar").forEach(el => el.remove());

  const duration = $video.duration || 1;
  const clips = filteredClips();

  clips.forEach(c => {
    const left = (c.start / duration) * 100;
    const width = ((c.end - c.start) / duration) * 100;
    const bar = document.createElement("div");
    bar.className = "clip-bar";
    bar.style.left = left + "%";
    bar.style.width = Math.max(width, 0.5) + "%";
    bar.style.background = getTagColor(c.tag_type);
    bar.innerHTML = `<div class="clip-bar-label">${esc(c.tag_type)}</div>`;
    bar.addEventListener("click", (e) => {
      e.stopPropagation();
      playClip(c);
    });
    $timeline.appendChild(bar);
  });
}

// Update playhead
$video.addEventListener("timeupdate", () => {
  if (!$video.duration) return;
  const pct = ($video.currentTime / $video.duration) * 100;
  $playhead.style.left = pct + "%";
});

// Click timeline to seek
$timeline.addEventListener("click", (e) => {
  if (!$video.duration) return;
  const rect = $timeline.getBoundingClientRect();
  const pct = (e.clientX - rect.left) / rect.width;
  $video.currentTime = pct * $video.duration;
});

// Re-render timeline when video metadata loads
$video.addEventListener("loadedmetadata", renderTimeline);

/* ── Tag Type Manager ─────────────────────────────────────────────── */
document.getElementById("btn-manage-tags").addEventListener("click", () => {
  renderTagTypeList();
  $tagModal.classList.add("active");
});

document.getElementById("btn-close-modal").addEventListener("click", () => {
  $tagModal.classList.remove("active");
});

function renderTagTypeList() {
  $tagTypeList.innerHTML = "";
  currentProject.tag_types.forEach((tt, i) => {
    const row = document.createElement("div");
    row.className = "tag-type-row";
    row.innerHTML = `
      <div class="swatch" style="background:${tt.color}"></div>
      <span class="tag-name">${esc(tt.name)}</span>
      <button class="remove-tag" data-idx="${i}">&times;</button>
    `;
    row.querySelector(".remove-tag").addEventListener("click", async () => {
      currentProject.tag_types.splice(i, 1);
      await saveTagTypes();
      renderTagTypeList();
      populateTagSelectors();
    });
    $tagTypeList.appendChild(row);
  });
}

document.getElementById("btn-add-tag-type").addEventListener("click", async () => {
  const name = document.getElementById("new-tag-name").value.trim();
  const color = document.getElementById("new-tag-color").value;
  if (!name) return;
  currentProject.tag_types.push({ name, color });
  await saveTagTypes();
  document.getElementById("new-tag-name").value = "";
  renderTagTypeList();
  populateTagSelectors();
});

async function saveTagTypes() {
  await api(`/api/projects/${currentProject.id}/tag_types`, {
    method: "PUT",
    body: JSON.stringify({ tag_types: currentProject.tag_types }),
  });
}

/* ── Player Manager ───────────────────────────────────────────────── */
document.getElementById("btn-manage-players").addEventListener("click", () => {
  renderPlayerList();
  $playerModal.classList.add("active");
});

document.getElementById("btn-close-player-modal").addEventListener("click", () => {
  $playerModal.classList.remove("active");
});

function renderPlayerList() {
  $playerList.innerHTML = "";
  (currentProject.players || []).forEach(p => {
    const row = document.createElement("div");
    row.className = "player-row";
    row.innerHTML = `
      <span class="player-number">${p.number ? esc(p.number) : "-"}</span>
      <span class="player-name">${esc(p.name)}</span>
      <button class="remove-player" data-id="${p.id}">&times;</button>
    `;
    row.querySelector(".remove-player").addEventListener("click", async () => {
      await api(`/api/projects/${currentProject.id}/players/${p.id}`, { method: "DELETE" });
      currentProject.players = currentProject.players.filter(x => x.id !== p.id);
      renderPlayerList();
      populatePlayerSelectors();
      renderClips();
    });
    $playerList.appendChild(row);
  });
}

document.getElementById("btn-add-player").addEventListener("click", async () => {
  const name = document.getElementById("new-player-name").value.trim();
  const number = document.getElementById("new-player-number").value.trim();
  if (!name) return;

  const player = await api(`/api/projects/${currentProject.id}/players`, {
    method: "POST",
    body: JSON.stringify({ name, number }),
  });

  if (player.error) return alert(player.error);

  currentProject.players.push(player);
  document.getElementById("new-player-name").value = "";
  document.getElementById("new-player-number").value = "";
  renderPlayerList();
  populatePlayerSelectors();
});

// Import roster from Excel/CSV
document.getElementById("roster-upload").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file || !currentProject) return;

  const $status = document.getElementById("import-status");
  $status.textContent = "Importing...";

  const fd = new FormData();
  fd.append("file", file);

  try {
    const res = await fetch(`/api/projects/${currentProject.id}/players/import`, {
      method: "POST",
      body: fd,
    });
    const data = await res.json();
    if (data.error) {
      $status.textContent = data.error;
    } else {
      currentProject.players.push(...data.players);
      renderPlayerList();
      populatePlayerSelectors();
      let msg = `Imported ${data.imported} player${data.imported !== 1 ? "s" : ""}`;
      if (data.skipped) msg += `, ${data.skipped} duplicate${data.skipped !== 1 ? "s" : ""} skipped`;
      $status.textContent = msg;
    }
  } catch (err) {
    $status.textContent = "Import failed";
  }

  // Reset file input so the same file can be re-uploaded
  e.target.value = "";
  setTimeout(() => { $status.textContent = ""; }, 4000);
});

/* ── Saved filter presets ─────────────────────────────────────────── */
function setPresetStatus(msg, kind) {
  $presetStatus.textContent = msg || "";
  $presetStatus.className = "preset-status" + (kind ? " " + kind : "");
}

function renderPresetSelect() {
  const keep = selectedPresetId;
  $presetSelect.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = filterPresets.length ? "Select a preset" : "No saved filters";
  $presetSelect.appendChild(placeholder);
  filterPresets.forEach(p => {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    $presetSelect.appendChild(opt);
  });
  const stillThere = filterPresets.some(p => p.id === keep);
  selectedPresetId = stillThere ? keep : "";
  $presetSelect.value = selectedPresetId;
}

async function loadFilterPresets() {
  if (!currentProject) return;
  const version = projectViewVersion;
  setPresetStatus("Loading presets…");
  try {
    const data = await api(`/api/projects/${currentProject.id}/filter_presets`);
    if (version !== projectViewVersion) return;
    if (!Array.isArray(data)) {
      filterPresets = [];
      renderPresetSelect();
      setPresetStatus(data.error || "Could not load presets", "error");
      return;
    }
    filterPresets = data;
    renderPresetSelect();
    setPresetStatus(filterPresets.length ? "" : "No saved filters yet.");
  } catch (err) {
    if (version !== projectViewVersion) return;
    filterPresets = [];
    renderPresetSelect();
    setPresetStatus("Could not load presets", "error");
  }
}

function applyPreset(preset) {
  applyingPreset = true;
  ensureSelectValue(
    $filterType,
    preset.tag_type || "",
    preset.tag_type ? `${preset.tag_type} (missing)` : ""
  );
  ensureSelectValue(
    $filterPlayer,
    preset.player || "",
    preset.player ? `Missing player ${preset.player}` : ""
  );
  $filterSearch.value = preset.search || "";
  selectedPresetId = preset.id;
  $presetSelect.value = preset.id;
  applyingPreset = false;
  renderClips();
}

function currentFilterPayload(name) {
  return {
    name,
    tag_type: $filterType.value,
    player: $filterPlayer.value,
    search: $filterSearch.value,
  };
}

async function saveCurrentPreset() {
  if (!currentProject) return;
  const version = projectViewVersion;
  setPresetStatus("Saving…");
  try {
    const data = await api(`/api/projects/${currentProject.id}/filter_presets`, {
      method: "POST",
      body: JSON.stringify(currentFilterPayload($presetName.value)),
    });
    if (version !== projectViewVersion) return;
    if (data.error) {
      setPresetStatus(data.error, "error");
      return;
    }
    filterPresets.push(data);
    selectedPresetId = data.id;
    $presetName.value = "";
    renderPresetSelect();
    setPresetStatus(`Saved “${data.name}”.`, "ok");
  } catch (err) {
    if (version !== projectViewVersion) return;
    setPresetStatus("Could not save preset", "error");
  }
}

async function renameSelectedPreset() {
  if (!currentProject) return;
  const version = projectViewVersion;
  if (!selectedPresetId) {
    setPresetStatus("Select a preset to rename.", "error");
    return;
  }
  setPresetStatus("Renaming…");
  try {
    const data = await api(
      `/api/projects/${currentProject.id}/filter_presets/${selectedPresetId}`,
      { method: "PUT", body: JSON.stringify({ name: $presetName.value }) }
    );
    if (version !== projectViewVersion) return;
    if (data.error) {
      setPresetStatus(data.error, "error");
      return;
    }
    const idx = filterPresets.findIndex(p => p.id === data.id);
    if (idx >= 0) filterPresets[idx] = data;
    $presetName.value = "";
    renderPresetSelect();
    setPresetStatus(`Renamed to “${data.name}”.`, "ok");
  } catch (err) {
    if (version !== projectViewVersion) return;
    setPresetStatus("Could not rename preset", "error");
  }
}

async function deleteSelectedPreset() {
  if (!currentProject) return;
  const version = projectViewVersion;
  if (!selectedPresetId) {
    setPresetStatus("Select a preset to delete.", "error");
    return;
  }
  const removing = selectedPresetId;
  setPresetStatus("Deleting…");
  try {
    const data = await api(
      `/api/projects/${currentProject.id}/filter_presets/${removing}`,
      { method: "DELETE" }
    );
    if (version !== projectViewVersion) return;
    if (data.error) {
      setPresetStatus(data.error, "error");
      return;
    }
    filterPresets = filterPresets.filter(p => p.id !== removing);
    selectedPresetId = "";
    renderPresetSelect();
    setPresetStatus(filterPresets.length ? "Preset deleted." : "No saved filters yet.", "ok");
  } catch (err) {
    if (version !== projectViewVersion) return;
    setPresetStatus("Could not delete preset", "error");
  }
}

$presetSelect.addEventListener("change", () => {
  const id = $presetSelect.value;
  selectedPresetId = id;
  if (!id) return;
  const preset = filterPresets.find(p => p.id === id);
  if (preset) applyPreset(preset);
});

$btnPresetSave.addEventListener("click", saveCurrentPreset);
$btnPresetRename.addEventListener("click", renameSelectedPreset);
$btnPresetDelete.addEventListener("click", deleteSelectedPreset);

$presetName.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    saveCurrentPreset();
  }
});

/* ── Bulk edit preview ────────────────────────────────────────────── */
function setBulkStatus(message, kind) {
  const text = message || "";
  const whiteSpace = text.includes("\n") ? "pre-line" : "";
  const className = "preset-status" + (kind ? ` ${kind}` : "");
  if ($bulkStatus.textContent !== text) $bulkStatus.textContent = text;
  if ($bulkStatus.style.whiteSpace !== whiteSpace) $bulkStatus.style.whiteSpace = whiteSpace;
  if ($bulkStatus.className !== className) $bulkStatus.className = className;
}

function bulkApplyBusy() {
  return Boolean(bulkApplyPending && currentProject
    && bulkApplyPending.projectId === currentProject.id);
}

function bulkFocusableControls(
  controls = $bulkModal.querySelectorAll("select, button, [tabindex]")
) {
  return Array.from(controls).filter(control => !control.disabled && !control.hidden
    && (control !== $bulkPreviewWrap || $bulkPreviewBody.children.length > 0));
}

function keepBulkFocusInside(previouslyFocused = document.activeElement) {
  if (!$bulkModal.classList.contains("active")) return;
  const active = document.activeElement;
  if ($bulkModal.contains(active) && !active.disabled && !active.hidden) {
    // Focus is somewhere usable. If it is not where we parked it, the user
    // moved, and a later settle must not move it again.
    if (active !== bulkAutoFocused) bulkAutoFocused = null;
    return;
  }

  const controls = Array.from($bulkModal.querySelectorAll("select, button, [tabindex]"));
  const focusable = bulkFocusableControls(controls);
  const focusableSet = new Set(focusable);
  const previousIndex = controls.indexOf(previouslyFocused);
  const next = previousIndex < 0 ? null : controls.slice(previousIndex + 1)
    .find(control => focusableSet.has(control));
  const fallback = focusableSet.has($btnBulkCancel) ? $btnBulkCancel : focusable[0];
  (next || fallback)?.focus();
  bulkAutoFocused = document.activeElement;
}

// When a request settles, move focus to the control that continues the flow,
// but only if focus is still where keepBulkFocusInside() parked it. Someone
// who moved elsewhere during the request keeps their place.
function settleBulkFocus(target) {
  const parked = bulkAutoFocused;
  bulkAutoFocused = null;
  if (!parked || !$bulkModal.classList.contains("active")) return;
  if (document.activeElement !== parked) return;
  if (target && $bulkModal.contains(target) && !target.disabled && !target.hidden) target.focus();
}

function invalidateBulkPreview() {
  const previouslyFocused = document.activeElement;
  bulkPreview = null;
  bulkPreviewGeneration++;
  const applyBusy = bulkApplyBusy();
  setBulkStatus(applyBusy ? BULK_APPLY_STATUS : "");
  $btnBulkPreview.disabled = applyBusy;
  $btnBulkConfirm.disabled = true;
  $btnBulkRefresh.hidden = true;
  $bulkPreviewBody.innerHTML = "";
  $bulkAffected.textContent = currentProject ? filteredClips().length : 0;
  keepBulkFocusInside(previouslyFocused);
}

function addBulkOption(select, value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.innerHTML = esc(label);
  select.appendChild(option);
}

function populateBulkSelects(tagType = "", playerId = "") {
  $bulkTagType.innerHTML = '<option value="">Keep tag type</option>';
  (currentProject.tag_types || []).forEach(type => addBulkOption($bulkTagType, type.name, type.name));
  $bulkPlayer.innerHTML = '<option value="">Keep players</option><option value="__clear__">Clear players</option>';
  (currentProject.players || []).forEach(player => addBulkOption(
    $bulkPlayer, player.id, player.number ? `#${player.number} ${player.name}` : player.name
  ));
  if (Array.from($bulkTagType.options).some(option => option.value === tagType)) {
    $bulkTagType.value = tagType;
  }
  if (Array.from($bulkPlayer.options).some(option => option.value === playerId)) {
    $bulkPlayer.value = playerId;
  }
}

function openBulkModal() {
  if (!currentProject) return;
  bulkAutoFocused = null;
  invalidateBulkPreview();
  populateBulkSelects();
  $bulkTagType.value = "";
  $bulkPlayer.value = "";
  const count = filteredClips().length;
  $bulkAffected.textContent = count;
  if (bulkApplyBusy()) setBulkStatus(BULK_APPLY_STATUS);
  else setBulkStatus(count ? "" : "No clips match the current filter");
  $bulkModal.classList.add("active");
  $taggingScreen.inert = true;
  $bulkTagType.focus();
}

function closeBulkModal() {
  $bulkModal.classList.remove("active");
  $taggingScreen.inert = false;
  bulkAutoFocused = null;
  invalidateBulkPreview();
  $btnBulkEdit.focus();
}

function formatBulkValues(values) {
  const players = (values.players || []).map(playerId => esc(getPlayerDisplay(playerId))).join(", ") || "no player";
  return `${esc(values.tag_type)} &middot; ${players}`;
}

async function previewBulkEdit() {
  if (!currentProject) return;
  if (bulkApplyBusy()) {
    const previouslyFocused = document.activeElement;
    $btnBulkPreview.disabled = true;
    keepBulkFocusInside(previouslyFocused);
    setBulkStatus(BULK_APPLY_STATUS);
    return;
  }
  invalidateBulkPreview();
  const clipIds = filteredClips().map(clip => clip.id);
  $bulkAffected.textContent = clipIds.length;
  if (!clipIds.length) {
    setBulkStatus("No clips match the current filter");
    return;
  }
  const changes = {};
  if ($bulkTagType.value) changes.tag_type = $bulkTagType.value;
  if ($bulkPlayer.value === "__clear__") changes.players = [];
  else if ($bulkPlayer.value) changes.players = [$bulkPlayer.value];
  if (!Object.keys(changes).length) {
    setBulkStatus("Choose a new tag type or player", "error");
    return;
  }
  const generation = bulkPreviewGeneration;
  const projectId = currentProject.id;
  setBulkStatus("Previewing…");
  const previouslyFocused = document.activeElement;
  $btnBulkPreview.disabled = true;
  keepBulkFocusInside(previouslyFocused);
  let focusTarget = $btnBulkPreview;
  try {
    const res = await fetch(`/api/projects/${projectId}/clips/bulk_preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clip_ids: clipIds, changes }),
    });
    if (generation !== bulkPreviewGeneration || !currentProject || currentProject.id !== projectId) return;
    const data = await res.json();
    if (generation !== bulkPreviewGeneration || !currentProject || currentProject.id !== projectId) return;
    if (res.status !== 200) {
      setBulkStatus(data.error || "Request failed", "error");
      return;
    }
    if (!Number.isInteger(data.count) || data.count < 0 || !Array.isArray(data.clips))
      return setBulkStatus("Request failed", "error");
    if (data.count === 0) {
      setBulkStatus("All selected clips already have these values");
      return;
    }
    bulkPreview = { previewId: data.preview_id, projectId: data.project_id,
      count: data.count, clipIds: data.clips.map(clip => clip.id) };
    $bulkAffected.textContent = data.count;
    $bulkPreviewBody.innerHTML = data.clips.map(clip => `
      <tr>
        <td>${fmt(clip.start)}–${fmt(clip.end)} ${esc(clip.label || "")}</td>
        <td>${formatBulkValues(clip.before)}</td>
        <td>${formatBulkValues(clip.after)}</td>
      </tr>`).join("");
    $btnBulkConfirm.disabled = false;
    setBulkStatus(`${data.count} clip(s) will change`);
    focusTarget = $btnBulkConfirm;
  } catch (err) {
    if (generation === bulkPreviewGeneration && currentProject && currentProject.id === projectId) {
      setBulkStatus("Request failed", "error");
    }
  } finally {
    if (generation === bulkPreviewGeneration && currentProject && currentProject.id === projectId
        && !bulkApplyBusy()) {
      $btnBulkPreview.disabled = false;
      settleBulkFocus(focusTarget);
    }
  }
}

function showBulkRecovery(message) {
  const previouslyFocused = document.activeElement;
  bulkPreview = null;
  $bulkPreviewBody.innerHTML = "";
  $btnBulkConfirm.disabled = true;
  $btnBulkRefresh.hidden = false;
  setBulkStatus(message, "error");
  keepBulkFocusInside(previouslyFocused);
  settleBulkFocus($btnBulkRefresh);
}

async function applyBulkEdit() {
  if (bulkApplyBusy()) {
    const previouslyFocused = document.activeElement;
    $btnBulkPreview.disabled = true;
    $btnBulkConfirm.disabled = true;
    keepBulkFocusInside(previouslyFocused);
    setBulkStatus(BULK_APPLY_STATUS);
    return;
  }
  if (!bulkPreview || !currentProject || bulkPreview.projectId !== currentProject.id) return;
  // Keep the single pending marker from being overwritten after a project
  // switch. Other projects can still preview while this request is pending.
  if (bulkApplyPending) {
    setBulkStatus(BULK_APPLY_STATUS);
    return;
  }
  const generation = bulkPreviewGeneration;
  const projectId = bulkPreview.projectId;
  const previewId = bulkPreview.previewId;
  const tagType = $bulkTagType.value;
  const player = $bulkPlayer.value;
  setBulkStatus(BULK_APPLY_STATUS);
  const previouslyFocused = document.activeElement;
  $btnBulkConfirm.disabled = true;
  $btnBulkPreview.disabled = true;
  keepBulkFocusInside(previouslyFocused);
  bulkApplyPending = { projectId };
  try {
    const res = await fetch(`/api/projects/${projectId}/clips/bulk_apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preview_id: previewId }),
    });
    const data = await res.json();
    if (!currentProject || currentProject.id !== projectId) return;
    const stillCurrent = generation === bulkPreviewGeneration;
    const modalOpen = $bulkModal.classList.contains("active");
    if (res.status === 200 && Array.isArray(data.clip_ids)) {
      const updated = Number.isInteger(data.updated) && data.updated >= 0
        ? data.updated : data.clip_ids.length;
      data.clip_ids.forEach(id => {
        const clip = currentProject.clips.find(candidate => candidate.id === id);
        if (!clip) return;
        if (tagType) clip.tag_type = tagType;
        if (player === "__clear__") clip.players = [];
        else if (player) clip.players = [player];
      });
      // A committed success is authoritative for this project. Rendering also
      // invalidates any newer preview before its confirmation can be reused.
      renderClips();
      renderTimeline();
      if (modalOpen) {
        if (stillCurrent) {
          $bulkTagType.value = "";
          $bulkPlayer.value = "";
        }
        setBulkStatus(stillCurrent
          ? `${updated} clip(s) updated`
          : `${updated} clip(s) updated — preview again`, "ok");
        $btnBulkConfirm.disabled = true;
        $btnBulkRefresh.hidden = true;
        $btnBulkPreview.disabled = false;
        settleBulkFocus($btnBulkPreview);
      }
      return;
    }
    if (!stillCurrent) return;
    if (res.status === 409 && data.code === "stale") {
      const conflicts = Array.isArray(data.conflicts) ? data.conflicts : [];
      const summaries = conflicts.slice(0, 5).map(conflict => {
        if (conflict.reason === "deleted" || !conflict.current) return `${conflict.id}: deleted`;
        const current = conflict.current;
        const label = current.label || conflict.id;
        const players = (current.players || []).map(getPlayerDisplay).join(", ") || "no player";
        return `${label}: modified (now ${current.tag_type} · ${players})`;
      });
      if (conflicts.length > summaries.length) {
        summaries.push(`+${conflicts.length - summaries.length} more conflict(s)`);
      }
      showBulkRecovery([data.error || "Clips changed after preview", ...summaries].join("\n"));
      return;
    }
    showBulkRecovery(data.error || "Request failed");
  } catch (err) {
    if (generation !== bulkPreviewGeneration || !currentProject || currentProject.id !== projectId) return;
    showBulkRecovery("Request failed");
  } finally {
    if (bulkApplyPending && bulkApplyPending.projectId === projectId) {
      bulkApplyPending = null;
    }
    if (currentProject && currentProject.id === projectId
        && $bulkModal.classList.contains("active")) {
      $btnBulkPreview.disabled = false;
    } else if (currentProject && $bulkModal.classList.contains("active")
        && $bulkStatus.textContent === BULK_APPLY_STATUS) {
      // Another project's dialog was told to wait for this batch; tell it the
      // wait is over and restore its own preview state.
      setBulkStatus(bulkPreview && bulkPreview.projectId === currentProject.id
        ? `${bulkPreview.count} clip(s) will change` : "");
      $btnBulkPreview.disabled = false;
    }
  }
}

async function refreshBulkProject() {
  if (!currentProject) return;
  const version = projectViewVersion;
  const generation = bulkPreviewGeneration;
  const projectId = currentProject.id;
  const bulkTagType = $bulkTagType.value;
  const bulkPlayer = $bulkPlayer.value;
  const clipTagType = $clipTagType.value;
  const clipPlayers = new Set(getSelectedPlayerIds());
  const annotationClipId = annClip ? annClip.id : null;
  setBulkStatus("Refreshing…");
  const previouslyFocused = document.activeElement;
  $btnBulkPreview.disabled = true;
  $btnBulkRefresh.hidden = true;
  keepBulkFocusInside(previouslyFocused);
  try {
    const project = await api(`/api/projects/${projectId}`);
    if (generation !== bulkPreviewGeneration || version !== projectViewVersion
        || !currentProject || currentProject.id !== projectId) return;
    if (!project || project.error || !Array.isArray(project.clips)) throw new Error("Refresh failed");
    currentProject.clips = project.clips;
    currentProject.players = project.players || [];
    currentProject.tag_types = project.tag_types;
    populateTagSelectors();
    populatePlayerSelectors();
    if (Array.from($clipTagType.options).some(option => option.value === clipTagType)) {
      $clipTagType.value = clipTagType;
    }
    $playerChecks.querySelectorAll('input[type="checkbox"]').forEach(checkbox => {
      checkbox.checked = clipPlayers.has(checkbox.value);
      checkbox.closest(".player-chip").classList.toggle("selected", checkbox.checked);
    });
    if (annotationClipId) {
      annClip = currentProject.clips.find(clip => clip.id === annotationClipId) || null;
      if (!annClip) exitAnnotationMode();
    }
    renderClips();
    renderTimeline();
    populateBulkSelects(bulkTagType, bulkPlayer);
    $bulkAffected.textContent = filteredClips().length;
    $btnBulkRefresh.hidden = true;
    setBulkStatus("Refreshed — preview again");
    $btnBulkPreview.disabled = bulkApplyBusy();
    settleBulkFocus($btnBulkPreview);
  } catch (err) {
    if (generation !== bulkPreviewGeneration || version !== projectViewVersion
        || !currentProject || currentProject.id !== projectId) return;
    setBulkStatus("Refresh failed", "error");
    $btnBulkRefresh.hidden = false;
    settleBulkFocus($btnBulkRefresh);
  } finally {
    if (generation === bulkPreviewGeneration && version === projectViewVersion
        && currentProject && currentProject.id === projectId && !bulkApplyBusy()) {
      $btnBulkPreview.disabled = false;
    }
  }
}

$btnBulkEdit.addEventListener("click", openBulkModal);
$btnBulkCancel.addEventListener("click", closeBulkModal);
$btnBulkPreview.addEventListener("click", previewBulkEdit);
$btnBulkConfirm.addEventListener("click", applyBulkEdit);
$btnBulkRefresh.addEventListener("click", refreshBulkProject);
$bulkTagType.addEventListener("change", invalidateBulkPreview);
$bulkPlayer.addEventListener("change", invalidateBulkPreview);
// A pointer or programmatic focus change the app did not make ends automatic
// focus ownership; keepBulkFocusInside() re-asserts it after its own focus().
$bulkModal.addEventListener("focusin", (e) => {
  if (e.target !== bulkAutoFocused) bulkAutoFocused = null;
});

/* ── Export ────────────────────────────────────────────────────────── */
function buildFilterParams() {
  const params = new URLSearchParams();
  if ($filterType.value) params.set("tag_type", $filterType.value);
  if ($filterPlayer.value) params.set("player", $filterPlayer.value);
  if ($filterSearch.value.trim()) params.set("search", $filterSearch.value.trim());
  return params.toString();
}

document.getElementById("btn-export-csv").addEventListener("click", () => {
  if (!currentProject) return;
  const qs = buildFilterParams();
  const url = `/api/projects/${currentProject.id}/export/csv${qs ? "?" + qs : ""}`;
  window.location.href = url;
});

document.getElementById("btn-export-json").addEventListener("click", () => {
  if (!currentProject) return;
  const qs = buildFilterParams();
  const url = `/api/projects/${currentProject.id}/export/json${qs ? "?" + qs : ""}`;
  window.location.href = url;
});

document.getElementById("btn-export-video").addEventListener("click", async () => {
  if (!currentProject) return;
  const btn = document.getElementById("btn-export-video");
  btn.classList.add("exporting");
  btn.textContent = "Exporting...";
  btn.disabled = true;

  try {
    const body = {};
    if ($filterType.value) body.tag_type = $filterType.value;
    if ($filterPlayer.value) body.player = $filterPlayer.value;
    if ($filterSearch.value.trim()) body.search = $filterSearch.value.trim();

    const res = await fetch(`/api/projects/${currentProject.id}/export/video`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const err = await res.json();
      alert(err.error || "Export failed");
      return;
    }

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${currentProject.name.replace(/ /g, "_")}_playlist.mp4`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert("Export failed: " + e.message);
  } finally {
    btn.classList.remove("exporting");
    btn.textContent = "Video";
    btn.disabled = false;
  }
});

let importPreviewGeneration = 0;

async function runImportPreview(file) {
  if (!file || !currentProject) return;
  const generation = ++importPreviewGeneration;
  const projectId = currentProject.id;
  const section = document.getElementById("import-preview");
  const status = document.getElementById("import-preview-status");
  section.hidden = false;
  document.getElementById("import-preview-summary").textContent = "";
  document.getElementById("import-preview-table").querySelector("tbody").innerHTML = "";
  status.classList.remove("error");
  status.textContent = "Checking…";

  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await fetch(`/api/projects/${projectId}/clips/import_preview`, {
      method: "POST",
      body: fd,
    });
    if (generation !== importPreviewGeneration || !currentProject || currentProject.id !== projectId) return;
    const body = await res.json();
    if (generation !== importPreviewGeneration || !currentProject || currentProject.id !== projectId) return;
    if (!res.ok) {
      status.classList.add("error");
      status.textContent = body.error || "Preview failed";
      return;
    }
    renderImportPreview(body);
  } catch (err) {
    if (generation === importPreviewGeneration && currentProject && currentProject.id === projectId) {
      status.classList.add("error");
      status.textContent = "Preview failed";
    }
  }
}

function renderImportPreview(data) {
  const summary = data.summary;
  document.getElementById("import-preview-summary").textContent =
    `Total ${summary.total} · valid ${summary.valid} · malformed ${summary.malformed} · duplicate ${summary.duplicate}`;
  const status = document.getElementById("import-preview-status");
  status.classList.remove("error");
  status.textContent = summary.total === 0 ? "No rows found" : "";
  document.getElementById("import-preview-table").querySelector("tbody").innerHTML = data.rows.map(row => {
    const clip = row.clip || {};
    const players = (clip.players || []).map(playerId => getPlayerDisplay(playerId)).join(", ");
    return `<tr data-status="${esc(row.status ?? "")}">
      <td>${esc(row.line ?? "")}</td>
      <td>${esc(row.status ?? "")}</td>
      <td>${esc(clip.tag_type ?? "")}</td>
      <td>${esc(clip.start ?? "")}</td>
      <td>${esc(clip.end ?? "")}</td>
      <td>${esc(clip.label ?? "")}</td>
      <td>${esc(players)}</td>
      <td>${esc((row.reasons || []).join("; "))}</td>
    </tr>`;
  }).join("");
}

function closeImportPreview() {
  importPreviewGeneration++;
  document.getElementById("import-preview").hidden = true;
  document.getElementById("import-preview-summary").textContent = "";
  const status = document.getElementById("import-preview-status");
  status.classList.remove("error");
  status.textContent = "";
  document.getElementById("import-preview-table").querySelector("tbody").innerHTML = "";
  document.getElementById("import-preview-file").value = "";
  document.getElementById("btn-import-preview").focus();
}

document.getElementById("btn-import-preview").addEventListener("click", () => {
  document.getElementById("import-preview-file").click();
});

document.getElementById("import-preview-file").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!file || !currentProject) return;
  runImportPreview(file);
});

document.getElementById("btn-import-preview-close").addEventListener("click", closeImportPreview);
document.getElementById("import-preview").addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeImportPreview();
});

/* ── Annotations: Toolbar Wiring ──────────────────────────────────── */

// Tool selection
document.querySelectorAll(".ann-tool-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".ann-tool-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    annTool = btn.dataset.tool;
  });
});

// Color & width
document.getElementById("ann-color").addEventListener("input", (e) => { annColor = e.target.value; });
document.getElementById("ann-width").addEventListener("change", (e) => { annLineWidth = parseInt(e.target.value); });

// Freeze frame
document.getElementById("btn-ann-freeze").addEventListener("click", toggleFreezeFrame);
document.getElementById("ann-freeze-dur").addEventListener("change", (e) => {
  annFreezeDuration = parseFloat(e.target.value) || 2;
});

// Clear all
document.getElementById("btn-ann-clear").addEventListener("click", () => {
  if (annList.length === 0) return;
  if (confirm("Remove all annotations from this clip?")) {
    clearAllAnnotations();
  }
});

// Done — auto-stop recording if active
document.getElementById("btn-ann-done").addEventListener("click", async () => {
  if (isRecording()) {
    const blob = await stopRecording();
    if (blob && annClip) {
      document.getElementById("rec-status").textContent = "Saving...";
      await uploadRecording(annClip.id, blob);
      document.getElementById("rec-status").textContent = "";
    }
    resetRecordingUI();
  }
  exitAnnotationMode();
  renderClips();
});

// Canvas mouse events
const $annCanvas = document.getElementById("annotation-canvas");
$annCanvas.addEventListener("mousedown", onAnnMouseDown);
$annCanvas.addEventListener("mousemove", onAnnMouseMove);
$annCanvas.addEventListener("mouseup", onAnnMouseUp);

// Render annotations on video timeupdate
$video.addEventListener("timeupdate", tickAnnotations);

// Also render on play clip (with annotations visible)
const _origPlayClip = playClip;
playClip = function(clip) {
  // If we're playing a clip that has annotations, load them for display
  if (clip.annotations && clip.annotations.length > 0 && !annActive) {
    annClip = clip;
    annList = clip.annotations.map(a => ({...a, data: {...a.data}}));
  }
  _origPlayClip(clip);
};

/* ── Video Editor ─────────────────────────────────────────────────── */
const $editorModal = document.getElementById("editor-modal");

document.getElementById("btn-edit-video").addEventListener("click", () => {
  if (!currentProject || !currentProject.video_filename) {
    return alert("No video loaded in this project.");
  }
  // Pre-fill end with video duration if available
  if ($video.duration) {
    document.getElementById("trim-end").value = Math.floor($video.duration * 10) / 10;
  }
  document.getElementById("editor-status").textContent = "";
  $editorModal.classList.add("active");
});

document.getElementById("btn-close-editor").addEventListener("click", () => {
  $editorModal.classList.remove("active");
});

// Tab switching
document.querySelectorAll(".editor-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".editor-tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".editor-pane").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(`editor-tab-${tab.dataset.tab}`).classList.add("active");
  });
});

// "Use current" buttons
document.querySelectorAll(".editor-use-current").forEach(btn => {
  btn.addEventListener("click", () => {
    const target = document.getElementById(btn.dataset.target);
    if (target && $video.currentTime !== undefined) {
      target.value = Math.round($video.currentTime * 10) / 10;
    }
  });
});

async function doEditorAction(url, body, actionBtn) {
  const $status = document.getElementById("editor-status");
  actionBtn.disabled = true;
  $status.textContent = "Processing...";

  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.error) {
      $status.textContent = data.error;
    } else {
      $status.textContent = data.message || "Done!";
      // Reload project to get updated clips and video
      await openProject(currentProject.id);
    }
  } catch (e) {
    $status.textContent = "Operation failed: " + e.message;
  } finally {
    actionBtn.disabled = false;
  }
}

document.getElementById("btn-do-trim").addEventListener("click", function() {
  const start = parseFloat(document.getElementById("trim-start").value);
  const end = parseFloat(document.getElementById("trim-end").value);
  if (isNaN(start) || isNaN(end) || end <= start) {
    return alert("Enter valid start and end times.");
  }
  if (!confirm(`This will permanently trim the video to ${start}s - ${end}s. Clips outside this range will be removed. Continue?`)) return;
  doEditorAction(`/api/projects/${currentProject.id}/video/trim`, { start, end }, this);
});

document.getElementById("btn-do-split").addEventListener("click", function() {
  const splitAt = parseFloat(document.getElementById("split-at").value);
  if (isNaN(splitAt) || splitAt <= 0) {
    return alert("Enter a valid split point.");
  }
  if (!confirm(`This will split the video at ${splitAt}s into two separate projects. Continue?`)) return;
  doEditorAction(`/api/projects/${currentProject.id}/video/split`, { split_at: splitAt }, this);
});

document.getElementById("btn-do-cut").addEventListener("click", function() {
  const cutStart = parseFloat(document.getElementById("cut-start").value);
  const cutEnd = parseFloat(document.getElementById("cut-end").value);
  if (isNaN(cutStart) || isNaN(cutEnd) || cutEnd <= cutStart) {
    return alert("Enter valid cut start and end times.");
  }
  if (!confirm(`This will permanently remove ${cutStart}s - ${cutEnd}s from the video. Continue?`)) return;
  doEditorAction(`/api/projects/${currentProject.id}/video/cut`, { cut_start: cutStart, cut_end: cutEnd }, this);
});

/* ── Recording: Wiring ────────────────────────────────────────────── */

// Hide record button if not supported
if (!isRecordingSupported()) {
  document.getElementById("rec-controls").innerHTML =
    '<span class="rec-not-supported">Recording not supported in this browser</span>';
}

document.getElementById("btn-rec-start").addEventListener("click", async () => {
  if (!annClip) return alert("Enter annotation mode on a clip first.");
  try {
    await startRecording(annClip.id);
    document.getElementById("btn-rec-start").style.display = "none";
    document.getElementById("btn-rec-stop").style.display = "";
    document.getElementById("rec-timer").style.display = "";
    document.getElementById("rec-status").textContent = "Recording...";
  } catch (e) {
    alert("Could not start recording: " + e.message);
  }
});

document.getElementById("btn-rec-stop").addEventListener("click", async () => {
  const blob = await stopRecording();
  document.getElementById("rec-status").textContent = "Saving...";
  if (blob && annClip) {
    await uploadRecording(annClip.id, blob);
  }
  resetRecordingUI();
  document.getElementById("rec-status").textContent = "Saved!";
  setTimeout(() => { document.getElementById("rec-status").textContent = ""; }, 2000);
});

function resetRecordingUI() {
  document.getElementById("btn-rec-start").style.display = "";
  document.getElementById("btn-rec-stop").style.display = "none";
  document.getElementById("rec-timer").style.display = "none";
}

async function uploadRecording(clipId, blob) {
  const fd = new FormData();
  fd.append("recording", blob, `recording_${clipId}_${Date.now()}.webm`);
  fd.append("duration", String(getRecordingDuration()));
  const res = await fetch(
    `/api/projects/${currentProject.id}/clips/${clipId}/recordings`,
    { method: "POST", body: fd }
  );
  const data = await res.json();
  if (!data.error) {
    // Update local clip data
    const clip = currentProject.clips.find(c => c.id === clipId);
    if (clip) {
      if (!clip.recordings) clip.recordings = [];
      clip.recordings.push(data);
    }
  }
  return data;
}

/* ── Recordings Modal ─────────────────────────────────────────────── */
let recModalClip = null;

function openRecordingsModal(clip) {
  recModalClip = clip;
  renderRecordingsList();
  document.getElementById("rec-modal").classList.add("active");
}

document.getElementById("btn-close-rec-modal").addEventListener("click", () => {
  document.getElementById("rec-modal").classList.remove("active");
  recModalClip = null;
  renderClips(); // refresh counts
});

function renderRecordingsList() {
  const container = document.getElementById("rec-list");
  const recs = recModalClip ? (recModalClip.recordings || []) : [];
  container.innerHTML = "";

  if (recs.length === 0) {
    container.innerHTML = '<p class="rec-empty">No recordings yet. Use Annotate to record an analysis session.</p>';
    return;
  }

  recs.forEach((r, i) => {
    const item = document.createElement("div");
    item.className = "rec-item";
    const durDisplay = r.duration ? `${Math.floor(r.duration / 60)}:${String(r.duration % 60).padStart(2, "0")}` : "";
    item.innerHTML = `
      <div class="rec-item-header">
        <span class="rec-label">Recording ${i + 1}</span>
        ${durDisplay ? `<span class="rec-dur">${durDisplay}</span>` : ""}
      </div>
      <video controls preload="metadata" src="/recordings/${esc(r.filename)}"></video>
      <div class="rec-actions">
        <a href="/recordings/${esc(r.filename)}" download="${esc(r.filename)}">Download</a>
        <button class="rec-del" data-id="${r.id}">Delete</button>
      </div>
    `;
    item.querySelector(".rec-del").addEventListener("click", async () => {
      await api(`/api/projects/${currentProject.id}/clips/${recModalClip.id}/recordings/${r.id}`, {
        method: "DELETE",
      });
      recModalClip.recordings = recModalClip.recordings.filter(x => x.id !== r.id);
      renderRecordingsList();
    });
    container.appendChild(item);
  });
}

/* ── Keyboard Shortcuts ───────────────────────────────────────────── */
document.addEventListener("keydown", (e) => {
  if ($bulkModal.classList.contains("active")) {
    if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      const controls = bulkFocusableControls();
      if (controls.length) {
        const currentIndex = controls.indexOf(document.activeElement);
        const nextIndex = currentIndex < 0
          ? (e.shiftKey ? controls.length - 1 : 0)
          : (currentIndex + (e.shiftKey ? -1 : 1) + controls.length) % controls.length;
        controls[nextIndex].focus();
      }
      // The user chose where focus is now, even if that is the parked control.
      bulkAutoFocused = null;
      e.preventDefault();
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      closeBulkModal();
    }
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  // Only on tagging screen, not in inputs
  if (!currentProject) return;
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT" || e.target.tagName === "TEXTAREA") return;

  switch (e.key.toLowerCase()) {
    case "i":
      $btnMarkIn.click();
      break;
    case "o":
      $btnMarkOut.click();
      break;
    case "p":
      e.preventDefault();
      $presetSelect.focus();
      break;
    case "b":
      if (!document.querySelector(".modal-overlay.active")) {
        e.preventDefault();
        openBulkModal();
      }
      break;
    case "m":
      if (!document.querySelector(".modal-overlay.active")) {
        e.preventDefault();
        document.getElementById("btn-import-preview").click();
      }
      break;
    case " ":
      e.preventDefault();
      $video.paused ? $video.play() : $video.pause();
      break;
  }
});

/* ── Escape HTML ──────────────────────────────────────────────────── */
function esc(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

/* ── Init ─────────────────────────────────────────────────────────── */
initAnnotations($video);
initRecording($video, document.getElementById("annotation-canvas"));
$projectSearch.value = readProjectSearchHash();
loadProjects();
