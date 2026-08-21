// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

/** Group expand/collapse duration. Shared so the search jump can wait it out before scrolling. */
export const GROUP_EXPAND_MS = 220;

/** Page-scoped settings CSS, injected on mount and removed on destroy. */
export const SETTINGS_STYLE = `
.settings-page { position: absolute; inset: 0; display: flex; flex-direction: column; background: var(--bg, #0b0b0d); }
.settings-body { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.settings-scroll { flex: 1; overflow-y: auto; min-height: 0; }
.settings-wrap { width: 100%; max-width: 720px; margin: 0 auto; padding: 28px 32px 48px; box-sizing: border-box; }
.settings-wrap .page-title { margin: 0 0 6px; font-size: 22px; font-weight: 600; letter-spacing: -.02em; }
.settings-note { color: var(--muted, #8a90a0); font-size: 13px; margin: 0 0 22px; line-height: 1.5; }

.set-search { width: 100%; box-sizing: border-box; margin: 0 0 18px; padding: 9px 12px;
  border: 1px solid var(--hairline, #2a2f3a); border-radius: 8px;
  background: rgba(255,255,255,.03); color: inherit; font: inherit; font-size: 13px; }
.set-search:focus { outline: none; border-color: rgba(117,105,253,.55); background: rgba(0,0,0,.2); }
.set-search::placeholder { color: var(--muted, #8a90a0); }
.set-card, .set-accordion, .set-card-search, .set-card-volume {
  margin: 0; border: 1px solid var(--hairline, #2a2f3a); border-radius: 10px;
  background: transparent; overflow: hidden;
}
.set-accordion > .set-group + .set-group { border-top: 1px solid var(--hairline, #2a2f3a); }
.set-group-h { display: flex; align-items: center; gap: 14px; width: 100%; box-sizing: border-box;
  margin: 0; padding: 14px 16px; border: none; border-radius: 0;
  background: none; color: inherit; font: inherit; text-align: left; cursor: pointer;
  transition: background .12s ease; }
.set-group-h:hover { background: rgba(255,255,255,.035); }
.set-group-icon { flex: none; display: flex; align-items: center; justify-content: center;
  width: 32px; height: 32px; border-radius: 8px; background: rgba(255,255,255,.06);
  color: var(--text, #e7e7ea); line-height: 0; }
.set-group-icon::before { content: ""; display: block; width: 18px; height: 18px;
  background: currentColor;
  -webkit-mask-image: var(--icon-url); mask-image: var(--icon-url);
  -webkit-mask-position: center; mask-position: center;
  -webkit-mask-repeat: no-repeat; mask-repeat: no-repeat;
  -webkit-mask-size: contain; mask-size: contain; }
.set-group-text { flex: 1; min-width: 0; }
.set-group-label { display: block; font-size: 14px; font-weight: 500; line-height: 1.3;
  color: var(--text, #e7e7ea); }
.set-group-summary { display: block; margin-top: 2px; font-size: 12px; line-height: 1.4;
  color: var(--muted, #8a90a0); }
.set-group-dot { flex: none; width: 6px; height: 6px; border-radius: 50%;
  background: var(--primary, #7569FD); display: none; }
.set-group-dot.show { display: block; }
.set-group-chev { flex: none; color: var(--muted, #8a90a0); opacity: .5;
  transition: transform ${GROUP_EXPAND_MS}ms ease; }
.set-group.open .set-group-chev { transform: rotate(90deg); }
/* 0fr -> 1fr animates to the body's natural height; the inner div owns the clipping. */
.set-group-body { display: grid; grid-template-rows: 0fr; transition: grid-template-rows ${GROUP_EXPAND_MS}ms ease; }
.set-group.open .set-group-body { grid-template-rows: 1fr; }
.set-group-body-inner { overflow: hidden; min-height: 0; }
/* Padding lives here, not on the clipped element — padding on a 0fr row still paints. */
.set-group-content { padding: 4px 16px 20px; }
.set-group-content > .set-page-section:last-child { margin-bottom: 0; }
.set-group-note { margin: 0 2px 16px; color: var(--muted, #8a90a0); font-size: 12px; line-height: 1.5; }
.set-search-result { display: block; width: 100%; box-sizing: border-box; margin: 0; padding: 12px 16px;
  border: none; border-bottom: 1px solid var(--hairline, #2a2f3a); border-radius: 0;
  background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.set-search-result:last-child { border-bottom: none; }
.set-search-result:hover { background: rgba(255,255,255,.035); }
.set-search-label { display: block; color: var(--text, #e7e7ea); font-size: 14px;
  font-weight: 600; line-height: 1.3; }
.set-search-doc { display: block; margin-top: 4px; color: var(--muted, #8a90a0);
  font-size: 12px; font-weight: 400; line-height: 1.45; }
.set-search-context { display: block; margin-top: 5px; color: var(--muted, #8a90a0);
  font-size: 11px; font-weight: 500; line-height: 1.3; opacity: .78; }
.set-search-source { display: block; margin-top: 7px; padding: 7px 9px;
  border-radius: 5px; background: rgba(117,105,253,.07);
  color: var(--muted, #8a90a0); font-size: 11px; line-height: 1.45; }
.set-search-source-kind { display: block; margin-bottom: 2px; color: var(--text, #e7e7ea);
  font-size: 9px; font-weight: 600; letter-spacing: .045em; text-transform: uppercase; opacity: .68; }
.set-search-source-text { display: block; }
.set-search-mark { padding: 0 1px; border-radius: 2px; background: rgba(117,105,253,.32);
  color: inherit; font: inherit; }
.set-search-empty { margin: 0; padding: 18px 16px; color: var(--muted, #8a90a0); font-size: 13px; }

.set-page-section { margin: 0 0 28px; }
.set-section-title { margin: 0 0 9px; padding: 0 2px; font-size: 13px; font-weight: 600;
  color: var(--text, #e7e7ea); letter-spacing: -.01em; }
.set-section-note { margin: 0 2px 10px; color: var(--muted, #8a90a0); font-size: 12px; line-height: 1.45; }
.set-subblock + .set-subblock { border-top: 1px solid var(--hairline, #2a2f3a); }
.set-subh { padding: 18px 20px 2px; font-size: 12px; font-weight: 600; letter-spacing: .02em;
  text-transform: none; color: var(--muted, #8a90a0); }

/* Controls sit in a right-aligned track. Number fields stay compact; selects can be wider.
   min-width + flex:none stops unit length (m/s vs rad/s) from changing field widths. */
.set-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px 32px;
  align-items: center; padding: 16px 20px; border: none; border-radius: 0;
  border-bottom: 1px solid var(--hairline, #2a2f3a); cursor: pointer;
  transition: background .12s ease, box-shadow .12s ease; }
.set-card > .set-row:last-child,
.set-subblock > .set-row:last-child { border-bottom: none; }
.set-row:hover { background: rgba(255,255,255,.02); }
.set-row.saved { background: rgba(224,145,58,.07); box-shadow: inset 2px 0 0 rgba(224,145,58,.7); }
.set-row.dirty { background: rgba(117,105,253,.08); box-shadow: inset 2px 0 0 rgba(117,105,253,.75); }
.set-row.invalid { background: rgba(233,86,86,.08); box-shadow: inset 2px 0 0 rgba(233,86,86,.8); }
.set-row.invalid .set-num-wrap { border-color: rgba(233,86,86,.8); }
@keyframes set-search-hit { 0%, 35% { background: rgba(117,105,253,.16); } 100% { background: transparent; } }
.set-row.search-hit { animation: set-search-hit 1.4s ease-out; }
.set-validation-slot { font-size: 11px; font-weight: 500; color: #e95656; text-align: right; line-height: 1.2;
  white-space: nowrap; align-self: flex-end; min-height: 1.2em; visibility: hidden; }
.set-validation-slot:not(:empty) { visibility: visible; }
.set-dirty.set-bad { color: #e95656; }
.set-info { min-width: 0; padding-right: 8px; }
.set-label { display: block; font-size: 13px; font-weight: 500; line-height: 1.35; }
.set-doc { display: block; color: var(--muted, #8a90a0); font-size: 12px; line-height: 1.45; margin-top: 3px;
  max-width: 42ch; }
.set-doc-link { color: var(--primary, #7569FD); text-decoration: none; }
.set-doc-link:hover { text-decoration: underline; }

.set-ctl { display: flex; align-items: center; justify-content: flex-end;
  min-width: 0; box-sizing: border-box; }
.set-ctl-main { display: flex; flex-direction: column; align-items: stretch; gap: 5px;
  width: 116px; min-width: 116px; flex: none; }
.set-ctl-main.is-wide { width: 200px; min-width: 200px; }
.set-ctl-main.is-toggle { width: auto; min-width: 0; align-items: flex-end; }
.set-ctl-row { display: flex; align-items: center; justify-content: flex-end; gap: 8px; width: 100%; }
.set-num-wrap { display: flex; align-items: center; gap: 5px; box-sizing: border-box;
  width: 100%; padding: 5px 8px; border-radius: 6px; cursor: text;
  border: 1px solid var(--hairline, #2a2f3a); background: rgba(255,255,255,.03); }
.set-num-wrap:focus-within { border-color: rgba(117,105,253,.55); background: rgba(0,0,0,.2); }
.set-num-wrap input.set-num { flex: 1 1 auto; min-width: 0; width: 4ch; padding: 0;
  text-align: left; border: none; background: transparent; color: inherit; font: inherit; outline: none; }
.set-unit { flex: none; color: var(--muted, #8a90a0); font-size: 12px; white-space: nowrap; }

.set-ctl input[type=checkbox] { appearance: none; -webkit-appearance: none; position: relative;
  width: 36px; height: 20px; margin: 0; border-radius: 999px; cursor: pointer;
  background: rgba(255,255,255,.12); border: none; transition: background .15s ease; flex: none; }
.set-ctl input[type=checkbox]::after { content: ""; position: absolute; top: 2px; left: 2px;
  width: 16px; height: 16px; border-radius: 50%; background: #fff;
  transition: transform .15s ease; box-shadow: 0 1px 2px rgba(0,0,0,.35); }
.set-ctl input[type=checkbox]:checked { background: var(--primary, #7569FD); }
.set-ctl input[type=checkbox]:checked::after { transform: translateX(16px); }

/* Per-row restore. Slot always reserved; visible only when value ≠ default. */
.set-restore { display: block; visibility: hidden; pointer-events: none;
  align-self: flex-end; margin: 0; padding: 0;
  border: none; background: none; font: inherit; font-size: 11px; font-weight: 500;
  color: var(--muted, #8a90a0); cursor: pointer; line-height: 1.2; text-align: right;
  white-space: nowrap; min-height: 1.2em; }
.set-row.has-override .set-restore { visibility: visible; pointer-events: auto; }
.set-restore:hover { color: var(--primary, #7569FD); }
.set-restore:focus-visible { outline: none; color: var(--primary, #7569FD);
  text-decoration: underline; }

.set-bar { flex: none; display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px;
  padding: 12px 28px; background: rgba(17,17,20,.92); border-top: 1px solid var(--hairline, #2a2f3a);
  backdrop-filter: blur(8px); }
.set-save { padding: 8px 16px; border-radius: 6px; border: none; color: #fff; font: inherit; font-weight: 600; cursor: pointer;
  background: var(--primary, #7569FD); transition: filter .15s ease, opacity .2s ease; }
.set-save:not(:disabled):hover { filter: brightness(1.1); }
.set-save:disabled { opacity: .4; cursor: default; }
.set-reset-all { margin-left: auto; padding: 7px 12px; border-radius: 6px; border: 1px solid var(--hairline, #2a2f3a);
  background: none; color: var(--text, #e7e7ea); font: inherit; font-size: 13px; cursor: pointer; }
.set-reset-all:disabled { opacity: .4; cursor: default; }
.set-restart { padding: 7px 12px; border-radius: 6px; border: 1px solid var(--hairline, #2a2f3a);
  background: none; color: var(--text, #e7e7ea); font: inherit; font-size: 13px; cursor: pointer;
  transition: border-color .15s ease, color .15s ease; }
.set-restart:not(:disabled):hover { border-color: var(--primary, #7569FD); color: var(--primary, #7569FD); }
.set-restart:disabled { opacity: .4; cursor: default; }
.set-shutdown:not(:disabled):hover { border-color: #e95656; color: #e95656; }
.set-dirty { font-size: 13px; color: var(--primary, #7569FD); }
.set-status { font-size: 13px; }
.set-status.ok { color: #3ecf8e; }
.set-status.err { color: #ff6b6b; }
.set-status.muted { color: var(--muted, #8a90a0); }
.set-ctl :is(input, select).set-text { box-sizing: border-box; width: 100%; padding: 6px 10px; border-radius: 6px;
  border: 1px solid var(--hairline, #2a2f3a); background-color: rgba(255,255,255,.03); color: inherit;
  font: inherit; font-size: 13px; }
.set-ctl :is(input, select).set-text:focus { outline: none; border-color: rgba(117,105,253,.55); background-color: rgba(0,0,0,.2); }
.set-ctl select.set-text { padding: 6px 30px 6px 10px; cursor: pointer; }
/* Custom range; content-box keeps the 10px center inside the 4px thumb border. */
.set-slider { -webkit-appearance: none; appearance: none;
  width: 100%; height: 20px; margin: 0;
  background: transparent; cursor: pointer; }
.set-slider::-webkit-slider-runnable-track { height: 6px; border-radius: 999px;
  background: linear-gradient(to right, var(--primary, #7569FD) 0 var(--set-slider-fill),
    rgba(255,255,255,.18) var(--set-slider-fill) 100%); }
.set-slider::-moz-range-track { height: 6px; border-radius: 999px; background: rgba(255,255,255,.18); }
.set-slider::-moz-range-progress { height: 6px; border-radius: 999px; background: var(--primary, #7569FD); }
.set-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 10px; height: 10px;
  margin-top: -6px; box-sizing: content-box; border: 4px solid var(--primary, #7569FD);
  border-radius: 50%; background: var(--text, #e7e7ea); }
.set-slider::-moz-range-thumb { width: 10px; height: 10px; box-sizing: content-box;
  border: 4px solid var(--primary, #7569FD); border-radius: 50%; background: var(--text, #e7e7ea); }
.set-slider:active:not(:disabled)::-webkit-slider-thumb { background: var(--primary, #7569FD); }
.set-slider:active:not(:disabled)::-moz-range-thumb { background: var(--primary, #7569FD); }
/* Ring for Tab / row-click only — .is-pointer is set while dragging. */
.set-slider:focus { outline: none; }
.set-slider:focus-visible:not(.is-pointer) { outline: 1px solid rgba(117,105,253,.75);
  outline-offset: 3px; border-radius: 2px; }
.set-slider:disabled { opacity: .4; cursor: default; }
.set-name-wrap { display: flex; gap: 8px; width: 100%; }
.set-name-wrap .set-text { flex: 1 1 auto; min-width: 0; }
.set-slider-wrap { display: flex; flex-direction: column; gap: 5px; width: 100%; }
.set-slider-read { color: var(--text, #e7e7ea); font-size: 13px; font-weight: 600;
  font-variant-numeric: tabular-nums; line-height: 1.2; text-align: center; }
.set-slider-wrap:has(.set-slider:disabled) .set-slider-read { opacity: .4; }
.set-card-volume .set-row { border-bottom: none; }
.set-card-volume-status { align-self: flex-end; font-size: 11px; font-weight: 500;
  line-height: 1.2; text-align: right; min-height: 1.2em; }

@media (max-width: 720px) {
  .settings-wrap { padding: 20px 18px 36px; }
  .set-row { gap: 10px 20px; padding: 14px 16px; }
  .set-bar { padding: 12px 18px; }
  .set-group-h { padding: 13px 14px; gap: 12px; }
}

@media (max-width: 560px) {
  .settings-wrap { padding: 16px 14px 28px; }
  .settings-wrap .page-title { font-size: 20px; }
  .set-card, .set-accordion, .set-card-search, .set-card-volume { border-radius: 8px; }
  .set-subh { padding: 14px 14px 2px; }
  .set-row { grid-template-columns: 1fr; gap: 10px; padding: 14px; }
  .set-doc { max-width: none; }
  .set-ctl { width: 100%; justify-content: flex-start; }
  .set-ctl-main.is-wide { width: min(200px, 100%); min-width: 0; }
  .set-ctl-main.is-slider { width: 100%; }
  .set-card-volume .set-ctl-main { width: 100%; min-width: 0; }
  /* Toggles stay label | switch — stacking them under the copy looks wrong. */
  .set-row-toggle { grid-template-columns: minmax(0, 1fr) auto; }
  .set-row-toggle .set-ctl { width: auto; justify-content: flex-end; }
  .set-group-summary { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .set-group-content { padding: 4px 14px 18px; }
  .set-bar { padding: 10px 14px; gap: 8px 10px; }
  .set-save { flex: 1 1 auto; text-align: center; }
  .set-dirty, .set-status { flex: 1 1 100%; order: 3; }
  .set-reset-all { margin-left: 0; order: 4; }
  .set-restart { order: 5; }
}
`;
