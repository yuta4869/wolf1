// wolf local GUI shell — stdlib-only client. No bundler, no
// framework. All user-supplied data is written via textContent.

(() => {
  "use strict";

  // -------- Per-command argument schema --------
  // value:  string input
  // bool:   checkbox
  // pick:   select with `choices`
  const CMD_SCHEMA = {
    "search-files": [
      { name: "query", type: "value", required: true },
      { name: "path", type: "value" },
      { name: "max_hits", type: "value", placeholder: "e.g. 5" },
    ],
    "summarize-file": [
      { name: "path", type: "value", required: true },
      { name: "backend", type: "pick", choices: ["fake", "ollama"] },
      { name: "model", type: "value" },
    ],
    "search-summarize": [
      { name: "query", type: "value", required: true },
      { name: "path", type: "value" },
      { name: "backend", type: "pick", choices: ["fake", "ollama"] },
      { name: "model", type: "value" },
    ],
    "mail-search": [
      { name: "path", type: "value", required: true },
      { name: "query", type: "value", required: true },
    ],
    "mail-summarize": [
      { name: "path", type: "value", required: true },
      { name: "backend", type: "pick", choices: ["fake", "ollama"] },
      { name: "model", type: "value" },
    ],
    "mail-thread": [
      { name: "path", type: "value", required: true },
    ],
    "mail-search-summarize": [
      { name: "path", type: "value", required: true },
      { name: "query", type: "value", required: true },
      { name: "backend", type: "pick", choices: ["fake", "ollama"] },
      { name: "model", type: "value" },
    ],
    "gmail-search": [
      { name: "query", type: "value", required: true },
      { name: "gmail_backend", type: "pick", choices: ["fake", "gmail"] },
      { name: "credentials_path", type: "value" },
    ],
    "gmail-read": [
      { name: "message_id", type: "value", required: true },
      { name: "gmail_backend", type: "pick", choices: ["fake", "gmail"] },
      { name: "credentials_path", type: "value" },
    ],
    "gmail-thread": [
      { name: "gmail_backend", type: "pick", choices: ["fake", "gmail"] },
      { name: "credentials_path", type: "value" },
      { name: "query", type: "value" },
      { name: "message_id", type: "value" },
      { name: "thread_id", type: "value" },
    ],
    "gmail-summarize": [
      { name: "gmail_backend", type: "pick", choices: ["fake", "gmail"] },
      { name: "credentials_path", type: "value" },
      { name: "llm_backend", type: "pick", choices: ["fake", "ollama"] },
      { name: "model", type: "value" },
      { name: "query", type: "value" },
      { name: "message_id", type: "value" },
    ],
    "gmail-search-summarize": [
      { name: "gmail_backend", type: "pick", choices: ["fake", "gmail"] },
      { name: "credentials_path", type: "value" },
      { name: "llm_backend", type: "pick", choices: ["fake", "ollama"] },
      { name: "model", type: "value" },
      { name: "query", type: "value" },
      { name: "thread_id", type: "value" },
      { name: "threaded", type: "bool" },
    ],
    "audit-tail": [
      { name: "limit", type: "value", placeholder: "20" },
      { name: "action_kind", type: "value", placeholder: "e.g. gmail.read" },
    ],
  };

  const SETTINGS_SCHEMA = [
    { name: "default_llm_backend", type: "pick", choices: ["fake", "ollama"] },
    { name: "default_ollama_model", type: "value" },
    { name: "default_ollama_url", type: "value" },
    { name: "default_gmail_backend", type: "pick", choices: ["fake", "gmail"] },
    { name: "default_output", type: "pick", choices: ["json", "text"] },
    { name: "theme", type: "pick", choices: ["system", "light", "dark"] },
    { name: "avatar_enabled", type: "bool" },
    { name: "avatar_style", type: "pick", choices: ["placeholder"] },
    { name: "gmail_credentials_path", type: "value" },
  ];

  // -------- helpers --------

  function $(sel) {
    return document.querySelector(sel);
  }

  function clear(el) {
    while (el && el.firstChild) {
      el.removeChild(el.firstChild);
    }
  }

  function makeField(spec, getValue) {
    const wrap = document.createElement("label");
    wrap.textContent = spec.name + (spec.required ? " *" : "");
    let input;
    if (spec.type === "bool") {
      input = document.createElement("input");
      input.type = "checkbox";
    } else if (spec.type === "pick") {
      input = document.createElement("select");
      (spec.choices || []).forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c;
        opt.textContent = c;
        input.appendChild(opt);
      });
    } else {
      input = document.createElement("input");
      input.type = "text";
      if (spec.placeholder) input.placeholder = spec.placeholder;
    }
    input.dataset.name = spec.name;
    input.dataset.kind = spec.type;
    if (getValue && typeof getValue === "function") {
      const v = getValue(spec.name);
      if (v !== undefined && v !== null) {
        if (spec.type === "bool") input.checked = !!v;
        else input.value = String(v);
      }
    }
    wrap.appendChild(input);
    return wrap;
  }

  function readFields(container) {
    const out = {};
    container.querySelectorAll("input,select").forEach((el) => {
      const name = el.dataset.name;
      if (!name) return;
      if (el.dataset.kind === "bool") {
        out[name] = !!el.checked;
      } else {
        out[name] = el.value;
      }
    });
    return out;
  }

  // -------- nav --------

  function activateTab(tabName) {
    document.querySelectorAll(".navtabs button").forEach((b) => {
      if (b.dataset.tab === tabName) b.classList.add("active");
      else b.classList.remove("active");
    });
    document.querySelectorAll("[data-panel]").forEach((p) => {
      if (p.dataset.panel === tabName) p.classList.add("active");
      else p.classList.remove("active");
    });
  }

  document.querySelectorAll(".navtabs button").forEach((b) => {
    b.addEventListener("click", () => activateTab(b.dataset.tab));
  });

  // -------- command panel --------

  function renderArgs() {
    const cmd = $("#cmd-select").value;
    const box = $("#cmd-args");
    clear(box);
    (CMD_SCHEMA[cmd] || []).forEach((spec) => {
      box.appendChild(makeField(spec));
    });
  }
  $("#cmd-select").addEventListener("change", renderArgs);
  renderArgs();

  async function runCommand(command, argsObj) {
    const result = $("#cmd-result");
    const stderr = $("#cmd-stderr");
    result.textContent = "(running…)";
    stderr.textContent = "";
    let resp;
    try {
      resp = await fetch("/api/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command, args: argsObj }),
      });
    } catch (e) {
      result.textContent = "(network error)";
      return;
    }
    let body;
    try {
      body = await resp.json();
    } catch (e) {
      result.textContent = "(invalid response)";
      return;
    }
    if (body.result !== undefined && body.result !== null) {
      result.textContent = JSON.stringify(body.result, null, 2);
    } else if (body.stdout_text) {
      result.textContent = body.stdout_text;
    } else if (body.error) {
      result.textContent = "ERROR: " + body.error;
    } else {
      result.textContent = JSON.stringify(body, null, 2);
    }
    stderr.textContent = body.stderr_text || "";
  }

  $("#cmd-run").addEventListener("click", () => {
    const command = $("#cmd-select").value;
    const args = readFields($("#cmd-args"));
    runCommand(command, args);
  });

  // -------- quick buttons --------

  document.querySelectorAll(".quick button").forEach((b) => {
    b.addEventListener("click", () => {
      const cmd = b.dataset.cmd;
      const key = b.dataset.key;
      const val = window.prompt(key + " for " + cmd, "");
      if (val === null) return;
      activateTab("command");
      $("#cmd-select").value = cmd;
      renderArgs();
      const inp = $("#cmd-args").querySelector(`input[data-name="${key}"]`);
      if (inp) inp.value = val;
      runCommand(cmd, { [key]: val });
    });
  });

  // -------- settings --------

  async function loadSettings() {
    const status = $("#settings-status");
    status.textContent = "(loading…)";
    try {
      const r = await fetch("/api/settings");
      const j = await r.json();
      const s = j.settings || {};
      const form = $("#settings-form");
      clear(form);
      SETTINGS_SCHEMA.forEach((spec) => {
        form.appendChild(makeField(spec, (n) => s[n]));
      });
      status.textContent = "loaded.";
    } catch (e) {
      status.textContent = "(load failed)";
    }
  }

  async function saveSettings() {
    const status = $("#settings-status");
    const payload = readFields($("#settings-form"));
    status.textContent = "(saving…)";
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (r.ok) {
        status.textContent = "saved.";
      } else if (j.error) {
        status.textContent = "ERROR: " + j.error;
      } else {
        status.textContent = "error.";
      }
    } catch (e) {
      status.textContent = "(network error)";
    }
  }
  $("#settings-save").addEventListener("click", saveSettings);
  loadSettings();

  // -------- audit panel --------

  async function loadAudit() {
    const out = $("#audit-result");
    out.textContent = "(loading…)";
    const limit = encodeURIComponent($("#audit-limit").value || "20");
    const kind = encodeURIComponent($("#audit-kind").value || "");
    let url = "/api/audit-tail?limit=" + limit;
    if (kind) url += "&action_kind=" + kind;
    try {
      const r = await fetch(url);
      const j = await r.json();
      out.textContent = JSON.stringify(j.result || j, null, 2);
    } catch (e) {
      out.textContent = "(network error)";
    }
  }
  $("#audit-reload").addEventListener("click", loadAudit);

  // -------- health --------

  async function checkHealth() {
    const pill = $("#health-pill");
    try {
      const r = await fetch("/api/health");
      const j = await r.json();
      if (r.ok && j.ok) {
        pill.textContent = "OK · " + j.host + ":" + j.port;
        pill.className = "ok";
        const footer = $("#footer-host");
        if (footer) footer.textContent = j.host + ":" + j.port;
      } else {
        pill.textContent = "down";
        pill.className = "bad";
      }
    } catch (e) {
      pill.textContent = "unreachable";
      pill.className = "bad";
    }
  }
  checkHealth();

  // -------- avatar placeholder --------

  function renderAvatarState() {
    const el = $("#avatar-state");
    if (!el) return;
    el.textContent =
      "avatar_enabled: (read from settings).\n" +
      "status: placeholder — no engine, no camera, no microphone, no WebRTC, no robot.";
  }
  renderAvatarState();
})();
