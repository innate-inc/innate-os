// @ts-check
// Sim-only Benchmark page: drives the host-side spatial-memory benchmark
// service through the /benchmark proxy — capture datasets, inspect what the
// admission policy kept, run retrieval benchmarks, and browse results.
// Plain page: no ROS session; everything is same-origin HTTP.

import { ageText } from "../map/memories.js";
import { fmtLatency, fmtScore, parseState, runLabel, scoreClass, summarize } from "./data.js";

const BASE = "/benchmark";

const STYLE = `
.bench-page { position: absolute; inset: 0; display: flex; flex-direction: column; overflow: hidden; }
.bench-head { display: flex; align-items: baseline; gap: 14px; padding: 18px 24px 10px; }
.bench-head h1 { margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -.02em; }
.bench-chip { font: 500 11px var(--font-mono, monospace); padding: 3px 8px; border-radius: 999px;
  border: 1px solid var(--hairline, #2a2f3a); color: var(--muted, #8a90a0); }
.bench-chip.on { color: var(--ok, #35c56f); border-color: var(--ok, #35c56f); }
.bench-chip.warn { color: var(--accent, #e8a33d); border-color: var(--accent, #e8a33d); }
.bench-grid { flex: 1; min-height: 0; display: grid; grid-template-columns: 1fr 1fr 1.2fr; gap: 14px; padding: 0 24px 20px; }
.bench-col { min-height: 0; overflow-y: auto; border: 1px solid var(--hairline, #2a2f3a);
  border-radius: 12px; background: var(--panel, #14161c); padding: 14px; }
.bench-col h2 { margin: 0 0 10px; font-size: 12px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .08em; color: var(--muted, #8a90a0); }
.bench-card { border: 1px solid var(--hairline, #2a2f3a); border-radius: 10px; padding: 10px 12px; margin-bottom: 10px; overflow-x: auto; }
.bench-card.sel { border-color: var(--accent, #e8a33d); }
.bench-card-title { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; cursor: pointer; }
.bench-card-title b { font-size: 14px; }
.bench-sub { color: var(--muted, #8a90a0); font-size: 12px; margin-top: 2px; }
.bench-row { display: flex; align-items: center; gap: 8px; margin: 8px 0; flex-wrap: wrap; }
.bench-row label { font-size: 12px; color: var(--muted, #8a90a0); }
.bench-row input[type=text], .bench-row select { background: var(--bg, #0d0f13); color: var(--text, #e7e7ea);
  border: 1px solid var(--hairline, #2a2f3a); border-radius: 7px; padding: 5px 8px; font: 500 12px var(--font-mono, monospace); }
.bench-btn { background: var(--bg, #0d0f13); color: var(--text, #e7e7ea); border: 1px solid var(--hairline, #2a2f3a);
  border-radius: 8px; padding: 6px 12px; font: 600 12px var(--font-ui, sans-serif); cursor: pointer; }
.bench-btn:hover:not(:disabled) { border-color: var(--accent, #e8a33d); }
.bench-btn:disabled { opacity: .45; cursor: default; }
.bench-btn.primary { border-color: var(--accent, #e8a33d); color: var(--accent, #e8a33d); }
.bench-thumbs { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 6px; margin-top: 8px; }
.bench-thumb { position: relative; border-radius: 6px; overflow: hidden; cursor: zoom-in; }
.bench-thumb img { display: block; width: 100%; aspect-ratio: 4/3; object-fit: cover; }
.bench-thumb i { position: absolute; left: 0; right: 0; bottom: 0; padding: 1px 4px; font: 500 9px var(--font-mono, monospace);
  font-style: normal; color: #fff; background: rgba(0,0,0,.55); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bench-log { background: var(--bg, #0d0f13); border: 1px solid var(--hairline, #2a2f3a); border-radius: 8px;
  padding: 8px 10px; margin-top: 8px; max-height: 260px; overflow-y: auto; font: 400 11px var(--font-mono, monospace);
  white-space: pre-wrap; word-break: break-word; color: var(--muted, #a8aebc); }
.bench-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
.bench-table th { text-align: left; color: var(--muted, #8a90a0); font-weight: 500; padding: 4px 6px;
  border-bottom: 1px solid var(--hairline, #2a2f3a); }
.bench-table td { padding: 4px 6px; border-bottom: 1px solid var(--hairline, #2a2f3a); vertical-align: top; }
.bench-score { font: 600 12px var(--font-mono, monospace); }
.bench-score.ok { color: var(--ok, #35c56f); } .bench-score.mid { color: var(--accent, #e8a33d); }
.bench-score.bad { color: var(--danger, #e05656); }
.bench-empty { color: var(--muted, #8a90a0); font-size: 13px; padding: 8px 2px; }
.bench-offline { margin: auto; text-align: center; color: var(--muted, #8a90a0); font-size: 14px; max-width: 460px; }
`;

