// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Python syntax colouring for the agent file the studio shows. Lifted from
// innate-os-4 feat/agent-studio (codePane.js); single-pass tokenizer, no parser.

const TOKEN =
  /("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|(#[^\n]*)|(@\w+)|\b(class|def)(\s+)(\w+)|\b(from|import|return|self|True|False|None|and|or|not|if|else|elif|for|in|while|pass|raise|try|except|finally|with|as|lambda|yield|is|property)\b|\b(\d+(?:\.\d+)?)\b/g;

/** @param {string} text */
function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Python source -> HTML with `tok-*` spans. @param {string} source */
export function highlightPython(source) {
  let out = "";
  let last = 0;
  for (const m of source.matchAll(TOKEN)) {
    const at = m.index ?? 0;
    out += escapeHtml(source.slice(last, at));
    const [, str, comment, decorator, defKw, defSpace, defName, keyword, number] = m;
    if (str !== undefined) out += `<span class="tok-str">${escapeHtml(str)}</span>`;
    else if (comment !== undefined) out += `<span class="tok-com">${escapeHtml(comment)}</span>`;
    else if (decorator !== undefined) out += `<span class="tok-dec">${escapeHtml(decorator)}</span>`;
    else if (defKw !== undefined) {
      out += `<span class="tok-kw">${defKw}</span>${defSpace}<span class="tok-name">${escapeHtml(defName)}</span>`;
    } else if (keyword !== undefined) out += `<span class="tok-kw">${keyword}</span>`;
    else if (number !== undefined) out += `<span class="tok-num">${number}</span>`;
    last = at + m[0].length;
  }
  out += escapeHtml(source.slice(last));
  // A trailing newline needs a visible line for the textarea and the <pre> to stay the same height.
  return out.endsWith("\n") ? out + " " : out;
}
