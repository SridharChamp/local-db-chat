"use strict";
(() => {
  const MAX_HISTORY = 4;      // follow-up context sent to the server
  const MAX_MESSAGES = 120;   // DOM nodes kept on screen; older ones are dropped
  const $ = (id) => document.getElementById(id);
  const log = $("log"), form = $("form"), q = $("q"), send = $("send");
  let history = [];
  let busy = false;

  // Access token (only needed when the server is shared on a network).
  let token = "";
  try {
    const m = location.hash.match(/token=([\w-]+)/);
    if (m) { sessionStorage.setItem("ldbc-token", m[1]); window.history.replaceState(null, "", location.pathname); }
    token = sessionStorage.getItem("ldbc-token") || "";
  } catch (_) { /* storage may be blocked; requests then go without a token */ }

  async function api(path, body) {
    const opts = { method: body ? "POST" : "GET", headers: {} };
    if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    if (token) opts.headers["X-LDBC-Token"] = token;
    let res;
    try { res = await fetch(path, opts); }
    catch (_) { throw new Error("Cannot reach the local server. Is 'ldbc serve' still running?"); }
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (!res.ok) {
      const detail = data && (data.error || (Array.isArray(data.detail) ? data.detail.map(d => d.msg).join("; ") : data.detail));
      throw new Error(detail || `Request failed (${res.status}).`);
    }
    return data;
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);  // never innerHTML: data is untrusted
    return n;
  }

  function add(node) {
    $("empty")?.remove();
    log.appendChild(node);
    while (log.children.length > MAX_MESSAGES) log.removeChild(log.firstChild);
    node.scrollIntoView({ behavior: "smooth", block: "end" });
    return node;
  }

  function message(kind, text) {
    const wrap = el("div", `msg ${kind}`);
    const bubble = el("div", "bubble");
    if (text !== undefined) bubble.appendChild(el("div", kind === "user" ? "" : "answer", text));
    wrap.appendChild(bubble);
    return { wrap, bubble };
  }

  function chip(text, cls) { return el("span", "chip" + (cls ? " " + cls : ""), text); }

  function isNumber(v) { return typeof v === "number" || (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v)); }

  function renderTable(columns, rows) {
    const wrap = el("div", "table-wrap");
    const table = el("table");
    const thead = el("thead"), tr = el("tr");
    columns.forEach(c => tr.appendChild(el("th", "", c)));
    thead.appendChild(tr); table.appendChild(thead);
    const tbody = el("tbody");
    const frag = document.createDocumentFragment();
    rows.forEach(row => {
      const r = el("tr");
      row.forEach(v => {
        const td = el("td", v === null ? "null" : isNumber(v) ? "num" : "", v === null ? "NULL" : v);
        if (v !== null) td.title = String(v);
        r.appendChild(td);
      });
      frag.appendChild(r);
    });
    tbody.appendChild(frag); table.appendChild(tbody); wrap.appendChild(table);
    return wrap;
  }

  function csv(columns, rows) {
    const cell = (v) => {
      if (v === null || v === undefined) return "";
      let s = String(v);
      if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;   // stop spreadsheet formula injection
      return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    return [columns, ...rows].map(r => r.map(cell).join(",")).join("\r\n");
  }

  function download(name, text) {
    const url = URL.createObjectURL(new Blob(["﻿" + text], { type: "text/csv;charset=utf-8" }));
    const a = el("a"); a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);   // free the blob
  }

  function renderAnswer(a) {
    const kind = a.error ? "error" : a.cannot_answer ? "cannot" : "bot";
    const { wrap, bubble } = message(kind, a.error || a.answer);
    if (a.error && !a.sql) return wrap;

    if (a.ok && a.columns.length && !(a.row_count === 1 && a.columns.length === 1)) {
      bubble.appendChild(renderTable(a.columns, a.rows));
    }
    const meta = el("div", "meta");
    if (a.ok) meta.appendChild(chip(`${a.row_count}${a.truncated ? "+" : ""} row${a.row_count === 1 ? "" : "s"}`));
    if (a.tables && a.tables.length) meta.appendChild(chip(a.tables.join(", ")));
    if (a.verified) meta.appendChild(chip("verified answer", "good"));
    else if (a.cached) meta.appendChild(chip("cached"));
    if (a.attempts > 1) meta.appendChild(chip(`${a.attempts} tries`));
    if (a.timings_ms && a.timings_ms.total !== undefined) meta.appendChild(chip(`${(a.timings_ms.total / 1000).toFixed(1)}s`));
    bubble.appendChild(meta);

    if (a.ok && a.problems && a.problems.length) {
      const note = el("p", "note", "Earlier attempts were rejected: " + a.problems.join(" · ") +
        " Check that this answer is what you asked for.");
      bubble.appendChild(note);
    }
    if (a.sql) {
      const det = el("details");
      det.appendChild(el("summary", "", "Show SQL"));
      det.appendChild(el("pre", "", a.sql));
      bubble.appendChild(det);
    }

    const actions = el("div", "actions");
    if (a.sql) {
      const copy = el("button", "ghost", "Copy SQL");
      copy.type = "button";
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(a.sql); copy.textContent = "Copied"; }
        catch (_) { copy.textContent = "Copy failed"; }
      };
      actions.appendChild(copy);
    }
    if (a.ok && a.rows.length) {
      const dl = el("button", "ghost", "Download CSV");
      dl.type = "button";
      dl.onclick = () => download("answer.csv", csv(a.columns, a.rows));
      actions.appendChild(dl);
    }
    if (a.ok && a.sql && !a.verified) {
      const good = el("button", "ghost", "Correct");
      const bad = el("button", "ghost", "Wrong");
      good.type = bad.type = "button";
      good.title = "Save this as a verified answer; similar questions will use it";
      bad.title = "Forget this answer";
      const send = async (correct) => {
        good.disabled = bad.disabled = true;
        try {
          await api("/api/feedback", { question: a.question, sql: a.sql, correct });
          (correct ? good : bad).textContent = correct ? "Saved as verified" : "Forgotten";
        } catch (e) { good.disabled = bad.disabled = false; alert(e.message); }
      };
      good.onclick = () => send(true);
      bad.onclick = () => send(false);
      actions.appendChild(good); actions.appendChild(bad);
    }
    if (actions.children.length) bubble.appendChild(actions);
    return wrap;
  }

  async function ask(question) {
    if (busy) return;
    question = question.trim();
    if (!question) return;
    busy = true; send.disabled = true;
    add(message("user", question).wrap);
    const pending = add(message("bot").wrap);
    pending.querySelector(".bubble").appendChild(el("div", "thinking", "Working on it..."));
    try {
      const a = await api("/api/ask", { question, history });
      pending.replaceWith(renderAnswer(a));
      if (a.ok && a.sql) {
        history.push({ question, sql: a.sql });
        if (history.length > MAX_HISTORY) history = history.slice(-MAX_HISTORY);
      }
    } catch (e) {
      pending.replaceWith(message("error", e.message).wrap);
    } finally {
      busy = false; send.disabled = false; q.focus();
      log.lastElementChild?.scrollIntoView({ block: "end" });
    }
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); const v = q.value; q.value = ""; grow(); ask(v); });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  function grow() { q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight, 160) + "px"; }
  q.addEventListener("input", grow);

  $("clear").onclick = () => {
    history = [];
    log.replaceChildren();
    const empty = el("div", "empty"); empty.id = "empty";
    empty.appendChild(el("p", "", "New conversation. Ask a question about your data."));
    log.appendChild(empty);
    q.focus();
  };

  // ---------------------------------------------------------------- schema
  let allTables = [];
  function drawTables() {
    const f = $("filter").value.trim().toLowerCase();
    const list = $("tables");
    const frag = document.createDocumentFragment();
    allTables.filter(t => !f || t.name.toLowerCase().includes(f) || t.columns.some(c => c.toLowerCase().includes(f)))
      .slice(0, 500)
      .forEach(t => {
        const li = el("li");
        li.appendChild(el("div", "tname", t.name + (t.kind === "view" ? " (view)" : "")));
        li.appendChild(el("div", "tcols", t.columns.join(", ")));
        if (t.comment) li.title = t.comment;
        frag.appendChild(li);
      });
    list.replaceChildren(frag);
  }

  async function loadSchema(refresh) {
    const note = $("side-note");
    note.textContent = refresh ? "Re-reading schema..." : "Loading...";
    try {
      if (refresh) await api("/api/refresh", {});
      const s = await api("/api/schema");
      allTables = s.tables;
      drawTables();
      note.textContent = `${s.tables.length} tables · ${s.dialect}` + (s.warnings.length ? ` · ${s.warnings.length} warning(s)` : "");
      if (s.warnings.length) note.title = s.warnings.join("\n");
      suggest(s.tables);
    } catch (e) { note.textContent = e.message; }
  }

  function suggest(tables) {
    const box = $("suggestions");
    if (!box) return;
    const picks = tables.slice(0, 3).map(t => `How many rows are in ${t.name}?`);
    box.replaceChildren(...picks.map(p => {
      const b = el("button", "ghost", p); b.type = "button"; b.onclick = () => ask(p); return b;
    }));
  }

  $("filter").addEventListener("input", drawTables);
  $("refresh").onclick = () => loadSchema(true);
  $("toggle-side").onclick = () => $("sidebar").classList.toggle("open");
  loadSchema(false);
  q.focus();
})();
