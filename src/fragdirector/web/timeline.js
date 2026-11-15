// The editable timeline (T11).
//
// Every edit is one request to the server (/api/edit/...), which checks it
// against the demo and answers with the whole new shot list. A refused edit
// changes nothing and the server's message is shown. Undo and redo keep the
// earlier shot lists in this page and put one back with PUT /api/shotlist.
//
// Loaded after app.js and uses its helpers ($, el, api, state, showMessage,
// formatClock, formatSeconds, playerName). All text goes on the page with
// textContent; the page uses no inline scripts or styles.
"use strict";

const HISTORY_LIMIT = 200;
const NUDGE_SECONDS = 0.5;
const DEFAULT_SHOT_SECONDS = 5;

const CAPTURE_POLL_MS = 2000;

const undoStack = { past: [], future: [] }; // whole shot lists (the "shotlist" part of a reply)
let busy = false; // one request at a time, so edits cannot overtake each other
let savedSnapshot = null; // the shots as they were when last saved or loaded
let captureInfo = null; // last reply of GET /api/capture
let capturePoll = null; // timer while a capture runs
let renderInfo = null; // last reply of GET /api/render
let renderPoll = null; // timer while the video is being made

// ---------------------------------------------------------------- helpers

function snapshotOf(shotlist) {
  return JSON.stringify(shotlist.shots);
}

function roundOf(tick) {
  return state.demo.rounds.find((r) => r.start_tick <= tick && tick < r.end_tick) || null;
}

// "1:23.4"; a negative time (freeze time) gets a leading "-".
function clockText(ticks, rate) {
  const tenths = Math.round(Math.abs(ticks / rate) * 10);
  const minutes = Math.floor(tenths / 600);
  const seconds = (tenths % 600) / 10;
  const sign = ticks < 0 && tenths > 0 ? "-" : "";
  return `${sign}${minutes}:${seconds.toFixed(1).padStart(4, "0")}`;
}

function spanText(startTick, endTick, rnd, rate) {
  if (!rnd) return `ticks ${startTick} to ${endTick}`;
  const from = clockText(startTick - rnd.freeze_end_tick, rate);
  const to = clockText(endTick - rnd.freeze_end_tick, rate);
  return `Round ${rnd.number}, ${from} to ${to}`;
}

function lengthText(startTick, endTick, speed, rate) {
  const seconds = (endTick - startTick) / rate / speed;
  return formatSeconds(seconds) + (speed === 1 ? "" : ` at ${speed}x`);
}

// The latest end a shot following this life may have: the end of the round for
// a survivor, else the death tick plus the time after death (state.demo), cut
// at the end of the round. The same rule as Demo.latest_shot_end on the server.
function latestShotEnd(rnd, life) {
  if (life.until_tick >= rnd.end_tick) return rnd.end_tick;
  return Math.min(life.until_tick + state.demo.after_death_ticks, rnd.end_tick);
}

// Players a shot over this span could follow: alive when it starts, and not
// dead for longer than the limit when it ends. The server has the last word.
function playersForSpan(startTick, endTick, rnd = roundOf(startTick)) {
  if (!rnd) return [];
  return rnd.lives
    .filter((life) => life.from_tick <= startTick && startTick < life.until_tick
      && endTick <= latestShotEnd(rnd, life))
    .map((life) => life.player);
}

// "Ends 1.5 s after X dies ..." for a shot that runs past its player's death, else "".
function afterDeathText(shot, endTick, rnd) {
  const life = rnd ? rnd.lives.find((l) => l.player === shot.pov_player) : null;
  if (!life || endTick <= life.until_tick) return "";
  const seconds = (endTick - life.until_tick) / state.shotlist.tick_rate;
  return `Ends ${formatSeconds(seconds)} after ${playerName(shot.pov_player)} dies, on the game's camera by the body.`;
}

function sortedByName(ids) {
  return [...ids].sort((a, b) => playerName(a).localeCompare(playerName(b)));
}

