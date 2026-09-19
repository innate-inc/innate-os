// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// "Publish to Hugging Face" dialog for a training dataset. The robot converts the skill's
// episodes to a LeRobotDataset and uploads them (proxy/hub_publish.py); this dialog only
// starts that job and mirrors its progress by polling /hub/publish, so closing it never
// interrupts an upload and reopening it picks the progress back up.

const POLL_MS = 1000;

/**
 * @typedef {{ kind: string, dir: string, repo_id: string, stage: string, episode: number,
 *   total: number, progress: number, message: string, error: string, url: string, running: boolean }} HubJob
 * @typedef {{ readonly: boolean, token: boolean, env_ready: boolean, job: HubJob | null,
 *   published: { repo_id: string, episodes: number, pushed: boolean } | null }} HubStatus
 */

/**
 * @param {HTMLElement} host  Any element of the page; the dialog covers the viewport.
 * @param {Skill} skill
 * @returns {{ close: () => void }}
 */
export function openPublishModal(host, skill) {
  const directory = skill.directory || "";
  let closed = false;
  // The robot remembers its last job; show this dataset's outcome once, then get out of the way.
  let outcomeDismissed = false;
  /** @type {number | undefined} */
  let pollTimer;

  const backdrop = el("div", "modal-backdrop modal-backdrop--page");
  const panel = el("div", "modal hub-modal");
  panel.addEventListener("click", (event) => event.stopPropagation());
  const head = el("div", "modal-head");
  const closeBtn = /** @type {HTMLButtonElement} */ (el("button", "modal-close", "✕"));
  closeBtn.type = "button";
  closeBtn.title = "Close";
  head.append(el("h2", "modal-title", "Publish to Hugging Face"), closeBtn);
  const body = el("div", "modal-body");
  panel.append(head, body);
  backdrop.appendChild(panel);
  host.ownerDocument.body.appendChild(backdrop);

  function close() {
    if (closed) return;
    closed = true;
    window.clearTimeout(pollTimer);
    host.ownerDocument.removeEventListener("keydown", onKey);
    backdrop.remove();
  }
  const onKey = (/** @type {KeyboardEvent} */ event) => {
    if (event.key === "Escape") close();
  };
  backdrop.addEventListener("click", close);
  closeBtn.addEventListener("click", close);
  host.ownerDocument.addEventListener("keydown", onKey);

  /** @returns {Promise<HubStatus | null>} */
  async function fetchStatus() {
    try {
      const res = await fetch(`/hub/publish?dir=${encodeURIComponent(directory)}`, { cache: "no-store" });
      const status = await res.json();
      return typeof status?.env_ready === "boolean" ? status : null;
    } catch {
      return null; // a robot older than this page answers with the app shell, which is not JSON
    }
  }

  /** @param {string} path @param {object} [payload] */
  async function post(path, payload) {
    try {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
        body: JSON.stringify(payload || {}),
      });
      const answer = await res.json();
      return { ok: Boolean(answer.ok), message: String(answer.message || "") };
    } catch (err) {
      return { ok: false, message: `Could not reach the robot: ${err}` };
    }
  }

  async function refresh() {
    if (closed) return;
    const status = await fetchStatus();
    if (closed) return;
    if (!status) return showNote("This robot's software does not support publishing yet. Update it and reload.");
    if (status.job?.running) return showProgress(status.job);
    const last = status.job;
    if (last && !outcomeDismissed && last.kind === "publish" && last.dir === directory) {
      return last.stage === "error" ? showFailure(last) : showDone(last);
    }
    if (status.readonly) return showNote("This demo cannot publish datasets.");
    if (!status.token) return showNeedsToken();
    if (!status.env_ready) return showNeedsSetup(status.job);
    showForm(status);
  }

  function pollSoon() {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(async () => {
      const status = await fetchStatus();
      if (closed) return;
      const job = status?.job;
      if (job?.running) return showProgress(job);
      if (job && job.stage === "error") return showFailure(job);
      if (job && job.kind === "publish") return showDone(job);
      refresh(); // a finished setup job: move on to the form
    }, POLL_MS);
  }

  /** @param {string} text */
  function showNote(text) {
    body.replaceChildren(el("p", "modal-hint", text));
  }

  function showNeedsToken() {
    const link = /** @type {HTMLAnchorElement} */ (el("a", "modal-start hub-action", "Open Settings"));
    link.href = "/settings";
    link.addEventListener("click", close);
    body.replaceChildren(
      el("p", "modal-hint", "Publishing needs a Hugging Face access token with write permission. Create one at huggingface.co/settings/tokens and save it under Settings, in the keys section."),
      link,
    );
  }

  /** @param {HubJob | null} lastJob */
  function showNeedsSetup(lastJob) {
    const button = /** @type {HTMLButtonElement} */ (el("button", "modal-start", "Install"));
    button.type = "button";
    const note = el("p", "modal-warn", lastJob?.kind === "setup" && lastJob.stage === "error" ? lastJob.error : "");
    button.addEventListener("click", async () => {
      button.disabled = true;
      const answer = await post("/hub/setup");
      if (answer.ok) return refresh();
      note.textContent = answer.message;
      button.disabled = false;
    });
    body.replaceChildren(
      el("p", "modal-hint", "Converting a dataset for Hugging Face uses the LeRobot library, which this robot has not installed yet. It is a one-time download of about 1.5 GB and takes a few minutes."),
      note,
      button,
    );
  }

  /** @param {HubStatus} status */
  function showForm(status) {
    const previous = status.published?.repo_id || "";
    const [previousOwner, previousName] = previous.includes("/") ? previous.split("/", 2) : ["", ""];

    const owner = /** @type {HTMLSelectElement} */ (el("select", "modal-select"));
    owner.append(new Option("Loading your account…", ""));
    owner.disabled = true;
    const name = /** @type {HTMLInputElement} */ (el("input", "modal-input"));
    name.type = "text";
    name.autocomplete = "off";
    name.spellcheck = false;
    name.value = previousName || `mars-${slug(skill.name)}`;
    const repoRow = el("div", "hub-repo");
    repoRow.append(owner, el("span", "hub-repo-slash", "/"), name);

    const isPrivate = checkbox("Private: only you and your organization can see it", true);
    const failures = checkbox("Include episodes labelled failed", false);
    const note = el("p", "modal-warn");
    const button = /** @type {HTMLButtonElement} */ (el("button", "modal-start", "Publish"));
    button.type = "button";
    button.disabled = true;

    const summary = status.published?.pushed
      ? `Published before as ${status.published.repo_id} (${status.published.episodes} episodes). Only new episodes are converted; the whole dataset is uploaded again.`
      : status.published
        ? `Converted for ${status.published.repo_id} before, but the upload did not finish. Publishing uploads it.`
        : `${skill.episode_count} episode${skill.episode_count === 1 ? "" : "s"} recorded on this robot. Converting and uploading runs in the background; you can close this window.`;

    body.replaceChildren(
      field("Repository", repoRow),
      isPrivate.row,
      failures.row,
      el("p", "modal-hint", summary),
      note,
      button,
    );

    fetch("/hub/whoami", { cache: "no-store" })
      .then((res) => res.json())
      .then((account) => {
        if (closed) return;
        if (!account.ok) {
          owner.replaceChildren(new Option("Account unavailable", ""));
          note.textContent = account.message || "Could not check the Hugging Face token.";
          return;
        }
        const owners = [account.name, ...(Array.isArray(account.orgs) ? account.orgs : [])].filter(Boolean);
        owner.replaceChildren(...owners.map((o) => new Option(o, o)));
        if (owners.includes(previousOwner)) owner.value = previousOwner;
        owner.disabled = false;
        button.disabled = false;
      })
      .catch(() => {
        if (!closed) note.textContent = "Could not reach the robot to check the token.";
      });

    button.addEventListener("click", async () => {
      const repoName = name.value.trim();
      if (!/^[A-Za-z0-9][\w.-]*$/.test(repoName)) {
        note.textContent = "Use letters, digits, dashes, dots or underscores for the name.";
        name.focus();
        return;
      }
      button.disabled = true;
      note.textContent = "";
      const answer = await post("/hub/publish", {
        dir: directory,
        repo_id: `${owner.value}/${repoName}`,
        private: isPrivate.input.checked,
        include_failures: failures.input.checked,
      });
      if (answer.ok) return refresh();
      note.textContent = answer.message;
      button.disabled = false;
    });
  }

  /** @param {HubJob} job */
  function showProgress(job) {
    const mine = job.kind === "setup" || job.dir === directory;
    const bar = el("div", "tbar hub-bar");
    const fill = el("div", "tbar-fill");
    const indeterminate = job.kind === "setup" || job.stage === "uploading" || job.stage === "starting";
    bar.classList.toggle("tbar-indeterminate", indeterminate);
    if (!indeterminate) fill.style.width = `${Math.round(job.progress * 100)}%`;
    bar.appendChild(fill);
    const heading = job.kind === "setup"
      ? "Installing the LeRobot environment"
      : mine
        ? `Publishing to ${job.repo_id}`
        : `Another dataset is being published (${job.repo_id})`;
    body.replaceChildren(
      el("p", "hub-heading", heading),
      bar,
      el("p", "modal-status", job.message),
      el("p", "modal-hint", "This keeps running if you close the window."),
    );
    pollSoon();
  }

  /** @param {HubJob} job */
  function showDone(job) {
    const children = [el("p", "hub-heading", job.message || "Published")];
    if (job.url) {
      const link = /** @type {HTMLAnchorElement} */ (el("a", "modal-start hub-action", "Open on Hugging Face"));
      link.href = job.url;
      link.target = "_blank";
      link.rel = "noopener";
      children.push(el("p", "modal-hint", job.repo_id), link);
    }
    const again = /** @type {HTMLButtonElement} */ (el("button", "modal-back", "Publish again"));
    again.type = "button";
    again.addEventListener("click", () => {
      outcomeDismissed = true;
      refresh();
    });
    children.push(again);
    body.replaceChildren(...children);
  }

  /** @param {HubJob} job */
  function showFailure(job) {
    const retry = /** @type {HTMLButtonElement} */ (el("button", "modal-start", "Back"));
    retry.type = "button";
    retry.addEventListener("click", () => {
      outcomeDismissed = true;
      refresh();
    });
    body.replaceChildren(
      el("p", "hub-heading", job.kind === "setup" ? "The installation failed" : "Publishing failed"),
      el("p", "modal-warn hub-error", job.error || "The robot reported no reason."),
      retry,
    );
  }

  showNote("Checking the robot…");
  refresh();
  return { close };
}

/**
 * @param {string} tag @param {string} className @param {string} [text]
 * @returns {HTMLElement}
 */
function el(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** @param {string} label @param {HTMLElement} control */
function field(label, control) {
  const wrap = el("div", "modal-field");
  wrap.append(el("span", "modal-field-label", label), control);
  return wrap;
}

/** @param {string} label @param {boolean} checked */
function checkbox(label, checked) {
  const row = el("label", "modal-check");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  row.append(input, el("span", "", label));
  return { row, input };
}

/** @param {string} name */
function slug(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "dataset";
}
