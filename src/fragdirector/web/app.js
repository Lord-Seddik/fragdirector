// The editor page: the moments panel (T10), demo info, saving and the start-up.
// The editable timeline (T11) is in timeline.js, which is loaded after this
// file and uses its helpers.
//
// The server keeps the shot list, so a reload shows the same timeline. The
// ticked moments and the filters are kept in this browser's localStorage.
// All text is put on the page with textContent, never as HTML.
"use strict";

const PRESET_INFO = {
  killer: "The killer's view only.",
  victims: "Each victim's view of the seconds before their death.",
  "major-replay": "Killer in real time, then each victim's view, last kill replayed in slow motion.",
  budget: "Fit the shots into a total length; lowest scores are dropped first.",
};
const ALL = "";

const state = {
  demo: null,
  moments: [],
  ticked: new Set(), // moment keys (see momentKey)
  filters: { player: ALL, round: ALL, kind: ALL, weapon: ALL, tag: ALL },
  shotlist: null,
  draftInfo: null, // notes from the last preset run
};

// ---------------------------------------------------------------- helpers

const $ = (id) => document.getElementById(id);

function el(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

async function api(method, url, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    throw new Error("Cannot reach the editor server. Is fragdirector still running?");
  }
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    // not JSON; fall through to the status text
  }
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function showMessage(text, isError) {
  const box = $("message");
  box.hidden = !text;
  box.textContent = text || "";
  box.className = isError ? "message error" : "message ok";
}

function formatClock(seconds) {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function formatSeconds(seconds) {
  return `${seconds.toFixed(1)} s`;
}

// A moment's id only lasts while the server runs, so ticks are remembered by
// what the moment is instead.
function momentKey(m) {
  return `${m.kind}|${m.round}|${m.player}|${m.start_tick}`;
}

function storageKey() {
  return `fragdirector:${state.demo.path}`;
}

function saveLocal() {
  try {
    localStorage.setItem(
      storageKey(),
      JSON.stringify({ ticked: [...state.ticked], filters: state.filters }),
    );
  } catch (error) {
    // storage may be blocked; the editor still works without it
  }
}

function loadLocal() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey()) || "null");
    if (!saved) return;
    const known = new Set(state.moments.map(momentKey));
    state.ticked = new Set((saved.ticked || []).filter((key) => known.has(key)));
    Object.assign(state.filters, saved.filters || {});
  } catch (error) {
    // ignore unreadable saved state
  }
}

function playerName(id) {
  const player = state.demo.players.find((p) => p.id === id);
  return player ? player.name : id;
}

// ---------------------------------------------------------------- filters

function momentWeapons(m) {
  return new Set(m.kills.map((k) => k.weapon).filter(Boolean));
}

function passes(m) {
  const f = state.filters;
  if (f.player !== ALL && m.player !== f.player) return false;
  if (f.round !== ALL && String(m.round) !== f.round) return false;
  if (f.kind !== ALL && m.kind !== f.kind) return false;
  if (f.weapon !== ALL && !momentWeapons(m).has(f.weapon)) return false;
  if (f.tag !== ALL && !m.tags.includes(f.tag)) return false;
  return true;
}

function fillSelect(select, allLabel, options, current) {
  select.replaceChildren(el("option", null, allLabel));
  select.firstChild.value = ALL;
  for (const [value, label] of options) {
    const option = el("option", null, label);
    option.value = value;
    select.append(option);
  }
  const valid = options.some(([value]) => value === current);
  select.value = valid ? current : ALL;
}

function buildFilterOptions() {
  const f = state.filters;
  const players = new Map();
  const rounds = new Set();
  const kinds = new Set();
  const weapons = new Set();
  const tags = new Set();
  for (const m of state.moments) {
    players.set(m.player, m.player_name);
    rounds.add(m.round);
    kinds.add(m.kind);
    momentWeapons(m).forEach((w) => weapons.add(w));
    m.tags.forEach((t) => tags.add(t));
  }
  const sorted = (set) => [...set].sort((a, b) => String(a).localeCompare(String(b)));
  fillSelect($("f-player"), "All players",
    [...players].sort((a, b) => a[1].localeCompare(b[1])), f.player);
  fillSelect($("f-round"), "All rounds",
    [...rounds].sort((a, b) => a - b).map((r) => [String(r), `Round ${r}`]), f.round);
  fillSelect($("f-kind"), "All kinds", sorted(kinds).map((k) => [k, k]), f.kind);
  fillSelect($("f-weapon"), "All weapons", sorted(weapons).map((w) => [w, w]), f.weapon);
  fillSelect($("f-tag"), "All tags", sorted(tags).map((t) => [t, t]), f.tag);
  // a saved filter that no longer exists was reset by fillSelect
  f.player = $("f-player").value;
  f.round = $("f-round").value;
  f.kind = $("f-kind").value;
  f.weapon = $("f-weapon").value;
  f.tag = $("f-tag").value;
}

function onFilterChange() {
  state.filters = {
    player: $("f-player").value,
    round: $("f-round").value,
    kind: $("f-kind").value,
    weapon: $("f-weapon").value,
    tag: $("f-tag").value,
  };
  saveLocal();
  renderMoments();
}

// ---------------------------------------------------------------- moments