/**
 * @param {string} path
 * @param {RequestInit} [init]
 * @returns {Promise<any>}
 */
async function api(path, init) {
  const resp = await fetch(BASE + path, init);
  if (!resp.ok) throw new Error(`${resp.status}`);
  return resp.json();
}

/**
 * @param {string} tag
 * @param {string} [className]
 * @param {string} [text]
 * @returns {HTMLElement}
 */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/** @param {HTMLElement} stage */
export function mount(stage) {
  const style = document.createElement("style");
  style.textContent = STYLE;
  document.head.appendChild(style);

  const root = el("div", "bench-page");
  stage.appendChild(root);

  let destroyed = false;
  let pollTimer = 0;
  let failedPolls = 0;
  let selectedDataset = "";
  let selectedRun = "";

  const head = el("div", "bench-head");
  head.appendChild(el("h1", "", "Benchmark"));
  const chips = el("div", "bench-row");
  head.appendChild(chips);
  const grid = el("div", "bench-grid");
  const colData = el("section", "bench-col");
  const colRun = el("section", "bench-col");
  const colResults = el("section", "bench-col");
  grid.append(colData, colRun, colResults);

  function offline() {
    root.textContent = "";
    const note = el("div", "bench-offline");
    note.append(
      el("p", "", "Benchmark service unreachable."),
      el("p", "", "It starts with `innate-sim up` on the sim host — see `innate-sim logs benchmark`."),
    );
    root.appendChild(note);
  }

  /** @param {ReturnType<typeof parseState>} state */
  function renderChips(state) {
    chips.textContent = "";
    chips.appendChild(el("span", "bench-chip on", "service up"));
    chips.appendChild(
      state.geminiKey
        ? el("span", "bench-chip on", "gemini key")
        : el("span", "bench-chip warn", "no gemini key — live runs disabled"),
    );
    if (state.job?.running) chips.appendChild(el("span", "bench-chip warn", `running: ${state.job.kind}`));
  }

  /** @param {ReturnType<typeof parseState>} state */
  function renderDatasets(state) {
    colData.textContent = "";
    colData.appendChild(el("h2", "", "Datasets"));

    const form = el("div", "bench-row");
    const nameInput = /** @type {HTMLInputElement} */ (el("input"));
    nameInput.type = "text";
    nameInput.value = selectedDataset || "home_tour";
    const modeSel = /** @type {HTMLSelectElement} */ (el("select"));
    for (const mode of ["glide", "drive"]) modeSel.appendChild(new Option(mode, mode));
    const captureBtn = /** @type {HTMLButtonElement} */ (el("button", "bench-btn primary", "Capture"));
    captureBtn.disabled = !!state.job?.running;
    captureBtn.onclick = () => run({ kind: "capture", dataset: nameInput.value.trim(), mode: modeSel.value });
    form.append(nameInput, modeSel, captureBtn);
    colData.appendChild(form);

    if (!state.datasets.length) colData.appendChild(el("div", "bench-empty", "No datasets yet — capture one."));
    for (const ds of state.datasets) {
      const card = el("div", "bench-card" + (ds.name === selectedDataset ? " sel" : ""));
      const title = el("div", "bench-card-title");
      title.append(el("b", "", ds.name), el("span", "bench-sub", ageText(ds.capture.captured_at)));
      title.onclick = () => {
        selectedDataset = ds.name;
        void refresh();
      };
      card.appendChild(title);
      card.appendChild(
        el(
          "div",
          "bench-sub",
          `${ds.capture.frames} frames · ${ds.memories} memories · ${ds.capture.mode}` +
            (ds.annotated ? " · annotated" : ""),
        ),
      );
      if (!ds.annotated) {
        const annotateBtn = /** @type {HTMLButtonElement} */ (el("button", "bench-btn", "Annotate"));
        annotateBtn.disabled = !!state.job?.running;
        annotateBtn.onclick = () => run({ kind: "annotate", dataset: ds.name });
        card.appendChild(annotateBtn);
      }
      if (ds.name === selectedDataset) card.appendChild(memoriesGrid(ds.name));
      colData.appendChild(card);
    }
  }

  /** @param {string} dataset */
  function memoriesGrid(dataset) {
    const host = el("div", "bench-thumbs");
    api(`/api/dataset/${dataset}/memories`)
      .then((payload) => {
        if (destroyed) return;
        for (const m of payload.memories ?? []) {
          const url = `${BASE}/api/dataset/${dataset}/memory/${m.id}.jpg`;
          const cell = el("div", "bench-thumb");
          const img = new Image();
          img.loading = "lazy";
          img.src = url;
          const props = m.props?.length ? ` ${m.props.join(",")}` : "";
          const cap = el("i", "", `#${m.id}${props}`);
          cell.title = `#${m.id} (${m.x.toFixed(1)}, ${m.y.toFixed(1)})${props}`;
          cell.onclick = () => window.open(url, "_blank");
          cell.append(img, cap);
          host.appendChild(cell);
        }
      })
      .catch(() => host.appendChild(el("div", "bench-empty", "memories unavailable")));
    return host;
  }

  /** @param {ReturnType<typeof parseState>} state */
  function renderRun(state) {
    colRun.textContent = "";
    colRun.appendChild(el("h2", "", "Retrieval benchmark"));

    const dsRow = el("div", "bench-row");
    dsRow.appendChild(el("label", "", "dataset"));
    const dsSel = /** @type {HTMLSelectElement} */ (el("select"));
    for (const ds of state.datasets) dsSel.appendChild(new Option(ds.name, ds.name));
    if (selectedDataset) dsSel.value = selectedDataset;
    dsRow.appendChild(dsSel);

    const modelsRow = el("div", "bench-row");
    modelsRow.appendChild(el("label", "", "models"));
    const modelsInput = /** @type {HTMLInputElement} */ (el("input"));
    modelsInput.type = "text";
    modelsInput.value = "gemini-3.6-flash";
    modelsInput.size = 34;
    modelsRow.appendChild(modelsInput);

    const optRow = el("div", "bench-row");
    const cacheBox = /** @type {HTMLInputElement} */ (el("input"));
    cacheBox.type = "checkbox";
    cacheBox.checked = true;
    const cacheLabel = el("label", "", " warm context cache");
    cacheLabel.prepend(cacheBox);
    const styleSel = /** @type {HTMLSelectElement} */ (el("select"));
    for (const qs of ["context", "raw"]) styleSel.appendChild(new Option(`query: ${qs}`, qs));
    const fakeSel = /** @type {HTMLSelectElement} */ (el("select"));
    fakeSel.appendChild(new Option("live (Gemini)", ""));
    fakeSel.appendChild(new Option("fake: oracle", "oracle"));
    fakeSel.appendChild(new Option("fake: first", "first"));
    if (!state.geminiKey) fakeSel.value = "oracle";
    optRow.append(cacheLabel, styleSel, fakeSel);

    const goRow = el("div", "bench-row");
    const goBtn = /** @type {HTMLButtonElement} */ (el("button", "bench-btn primary", "Run benchmark"));
    goRow.appendChild(goBtn);
    const syncGo = () => {
      goBtn.disabled = !!state.job?.running || !state.datasets.length || (!fakeSel.value && !state.geminiKey);
    };
    fakeSel.onchange = syncGo;
    syncGo();
    goBtn.onclick = () =>
      run({
        kind: "benchmark",
        dataset: dsSel.value,
        models: modelsInput.value.trim(),
        cache: cacheBox.checked,
        query_style: styleSel.value,
        fake: fakeSel.value || undefined,
      });

    colRun.append(dsRow, modelsRow, optRow, goRow);

    if (state.job) {
      const jobCard = el("div", "bench-card");
      const status = state.job.running ? "running…" : `exit ${state.job.code}`;
      jobCard.appendChild(el("div", "bench-card-title", `${state.job.kind} — ${status}`));
      const log = el("div", "bench-log", (state.job.log ?? []).join("\n"));
      jobCard.appendChild(log);
      colRun.appendChild(jobCard);
      log.scrollTop = log.scrollHeight;
    }
  }

  /** @param {ReturnType<typeof parseState>} state */
  function renderResults(state) {
    colResults.textContent = "";
    colResults.appendChild(el("h2", "", "Results"));
    if (!state.results.length) colResults.appendChild(el("div", "bench-empty", "No runs yet."));
    for (const runInfo of state.results) {
      const { stamp, mode } = runLabel(runInfo.name);
      const card = el("div", "bench-card" + (runInfo.name === selectedRun ? " sel" : ""));
      const title = el("div", "bench-card-title");
      title.append(el("b", "", mode), el("span", "bench-sub", stamp));
      title.onclick = () => {
        selectedRun = runInfo.name === selectedRun ? "" : runInfo.name;
        void refresh();
      };
      card.appendChild(title);
      const meta = runInfo.meta ?? {};
      card.appendChild(el("div", "bench-sub", `${meta.frames ?? "?"} memories · ${meta.tasks ?? "?"} tasks · ${meta.query_style ?? ""}`));
      if (runInfo.name === selectedRun) runDetail(card, runInfo.name);
      colResults.appendChild(card);
    }
  }

  /**
   * @param {HTMLElement} card
   * @param {string} name
   */
  function runDetail(card, name) {
    api(`/api/results/${name}`)
      .then((payload) => {
        if (destroyed) return;
        for (const result of payload.results ?? []) {
          const rows = result.rows ?? [];
          const s = summarize(rows);
          const table = /** @type {HTMLTableElement} */ (el("table", "bench-table"));
          const headRow = table.insertRow();
          headRow.appendChild(el("th", "", `${result.model} (${result.cache_state})`));
          headRow.appendChild(el("th", "", "score"));
          const overall = table.insertRow();
          overall.insertCell().textContent = `overall · latency ${fmtLatency(s.meanLatency)}`;
          overall.insertCell().appendChild(el("span", `bench-score ${scoreClass(s.overall)}`, fmtScore(s.overall)));
          for (const [category, value] of s.byCategory) {
            const tr = table.insertRow();
            tr.insertCell().textContent = category;
            tr.insertCell().appendChild(el("span", `bench-score ${scoreClass(value)}`, fmtScore(value)));
          }
          card.appendChild(table);

          const tasks = /** @type {HTMLTableElement} */ (el("table", "bench-table"));
          const th = tasks.insertRow();
          for (const label of ["task", "score", "frame", "reason"]) th.appendChild(el("th", "", label));
          for (const row of rows) {
            const tr = tasks.insertRow();
            tr.insertCell().textContent = row.task;
            tr.insertCell().appendChild(el("span", `bench-score ${scoreClass(row.score)}`, row.score.toFixed(1)));
            tr.insertCell().textContent = row.frame == null ? "—" : `#${row.frame} ${row.room ?? ""}`;
            tr.insertCell().textContent = row.reason;
          }
          card.appendChild(tasks);
        }
      })
      .catch(() => card.appendChild(el("div", "bench-empty", "results unavailable")));
  }

  /** @param {Record<string, unknown>} body */
  async function run(body) {
    try {
      await api("/api/run", { method: "POST", body: JSON.stringify(body) });
    } catch {
      // 409 while busy or 400 on bad input — the next refresh shows the truth
    }
    await refresh();
  }

  async function refresh() {
    /** @type {ReturnType<typeof parseState>} */
    let state;
    try {
      state = parseState(await api("/api/state"));
    } catch {
      if (destroyed) return;
      failedPolls += 1;
      // One dropped poll must not kill an active job's log view: keep the
      // panel through brief hiccups, and keep the offline screen retrying.
      if (failedPolls >= 3 || !root.contains(grid)) offline();
      clearTimeout(pollTimer);
      pollTimer = window.setTimeout(() => void refresh(), failedPolls >= 3 ? 5000 : 1500);
      return;
    }
    if (destroyed) return;
    failedPolls = 0;
    if (!root.contains(grid)) {
      root.textContent = "";
      root.append(head, grid);
    }
    if (!selectedDataset && state.datasets.length) selectedDataset = state.datasets[0].name;
    renderChips(state);
    renderDatasets(state);
    renderRun(state);
    renderResults(state);
    if (state.job?.running) {
      clearTimeout(pollTimer);
      pollTimer = window.setTimeout(() => void refresh(), 1500);
    }
  }

  void refresh();

  return {
    destroy() {
      destroyed = true;
      clearTimeout(pollTimer);
      style.remove();
      root.remove();
    },
  };
}