function killsBetween(startTick, endTick) {
  const seen = new Set();
  const found = [];
  for (const m of state.moments) {
    for (const k of m.kills) {
      const id = `${k.tick}|${k.victim}`;
      if (seen.has(id) || k.tick < startTick || k.tick > endTick) continue;
      seen.add(id);
      found.push(k);
    }
  }
  return found.sort((a, b) => a.tick - b.tick);
}

function describeKill(k, rate, freezeEnd) {
  const flags = [];
  if (k.headshot) flags.push("HS");
  if (k.noscope) flags.push("noscope");
  if (k.through_smoke) flags.push("smoke");
  if (k.penetrated) flags.push("wallbang");
  const clock = freezeEnd === null ? "" : `${formatClock((k.tick - freezeEnd) / rate)} `;
  const who = k.attacker_name ? `${k.attacker_name} killed ${k.victim_name}` : `${k.victim_name} died`;
  return `${clock}${who} (${[k.weapon, ...flags].filter(Boolean).join(", ")})`;
}

// ---------------------------------------------------------------- state

function setShotlist(data, options = {}) {
  const previous = state.shotlist;
  if (options.record && previous) {
    undoStack.past.push(previous.raw);
    if (undoStack.past.length > HISTORY_LIMIT) undoStack.past.shift();
    undoStack.future.length = 0;
  }
  state.shotlist = {
    raw: data.shotlist,
    shots: data.shotlist.shots,
    tick_rate: data.shotlist.tick_rate,
    duration_seconds: data.duration_seconds,
    problems: data.problems,
  };
  if (options.saved || savedSnapshot === null) savedSnapshot = snapshotOf(data.shotlist);
  renderTimeline();
  refreshCapture(); // which shots have a clip depends on the shots
}

// ---------------------------------------------------------------- capture
//
// Capturing runs on the server in the background (CS2 starts and takes over
// the screen). The page polls GET /api/capture while it runs. Only the
// capture parts of the page are updated, so typing in a shot is not disturbed.

function captureRunning() {
  return Boolean(captureInfo && captureInfo.capture.state === "running");
}

