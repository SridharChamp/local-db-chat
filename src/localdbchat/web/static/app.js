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

  // ----------------------------------------------------------------- setup
  let status = null;
  let pullTimer = null;

  function showSetup(on) {
    $("setup").hidden = !on;
    log.hidden = on;
    form.hidden = on;
    $("setup-close").hidden = !(status && status.configured);
  }

  function fillKinds(kinds) {
    const sel = $("f-kind");
    if (sel.options.length) return;
    kinds.forEach(k => { const o = el("option", "", k.label); o.value = k.kind; sel.appendChild(o); });
    sel.onchange = kindChanged;
    kindChanged();
  }

  function kindChanged() {
    const kind = $("f-kind").value;
    const info = (status?.kinds || []).find(k => k.kind === kind);
    $("server-fields").hidden = kind === "sqlite";
    $("file-fields").hidden = kind !== "sqlite";
    $("ssl-wrap").hidden = kind === "mssql";
    $("trust-wrap").hidden = kind !== "mssql";
    $("f-port").placeholder = info && info.port ? String(info.port) : "";
    setMsg("");
  }

  function setMsg(text, kind) {
    const m = $("db-msg");
    m.textContent = text;
    m.className = "msg-line" + (kind ? " " + kind : "");
  }

  function connectionBody() {
    const port = $("f-port").value.trim();
    return {
      kind: $("f-kind").value, host: $("f-host").value.trim(), port: port ? Number(port) : null,
      database: $("f-database").value.trim(), user: $("f-user").value.trim(), password: $("f-password").value,
      path: $("f-path").value.trim(), ssl: $("f-ssl").checked, trust_certificate: $("f-trust").checked,
    };
  }

  function drawModel(s) {
    const text = $("model-text"), pullBtn = $("model-pull"), link = $("ollama-link");
    const p = s.pull || {};
    pullBtn.hidden = true; link.hidden = true; $("model-progress").hidden = true;
    text.className = "muted";
    if (p.running) {
      const pct = p.total ? Math.floor(p.completed / p.total * 100) : 0;
      text.textContent = `Downloading ${s.model}: ${p.status}${p.total ? ` ${pct}% of ${(p.total / 1e9).toFixed(1)} GB` : ""}`;
      $("model-progress").hidden = false;
      $("model-bar").style.width = pct + "%";
      return;
    }
    if (!s.ollama_running) {
      text.textContent = "Ollama is not running on this computer. Install it, start it, then press Check again. " + (s.ollama_error || "");
      link.hidden = false;
      return;
    }
    if (!s.model_installed) {
      text.textContent = (p.error ? p.error + " " : "") +
        `The model ${s.model} is not downloaded yet. It is a one-time download of a few GB through Ollama.`;
      pullBtn.textContent = `Download ${s.model}`;
      pullBtn.hidden = false;
      return;
    }
    text.textContent = `Ready: ${s.model} is installed.`;
    text.className = "ok-text";
  }

  async function refreshStatus() {
    try { status = await api("/api/status"); }
    catch (e) { $("model-text").textContent = e.message; return null; }
    fillKinds(status.kinds);
    drawModel(status);
    $("conn-line").textContent = status.configured
      ? `Connected to ${status.database}. Read-only. Runs on this computer only.`
      : "Not connected yet. Runs on this computer only.";
    if (status.pull && status.pull.running) {
      clearTimeout(pullTimer);
      pullTimer = setTimeout(refreshStatus, 1500);
    }
    return status;
  }

  $("model-recheck").onclick = refreshStatus;
  $("model-pull").onclick = async () => {
    $("model-pull").disabled = true;
    try { await api("/api/model/pull", {}); } catch (e) { alert(e.message); }
    $("model-pull").disabled = false;
    refreshStatus();
  };

  $("db-test").onclick = async () => {
    setMsg("Testing…");
    $("db-test").disabled = true;
    try {
      const r = await api("/api/setup/test", connectionBody());
      setMsg(r.message, r.ok ? "good" : "bad");
    } catch (e) { setMsg(e.message, "bad"); }
    finally { $("db-test").disabled = false; }
  };

  $("db-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    setMsg("Connecting and reading the schema…");
    $("db-save").disabled = true;
    try {
      const r = await api("/api/setup/save", connectionBody());
      $("f-password").value = "";
      const s = await refreshStatus();
      history = [];
      loadSchema(false);
      if (s && !s.model_installed) {
        setMsg(r.message + " Now download the model above to start asking questions.", "good");
        $("setup-close").hidden = false;
        return;
      }
      setMsg(r.message, "good");
      showSetup(false);
      $("clear").click();
    } catch (err) { setMsg(err.message, "bad"); }
    finally { $("db-save").disabled = false; }
  });

  $("settings").onclick = async () => { await refreshStatus(); showSetup(true); };
  $("setup-close").onclick = () => showSetup(false);

  (async function boot() {
    const s = await refreshStatus();
    if (!s || !s.configured || !s.model_installed) {
      showSetup(true);
      if (s && s.configured) loadSchema(false);
      return;
    }
    showSetup(false);
    loadSchema(false);
    q.focus();
  })();
})();