function momentRow(m) {
  const key = momentKey(m);
  const box = el("input");
  box.type = "checkbox";
  box.checked = state.ticked.has(key);
  box.setAttribute("aria-label", `Include ${m.title}`);
  box.addEventListener("change", () => {
    if (box.checked) state.ticked.add(key);
    else state.ticked.delete(key);
    saveLocal();
    row.classList.toggle("ticked", box.checked);
    renderTickCount();
  });

  const when = `Round ${m.round}` +
    (m.clock_seconds === null ? "" : `, ${formatClock(m.clock_seconds)}`);
  const tags = el("span", "tags", ...m.tags.map((t) => el("span", "tag", t)));
  const body = el("span", "moment-body",
    el("span", "moment-title", m.title),
    el("span", "muted small", `${when} - ${m.player_name}`),
    tags);
  const row = el("li", "moment" + (box.checked ? " ticked" : ""),
    el("label", "moment-label", box, body, el("span", "score", m.score.toFixed(0))));
  return row;
}

function renderMoments() {
  const shown = state.moments.filter(passes);
  const list = $("moments");
  list.replaceChildren(...shown.map(momentRow));
  if (shown.length === 0) {
    list.append(el("li", "muted empty",
      state.moments.length === 0 ? "No moments were found in this demo." : "No moment matches the filters."));
  }
  $("moments-count").textContent = `(${shown.length} of ${state.moments.length})`;
  renderTickCount();
}

function renderTickCount() {
  const n = state.ticked.size;
  $("tick-count").textContent = `${n} ticked`;
  $("build").disabled = n === 0;
}

// ---------------------------------------------------------------- actions

async function buildDraft(event) {
  event.preventDefault();
  const ids = state.moments.filter((m) => state.ticked.has(momentKey(m))).map((m) => m.id);
  const body = { preset: $("preset").value, moment_ids: ids, mode: $("mode").value };
  const budgetText = $("budget").value.trim();
  if (budgetText !== "") {
    const budget = Number(budgetText);
    if (!Number.isFinite(budget) || budget <= 0) {
      showMessage("The budget must be a number of seconds above 0 (or empty for none).", true);
      return;
    }
    body.budget = budget;
  }
  $("build").disabled = true;
  try {
    const data = await api("POST", "/api/preset", body);
    state.draftInfo = { notes: data.notes || [] };
    setShotlist(data, { record: true });
    const extra = data.budget_met === false ? " The budget could not be met exactly." : "";
    showMessage(`Built ${data.shotlist.shots.length} shot(s) from ${ids.length} moment(s).${extra}`, false);
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    renderTickCount();
  }
}

async function saveFile() {
  try {
    const data = await api("POST", "/api/shotlist/save", {});
    markSaved();
    showMessage(`Saved ${data.shots} shot(s) to ${data.saved}.`, false);
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function reloadFile() {
  try {
    const data = await api("POST", "/api/shotlist/load", {});
    state.draftInfo = null;
    setShotlist(data, { record: true, saved: true });
    showMessage(`Loaded ${data.shotlist.shots.length} shot(s) from ${data.loaded}.`, false);
  } catch (error) {
    showMessage(error.message, true);
  }
}

// ---------------------------------------------------------------- start

async function main() {
  try {
    const [demo, moments, shotlist] = await Promise.all([
      api("GET", "/api/demo"),
      api("GET", "/api/moments"),
      api("GET", "/api/shotlist"),
    ]);
    state.demo = demo;
    state.moments = moments.moments;
    loadLocal();

    const rounds = demo.rounds.length;
    $("demo-info").textContent =
      `${demo.path} - ${demo.map_name || "unknown map"}, ${rounds} rounds, ${demo.players.length} players`;
    $("save-path").textContent = demo.save_path ? `File: ${demo.save_path}` : "No file given";
    $("save").disabled = $("reload-file").disabled = !demo.save_path;

    const presetSelect = $("preset");
    for (const name of demo.presets) {
      const option = el("option", null, name);
      option.value = name;
      presetSelect.append(option);
    }
    if (demo.presets.includes("killer")) presetSelect.value = "killer";
    const showPresetInfo = () => {
      $("preset-description").textContent = PRESET_INFO[presetSelect.value] || "";
    };
    presetSelect.addEventListener("change", showPresetInfo);
    showPresetInfo();

    buildFilterOptions();
    for (const id of ["f-player", "f-round", "f-kind", "f-weapon", "f-tag"]) {
      $(id).addEventListener("change", onFilterChange);
    }
    $("f-reset").addEventListener("click", () => {
      state.filters = { player: ALL, round: ALL, kind: ALL, weapon: ALL, tag: ALL };
      buildFilterOptions();
      saveLocal();
      renderMoments();
    });
    $("tick-visible").addEventListener("click", () => {
      state.moments.filter(passes).forEach((m) => state.ticked.add(momentKey(m)));
      saveLocal();
      renderMoments();
    });
    $("untick-all").addEventListener("click", () => {
      state.ticked.clear();
      saveLocal();
      renderMoments();
    });
    $("draft").addEventListener("submit", buildDraft);
    $("save").addEventListener("click", saveFile);
    $("reload-file").addEventListener("click", reloadFile);

    renderMoments();
    initTimeline();
    setShotlist(shotlist);
  } catch (error) {
    $("demo-info").textContent = "";
    showMessage(`Could not load the demo: ${error.message}`, true);
  }
}

// the scripts are at the end of the page; wait until timeline.js has loaded too
document.addEventListener("DOMContentLoaded", main);