function applyCaptureInfo() {
  applyRenderInfo(); // the video button depends on which clips exist
  const info = captureInfo;
  const captured = info ? info.captured : [];
  document.querySelectorAll(".clip-mark").forEach((node) => {
    node.textContent = captured[Number(node.dataset.index)] ? "clip ready" : "";
  });
  document.querySelectorAll("button.capture").forEach((node) => {
    node.disabled = busy || captureRunning() || !info || !info.clips_dir;
  });
  const box = $("capture-status");
  if (!info || info.capture.state === "idle") {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.className = `capture-status ${info.capture.state}`;
  $("capture-message").textContent = info.capture.message;
  $("capture-log").textContent = info.capture.log.join("\n");
}

async function refreshCapture() {
  try {
    captureInfo = await api("GET", "/api/capture");
  } catch (error) {
    return; // the next edit or poll tries again
  }
  applyCaptureInfo();
  if (captureRunning() && capturePoll === null) {
    capturePoll = setInterval(refreshCapture, CAPTURE_POLL_MS);
  } else if (!captureRunning() && capturePoll !== null) {
    clearInterval(capturePoll);
    capturePoll = null;
  }
}

async function captureShot(index) {
  if (busy || captureRunning()) return;
  const ok = window.confirm(
    `Capture shot ${index + 1} now?\n\nCS2 will start and take over the screen for about a minute. ` +
    "Steam must be running and CS2 closed. Any clip this shot already has is replaced.");
  if (!ok) return;
  try {
    captureInfo = await api("POST", "/api/capture", { index });
    showMessage(`Capturing shot ${index + 1}. Leave the game alone until it closes.`, false);
  } catch (error) {
    showMessage(`Not captured: ${error.message}`, true);
  }
  refreshCapture();
}

// ---------------------------------------------------------------- make the video
//
// Putting the clips together also runs on the server in the background
// (FFmpeg, a minute or so). It needs a clip for every shot, which is why the
// button waits for "clip ready" on all of them.

function renderRunning() {
  return Boolean(renderInfo && renderInfo.render.state === "running");
}

function allClipsReady() {
  const captured = captureInfo ? captureInfo.captured : [];
  return captured.length > 0 && captured.every(Boolean);
}

function applyRenderInfo() {
  const info = renderInfo;
  const shots = state.shotlist ? state.shotlist.shots.length : 0;
  let hint = "";
  if (!info || info.video_path === null) hint = "Start the editor with a shot list file to make a video.";
  else if (shots === 0) hint = "The timeline is empty.";
  else if (captureRunning()) hint = "Wait for the capture to finish.";
  else if (!allClipsReady()) hint = "Every shot needs its clip first: use Capture on the shots marked without one.";
  else if (info.video_exists) hint = `Making it again replaces ${info.video_path}.`;
  else hint = `Writes ${info.video_path}.`;
  $("video-hint").textContent = hint;
  $("make-video-button").disabled = busy || renderRunning() || captureRunning() || shots === 0
    || !info || info.video_path === null || !allClipsReady();

  const box = $("render-status");
  if (!info || info.render.state === "idle") {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.className = `capture-status ${info.render.state}`;
  $("render-message").textContent = info.render.message;
  $("render-log").textContent = info.render.log.join("\n");
}

async function refreshRender() {
  try {
    renderInfo = await api("GET", "/api/render");
  } catch (error) {
    return; // the next poll or edit tries again
  }
  applyRenderInfo();
  if (renderRunning() && renderPoll === null) {
    renderPoll = setInterval(refreshRender, CAPTURE_POLL_MS);
  } else if (!renderRunning() && renderPoll !== null) {
    clearInterval(renderPoll);
    renderPoll = null;
  }
}

async function makeVideo(event) {
  event.preventDefault();
  if (busy || renderRunning()) return;
  const crossfade = Number($("video-crossfade").value.trim() === "" ? "0" : $("video-crossfade").value);
  if (!Number.isFinite(crossfade) || crossfade < 0 || crossfade > 5) {
    showMessage("The crossfade must be a number of seconds from 0 to 5.", true);
    return;
  }
  try {
    renderInfo = await api("POST", "/api/render", { crossfade, labels: $("video-labels").checked });
    showMessage("Making the video. This takes a minute or so; the page stays usable.", false);
  } catch (error) {
    showMessage(`Video not made: ${error.message}`, true);
  }
  refreshRender();
}

function markSaved() {
  savedSnapshot = snapshotOf(state.shotlist.raw);
  renderToolbar();
}

// While a request is out, every control is off; when it ends the timeline is
// drawn again, which turns the right ones back on.
function setBusy(value) {
  busy = value;
  if (state.shotlist) renderTimeline();
  else renderToolbar();
}

// Send one edit. Resolves true if the server accepted it.
async function edit(operation, body, doneText) {
  if (busy) return false;
  setBusy(true);
  try {
    const data = await api("POST", `/api/edit/${operation}`, body);
    setShotlist(data, { record: true });
    showMessage(doneText || "", false);
    return true;
  } catch (error) {
    showMessage(`Not changed: ${error.message}`, true);
    renderTimeline(); // puts a dragged handle or a typed value back
    return false;
  } finally {
    setBusy(false);
  }
}

async function stepHistory(from, to, doneText) {
  if (busy || from.length === 0) return;
  setBusy(true);
  try {
    const target = from[from.length - 1];
    const data = await api("PUT", "/api/shotlist", { shotlist: target });
    from.pop();
    to.push(state.shotlist.raw);
    setShotlist(data);
    showMessage(doneText, false);
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    setBusy(false);
  }
}

const undo = () => stepHistory(undoStack.past, undoStack.future, "Undone.");
const redo = () => stepHistory(undoStack.future, undoStack.past, "Redone.");

// ---------------------------------------------------------------- one shot

function button(text, label, onClick, disabled) {
  const node = el("button", null, text);
  node.type = "button";
  node.title = label;
  node.setAttribute("aria-label", label);
  node.disabled = busy || Boolean(disabled);
  node.addEventListener("click", onClick);
  return node;
}

function nudgeTicks() {
  return Math.round(state.shotlist.tick_rate * NUDGE_SECONDS);
}

function nudgeButtons(index, which) {
  const ticks = nudgeTicks();
  const key = which === "start" ? "start_delta" : "end_delta";
  const noun = which === "start" ? "start" : "end";
  return [
    button(`-${NUDGE_SECONDS}s`, `Move the ${noun} of shot ${index + 1} ${NUDGE_SECONDS} s earlier`,
      () => edit("trim_by", { index, [key]: -ticks }, `Moved the ${noun} of shot ${index + 1} earlier.`)),
    button(`+${NUDGE_SECONDS}s`, `Move the ${noun} of shot ${index + 1} ${NUDGE_SECONDS} s later`,
      () => edit("trim_by", { index, [key]: ticks }, `Moved the ${noun} of shot ${index + 1} later.`)),
  ];
}

// The bar under a shot: the shot inside its round (with some margin), the
// time its player is alive, the time after their death a shot may still use
// (hatched), the kills, and a handle at each end to drag. The part of the shot
// after the death has its own colour.
function spanBar(shot, index, rnd, onDragText) {
  const rate = state.shotlist.tick_rate;
  const padding = Math.max(5 * rate, Math.round((shot.end_tick - shot.start_tick) / 2));
  const lo = Math.max(rnd.start_tick, shot.start_tick - padding);
  const hi = Math.min(rnd.end_tick, shot.end_tick + padding);
  const range = Math.max(1, hi - lo);
  const percent = (tick) => ((Math.min(hi, Math.max(lo, tick)) - lo) / range) * 100;

  const track = el("div", "span-track");
  const life = rnd.lives.find((l) => l.player === shot.pov_player);
  if (life) {
    const alive = el("div", "span-alive");
    alive.style.left = `${percent(life.from_tick)}%`;
    alive.style.width = `${percent(life.until_tick) - percent(life.from_tick)}%`;
    alive.title = `${playerName(shot.pov_player)} is alive here`;
    track.append(alive);
    if (life.until_tick < rnd.end_tick) {
      const limit = latestShotEnd(rnd, life);
      const zone = el("div", "span-after-zone");
      zone.style.left = `${percent(life.until_tick)}%`;
      zone.style.width = `${percent(limit) - percent(life.until_tick)}%`;
      zone.title = `A shot can stay on ${playerName(shot.pov_player)} up to ` +
        `${formatSeconds(state.demo.after_death_seconds)} after they die (until the round ends).`;
      track.append(zone);
    }
  }
  const selection = el("div", "span-selection");
  const afterPart = el("div", "span-selection-after");
  track.append(selection, afterPart);

  for (const k of killsBetween(lo, hi)) {
    const mark = el("div", "span-kill");
    mark.style.left = `${percent(k.tick)}%`;
    mark.title = describeKill(k, rate, rnd.freeze_end_tick);
    track.append(mark);
  }

  const handles = {};
  let startTick = shot.start_tick;
  let endTick = shot.end_tick;
  const place = () => {
    selection.style.left = `${percent(startTick)}%`;
    selection.style.width = `${percent(endTick) - percent(startTick)}%`;
    const death = life && life.until_tick < rnd.end_tick ? life.until_tick : null;
    afterPart.hidden = death === null || endTick <= death;
    if (!afterPart.hidden) {
      const from = Math.max(startTick, death);
      afterPart.style.left = `${percent(from)}%`;
      afterPart.style.width = `${percent(endTick) - percent(from)}%`;
    }
    handles.start.style.left = `${percent(startTick)}%`;
    handles.end.style.left = `${percent(endTick)}%`;
  };

  for (const which of ["start", "end"]) {
    const handle = el("div", `span-handle ${which}`);
    handle.title = `Drag to change the ${which} of the shot`;
    handle.setAttribute("aria-label", `Drag to change the ${which} of shot ${index + 1}`);
    handles[which] = handle;
    track.append(handle);

    handle.addEventListener("pointerdown", (event) => {
      if (busy || event.button !== 0) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const box = track.getBoundingClientRect();
      const original = which === "start" ? shot.start_tick : shot.end_tick;
      track.classList.add("dragging");

      const tickAt = (clientX) => {
        const raw = Math.round(lo + ((clientX - box.left) / box.width) * range);
        const tick = Math.min(hi, Math.max(lo, raw));
        return which === "start" ? Math.min(tick, shot.end_tick - 1) : Math.max(tick, shot.start_tick + 1);
      };
      const onMove = (e) => {
        const tick = tickAt(e.clientX);
        if (which === "start") startTick = tick;
        else endTick = tick;
        place();
        onDragText(startTick, endTick);
      };
      const finish = () => {
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        handle.removeEventListener("pointercancel", onCancel);
        track.classList.remove("dragging");
      };
      const onUp = () => {
        finish();
        const tick = which === "start" ? startTick : endTick;
        if (tick === original) return;
        const body = which === "start" ? { index, start_tick: tick } : { index, end_tick: tick };
        edit("trim", body, `Trimmed shot ${index + 1}.`);
      };
      const onCancel = () => {
        finish();
        renderTimeline();
      };
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
      handle.addEventListener("pointercancel", onCancel);
    });
  }
  place();

  const axis = el("div", "span-axis muted small",
    el("span", null, clockText(lo - rnd.freeze_end_tick, rate)),
    el("span", null, clockText(hi - rnd.freeze_end_tick, rate)));
  return el("div", "span-bar", track, axis);
}

function povSelect(shot, index) {
  const ids = new Set(playersForSpan(shot.start_tick, shot.end_tick));
  ids.add(shot.pov_player); // a hand-edited file may name someone who is not allowed
  const select = el("select");
  select.setAttribute("aria-label", `Whose view shot ${index + 1} shows`);
  for (const id of sortedByName(ids)) {
    const option = el("option", null, playerName(id));
    option.value = id;
    select.append(option);
  }
  select.value = shot.pov_player;
  select.disabled = busy;
  select.addEventListener("change", () => {
    if (select.value === shot.pov_player) return;
    edit("change_pov", { index, pov_player: select.value }, `Shot ${index + 1} now shows ${playerName(select.value)}.`);
  });
  return select;
}

function speedInput(shot, index) {
  const input = el("input");
  input.type = "number";
  input.step = "0.05";
  input.value = String(shot.speed);
  input.disabled = busy;
  input.setAttribute("aria-label", `Speed of shot ${index + 1} (1 is real time)`);
  input.addEventListener("change", () => {
    const text = input.value.trim();
    const speed = text === "" ? NaN : Number(text);
    if (!Number.isFinite(speed)) {
      showMessage("Not changed: the speed must be a number.", true);
      input.value = String(shot.speed);
      return;
    }
    if (speed === shot.speed) return;
    edit("set_speed", { index, speed }, `Set the speed of shot ${index + 1} to ${speed}x.`);
  });
  return input;
}

function labelInput(shot, index) {
  const input = el("input");
  input.type = "text";
  input.value = shot.label;
  input.disabled = busy;
  input.placeholder = "label";
  input.setAttribute("aria-label", `Label of shot ${index + 1}`);
  input.addEventListener("change", () => {
    if (input.value === shot.label) return;
    edit("set_label", { index, label: input.value }, `Changed the label of shot ${index + 1}.`);
  });
  return input;
}

let dragFrom = null; // index of the shot being dragged to a new place

function clipMark(index) {
  const node = el("span", "clip-mark small");
  node.dataset.index = String(index);
  return node;
}

function captureButton(index) {
  const node = button("Capture", `Record shot ${index + 1} from CS2 now`, () => captureShot(index));
  node.classList.add("capture");
  return node;
}

function clearDropMarks() {
  document.querySelectorAll(".shot.drop-before, .shot.drop-after").forEach((node) => {
    node.classList.remove("drop-before", "drop-after");
  });
}

function shotCard(shot, index) {
  const shots = state.shotlist.shots;
  const rate = state.shotlist.tick_rate;
  const rnd = roundOf(shot.start_tick);
  const freezeEnd = rnd ? rnd.freeze_end_tick : null;

  const grip = el("span", "grip", "::");
  grip.title = "Drag to reorder";
  grip.setAttribute("aria-hidden", "true");
  const length = el("span", "shot-length", lengthText(shot.start_tick, shot.end_tick, shot.speed, rate));
  const span = el("div", "muted small", spanText(shot.start_tick, shot.end_tick, rnd, rate));
  const after = el("div", "after-death-note small", afterDeathText(shot, shot.end_tick, rnd));
  after.hidden = after.textContent === "";
  const onDragText = (startTick, endTick) => {
    span.textContent = spanText(startTick, endTick, rnd, rate);
    length.textContent = lengthText(startTick, endTick, shot.speed, rate);
    after.textContent = afterDeathText(shot, endTick, rnd);
    after.hidden = after.textContent === "";
  };

  const kills = killsBetween(shot.start_tick, shot.end_tick);
  const card = el("li", "shot",
    el("div", "shot-head",
      grip,
      el("span", "shot-index", String(index + 1)),
      povSelect(shot, index),
      length,
      clipMark(index)),
    span,
    after,
    rnd ? spanBar(shot, index, rnd, onDragText) : null,
    el("div", "shot-controls",
      el("span", "control", "Start", ...nudgeButtons(index, "start")),
      el("span", "control", "End", ...nudgeButtons(index, "end")),
      el("label", "control", "Speed", speedInput(shot, index)),
      el("label", "control grow", "Label", labelInput(shot, index))),
    el("div", "shot-buttons",
      button("Up", `Move shot ${index + 1} earlier in the timeline`,
        () => edit("move", { from_index: index, to_index: index - 1 }, `Moved shot ${index + 1} up.`), index === 0),
      button("Down", `Move shot ${index + 1} later in the timeline`,
        () => edit("move", { from_index: index, to_index: index + 1 }, `Moved shot ${index + 1} down.`),
        index === shots.length - 1),
      button("Duplicate", `Copy shot ${index + 1}`,
        () => edit("duplicate", { index }, `Duplicated shot ${index + 1}.`)),
      button("Delete", `Delete shot ${index + 1}`,
        () => edit("delete", { index }, `Deleted shot ${index + 1}.`)),
      captureButton(index)),
    kills.length
      ? el("ul", "shot-kills", ...kills.map((k) => el("li", null, describeKill(k, rate, freezeEnd))))
      : null);

  // Reordering by drag: only the grip starts a drag, so the inputs keep working.
  grip.addEventListener("pointerdown", () => { if (!busy) card.draggable = true; });
  card.addEventListener("dragstart", (event) => {
    dragFrom = index;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(index));
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => {
    dragFrom = null;
    card.draggable = false;
    card.classList.remove("dragging");
    clearDropMarks();
  });
  card.addEventListener("dragover", (event) => {
    if (dragFrom === null) return;
    event.preventDefault();
    const box = card.getBoundingClientRect();
    const before = event.clientY < box.top + box.height / 2;
    clearDropMarks();
    card.classList.add(before ? "drop-before" : "drop-after");
  });
  card.addEventListener("drop", (event) => {
    if (dragFrom === null) return;
    event.preventDefault();
    const box = card.getBoundingClientRect();
    const slot = event.clientY < box.top + box.height / 2 ? index : index + 1; // gap, 0..n
    const from = dragFrom;
    const to = slot > from ? slot - 1 : slot;
    dragFrom = null;
    clearDropMarks();
    if (to !== from) edit("move", { from_index: from, to_index: to }, `Moved shot ${from + 1} to place ${to + 1}.`);
  });
  return card;
}

// ---------------------------------------------------------------- the timeline

function renderToolbar() {
  $("undo").disabled = busy || undoStack.past.length === 0;
  $("redo").disabled = busy || undoStack.future.length === 0;
  const unsaved = state.shotlist && snapshotOf(state.shotlist.raw) !== savedSnapshot;
  $("dirty").hidden = !unsaved;
}

function renderTimeline() {
  const shots = state.shotlist.shots;
  $("timeline").replaceChildren(...shots.map(shotCard));
  if (shots.length === 0) {
    $("timeline").append(el("li", "muted empty",
      "The timeline is empty. Tick some moments and build a first draft, or add a shot by hand."));
  }
  const total = state.shotlist.duration_seconds;
  $("timeline-total").textContent = shots.length
    ? `(${shots.length} shot${shots.length === 1 ? "" : "s"}, ${formatSeconds(total)} in the video)`
    : "";

  $("problems").replaceChildren(...state.shotlist.problems.map((p) => el("li", null, p)));
  const info = state.draftInfo;
  $("draft-notes").replaceChildren(...(info ? info.notes : []).map((n) => el("li", null, n)));
  renderToolbar();
  applyCaptureInfo();
}

// ---------------------------------------------------------------- add a shot by hand

function addSpan() {
  const rnd = state.demo.rounds.find((r) => String(r.number) === $("add-round").value);
  const rate = state.demo.tick_rate;
  const start = Number($("add-start").value);
  const length = Number($("add-length").value);
  if (!rnd || !Number.isFinite(start) || !Number.isFinite(length) || length <= 0) return null;
  const startTick = rnd.freeze_end_tick + Math.round(start * rate);
  const endTick = startTick + Math.max(1, Math.round(length * rate));
  // a start or length that runs past the round would land in another round
  return { rnd, startTick, endTick, inside: startTick >= rnd.start_tick && endTick <= rnd.end_tick };
}

function refreshAddPlayers() {
  const select = $("add-player");
  const span = addSpan();
  const previous = select.value;
  select.replaceChildren();
  const ids = span && span.inside ? playersForSpan(span.startTick, span.endTick, span.rnd) : [];
  if (ids.length === 0) {
    const option = el("option", null, "nobody can show this span");
    option.value = "";
    select.append(option);
    return;
  }
  for (const id of sortedByName(ids)) {
    const option = el("option", null, playerName(id));
    option.value = id;
    select.append(option);
  }
  if (ids.includes(previous)) select.value = previous;
}

function initAddForm() {
  const rounds = $("add-round");
  for (const rnd of state.demo.rounds) {
    const option = el("option", null, `Round ${rnd.number}`);
    option.value = String(rnd.number);
    rounds.append(option);
  }
  $("add-length").value = String(DEFAULT_SHOT_SECONDS);
  $("add-start").value = "0";
  for (const id of ["add-round", "add-start", "add-length"]) {
    $(id).addEventListener("input", refreshAddPlayers);
  }
  refreshAddPlayers();

  $("add-shot").addEventListener("submit", (event) => {
    event.preventDefault();
    const span = addSpan();
    if (!span) {
      showMessage("Not added: give a round, a start time and a length above 0 seconds.", true);
      return;
    }
    if (!span.inside) {
      const rate = state.demo.tick_rate;
      const last = (span.rnd.end_tick - span.rnd.freeze_end_tick) / rate;
      showMessage(`Not added: that span is not inside round ${span.rnd.number} ` +
        `(its clock runs from ${((span.rnd.start_tick - span.rnd.freeze_end_tick) / rate).toFixed(1)} s ` +
        `to ${last.toFixed(1)} s).`, true);
      return;
    }
    const player = $("add-player").value;
    if (!player) {
      showMessage("Not added: nobody can show that span. The player has to be alive at its start " +
        `and can be dead for at most ${formatSeconds(state.demo.after_death_seconds)} at its end.`, true);
      return;
    }
    const speedText = $("add-speed").value.trim();
    const speed = speedText === "" ? 1 : Number(speedText);
    if (!Number.isFinite(speed)) {
      showMessage("Not added: the speed must be a number.", true);
      return;
    }
    const shot = {
      start_tick: span.startTick,
      end_tick: span.endTick,
      pov_player: player,
      speed,
      label: $("add-label").value,
    };
    edit("insert", { shot }, "Added a shot at the end of the timeline.").then((added) => {
      if (added) $("add-label").value = "";
    });
  });
}

// ---------------------------------------------------------------- start

function initTimeline() {
  $("undo").addEventListener("click", undo);
  $("redo").addEventListener("click", redo);
  $("make-video").addEventListener("submit", makeVideo);
  refreshRender();
  initAddForm();
  document.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return; // keep the browser's own undo there
    const key = event.key.toLowerCase();
    if (key === "z" && !event.shiftKey) { event.preventDefault(); undo(); }
    else if (key === "y" || (key === "z" && event.shiftKey)) { event.preventDefault(); redo(); }
  });
}
