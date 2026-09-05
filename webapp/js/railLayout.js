// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The rail's roster and grouping — pure data and layout, no DOM, so plain-node
// tests can cover the boundary rules (tests/railLayout.test.js). shell.js
// renders what railRows() returns.

/** @typedef {{ key: string, label: string, icon: string }} Section */
/** @typedef {{ label: string | null, sections: Section[] }} Group */
/** @typedef {{ kind: "divider", label: string | null } | { kind: "section", section: Section }} RailRow */

// In sim mode (config.simControls) only these sections make sense — the rest
// (Datasets/Collect/Training/Profiling/Calibration) are robot-data workflows
// with no sim backing — the sim has no real stereo camera or ChArUco board to
// calibrate against — so they're hidden from the rail. Arm SDK stays: the sim
// runs the same IK node and answers the goto services, so the page exercises
// the real Manipulation SDK against the simulated arm. The router gates its
// routes on this set too (route keys are section keys).
export const SIM_SECTIONS = new Set(["teleop", "agent", "nav", "logging", "armsdk", "settings"]);

// The rail is grouped: standalone pages ride alone; multi-page workflows carry
// an eyebrow label (visible while the rail is hover-expanded; collapsed, a
// hairline tick marks the boundary). Collect → Datasets → Training → Profiling
// is the policy pipeline, grouped as AI Lab.
/** @type {Group[]} */
export const GROUPS = [
  {
    label: null,
    sections: [
      {
        key: "teleop",
        label: "Teleop",
        // The joystick motif: rim, cardinal ticks, knob.
        icon: '<circle cx="12" cy="12" r="8.5"/><line x1="12" y1="3.5" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="20.5"/><line x1="3.5" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="20.5" y2="12"/><circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/>',
      },
    ],
  },
  {
    label: null,
    sections: [
      {
        key: "agent",
        label: "Agent",
        // Sparkle motif: a four-point star for the autonomous brain.
        icon: '<path d="M12 3.5l1.7 6.8 6.8 1.7-6.8 1.7L12 20.5l-1.7-6.8L3.5 12l6.8-1.7z"/>',
      },
    ],
  },
  {
    label: null,
    sections: [
      {
        key: "nav",
        label: "Navigation",
        // Radar motif: sweep arcs and a contact dot, for the live sensor view.
        icon: '<path d="M12 12L18.4 5.6"/><path d="M15.2 8.8a4.5 4.5 0 1 0 1.3 3.2"/><path d="M18.4 5.6A9 9 0 1 0 21 12"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/>',
      },
    ],
  },
  {
    label: null,
    sections: [
      {
        key: "logging",
        label: "Logging",
        icon: '<polyline points="4.5,7 10,12 4.5,17"/><line x1="12.5" y1="17" x2="19.5" y2="17"/>',
      },
    ],
  },
  {
    label: "AI Lab",
    sections: [
      {
        key: "collect",
        label: "Collect",
        icon: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="3.2" fill="currentColor" stroke="none"/>',
      },
      {
        key: "datasets",
        label: "Datasets",
        icon: '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3V6"/><path d="M4.5 12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3"/>',
      },
      {
        key: "training",
        label: "Training",
        icon: '<polyline points="4,17.5 9.5,11.5 13.5,15 20,7"/><polyline points="15.5,7 20,7 20,11.5"/>',
      },
      {
        key: "profiling",
        label: "Profiling",
        // Stopwatch motif: dial, crown, and a sweeping hand.
        icon: '<circle cx="12" cy="13" r="7.5"/><line x1="12" y1="13" x2="15" y2="10"/><line x1="12" y1="2.5" x2="12" y2="5" /><line x1="9.5" y1="2.5" x2="14.5" y2="2.5"/>',
      },
    ],
  },
  {
    label: "Maintenance",
    sections: [
      {
        key: "armsdk",
        label: "Arm SDK",
        // Articulated-arm motif: base, two links with a joint, and a claw.
        icon: '<circle cx="6" cy="19" r="2"/><path d="M7.5 17.5L10.5 10"/><circle cx="11" cy="8.8" r="1.4"/><path d="M12.3 8L17 5.5"/><path d="M17 5.5l2.5 1M17 5.5l.5 2.7"/>',
      },
      {
        key: "calibration",
        label: "Calibration",
        // Camera motif: body with a viewfinder bump, and a lens. The body spans
        // x 4–19, y 6–19, so its centre lands on the half unit.
        icon: '<path d="M4 8a2 2 0 0 1 2-2h2l1.2-1.8a1 1 0 0 1 .8-.4h3a1 1 0 0 1 .8.4L15 6h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8z"/><circle cx="11.5" cy="12.5" r="3.4"/>',
      },
    ],
  },
];

// Pinned at the rail's very bottom.
/** @type {Section[]} */
export const FOOTER_SECTIONS = [
  {
    key: "settings",
    label: "Settings",
    // Sliders motif: two tracks, each with a knob.
    icon: '<line x1="4" y1="8.5" x2="20" y2="8.5"/><circle cx="10" cy="8.5" r="2.3" fill="currentColor" stroke="none"/><line x1="4" y1="15.5" x2="20" y2="15.5"/><circle cx="15" cy="15.5" r="2.3" fill="currentColor" stroke="none"/>',
  },
];

// Flattened in rail order (footer last) — the source of number-key shortcuts
// (1..N) and the tab-title lookup. Indices stay stable when the sim filter
// hides sections.
/** @type {Section[]} */
export const SECTIONS = [...GROUPS.flatMap((group) => group.sections), ...FOOTER_SECTIONS];

/**
 * Flatten `groups` into rail rows. `visible` limits sections (the sim filter);
 * a group left empty vanishes with its label. A divider separates two groups
 * only when at least one of them is labeled — adjacent standalone pages read
 * as one cluster.
 * @param {Group[]} groups
 * @param {Set<string> | null} visible
 * @returns {RailRow[]}
 */
export function railRows(groups, visible) {
  const kept = groups
    .map((group) => ({
      label: group.label,
      sections: group.sections.filter((s) => !visible || visible.has(s.key)),
    }))
    .filter((group) => group.sections.length > 0);
  /** @type {RailRow[]} */
  const rows = [];
  kept.forEach((group, i) => {
    if (i > 0 && (group.label || kept[i - 1].label)) rows.push({ kind: "divider", label: group.label });
    for (const section of group.sections) rows.push({ kind: "section", section });
  });
  return rows;
}
