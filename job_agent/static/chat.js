// The assistant chat: sends your messages, polls for what the assistant does, renders it.
// Everything shown is escaped first; Markdown is applied to the escaped text.
(function () {
  "use strict";
  const root = document.getElementById("chat");
  if (!root) return;
  const token = root.dataset.token;
  let chatId = root.dataset.chat ? Number(root.dataset.chat) : null;
  const log = document.getElementById("log");
  const live = document.getElementById("live");
  const tasksBox = document.getElementById("tasks");
  const input = document.getElementById("message");
  const sendButton = document.getElementById("send");
  const stopButton = document.getElementById("stop");
  const model = document.getElementById("model");
  const empty = document.getElementById("empty");
  const nodes = new Map();
  let rev = 0, running = false, timer = null, stickToBottom = true, shownTasks = "";

  // ----- Markdown (on escaped text) -------------------------------------------------

  function esc(text) {
    return String(text ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  }

  function inline(text) {
    const codes = [];
    text = text.replace(/`([^`]+)`/g, (_, code) => { codes.push(code); return `\u0000${codes.length - 1}\u0000`; });
    text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, label, href) => {
      const url = href.replace(/&amp;/g, "&");
      if (!/^(https?:\/\/|\/(?!\/))/.test(url)) return match;
      const external = /^https?:/.test(url) ? ' target="_blank" rel="noopener noreferrer"' : "";
      return `<a href="${esc(url)}"${external}>${label}</a>`;
    });
    text = text.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<i>$2</i>");
    return text.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
  }

  function markdown(source) {
    const lines = esc(source).split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        const body = [];
        for (i++; i < lines.length && !/^```/.test(lines[i]); i++) body.push(lines[i]);
        out.push(`<pre>${body.join("\n")}</pre>`); i++; continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.*)/);
      if (heading) { out.push(`<h4>${inline(heading[2])}</h4>`); i++; continue; }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const cells = row => row.trim().replace(/^\||\|$/g, "").split("|").map(c => inline(c.trim()));
        const head = cells(line);
        const rows = [];
        for (i += 2; i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]); i++) rows.push(cells(lines[i]));
        out.push(`<table><tr>${head.map(c => `<th>${c}</th>`).join("")}</tr>` +
                 rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("") + "</table>");
        continue;
      }
      if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const items = [];
        for (; i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i]); i++) {
          items.push(`<li>${inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""))}</li>`);
        }
        out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
        continue;
      }
      if (!line.trim()) { i++; continue; }
      const para = [];
      for (; i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*([-*]|\d+\.)\s|\s*\|)/.test(lines[i]); i++) {
        para.push(inline(lines[i]));
      }
      if (!para.length) { para.push(inline(lines[i])); i++; }
      out.push(`<p>${para.join("<br>")}</p>`);
    }
    return out.join("");
  }

  // ----- items ----------------------------------------------------------------------

  const TOOL_NAMES = {
    overview: "Checked where things stand", find_jobs: "Looked through jobs", get_job: "Opened a job",
    get_packet: "Read an application", refresh_jobs: "Refreshing job lists", discover_companies: "Discovering companies",
    rank_jobs: "Ranking jobs", prepare_applications: "Preparing applications", edit_answer: "Edited an answer",
    edit_document: "Edited a document", request_submission: "Asked you to confirm", set_job_status: "Updated jobs",
    read_file: "Read a file", edit_file: "Edited a file", update_search: "Updated your search",
    list_tasks: "Checked tasks", wait_for_tasks: "Waiting for tasks", cancel_task: "Cancelled a task",
    WebSearch: "Searched the web", WebFetch: "Read a web page", web_search: "Searched the web", web_fetch: "Read a web page",
  };

  function argsLine(name, args) {
    if (!args) return "";
    if (args.query) return `“${args.query}”`;
    if (args.url) return args.url;
    if (args.job_ids) return `jobs ${args.job_ids.join(", ")}`;
    if (args.job_id) return `job ${args.job_id}`;
    if (args.file) return args.file;
    if (args.task_ids) return `tasks ${args.task_ids.join(", ")}`;
    return "";
  }

  function render(item) {
    const d = item.data;
    const el = document.createElement("div");
    el.className = `item ${item.kind}`;
    if (item.kind === "user") {
      el.innerHTML = `<div class="bubble">${esc(d.text).replace(/\n/g, "<br>")}</div>`;
    } else if (item.kind === "text") {
      el.innerHTML = `<div class="md">${markdown(d.text)}</div>`;
    } else if (item.kind === "tool") {
      const label = TOOL_NAMES[d.name] || d.name;
      const what = argsLine(d.name, d.args);
      const state = d.status === "running" ? '<span class="spin"></span>' : d.status === "error" ? "⚠︎" : "✓";
      el.innerHTML = `<details><summary><span class="state ${esc(d.status)}">${state}</span> ${esc(label)}` +
        (what ? ` <span class="muted">${esc(what)}</span>` : "") +
        (d.summary ? ` <span class="muted">· ${esc(d.summary)}</span>` : "") + `</summary>` +
        `<pre>${esc(JSON.stringify(d.args, null, 1))}</pre>` + (d.result ? `<pre>${esc(pretty(d.result))}</pre>` : "") +
        `</details>`;
    } else if (item.kind === "confirm") {
      const jobs = d.jobs.map(j => `<li><a href="/job/${j.id}" target="_blank">${esc(j.title)}</a> at ${esc(j.company)}</li>`).join("");
      let foot;
      if (d.state === "pending") {
        foot = `<div class="row"><button class="primary" data-choice="yes">Approve and submit ${d.jobs.length}</button>` +
               `<button data-choice="no">Not now</button><span class="muted">Open each to review it first.</span></div>`;
      } else {
        foot = `<p class="muted">${esc(d.result || (d.state === "done" ? "Confirmed." : "Cancelled."))}</p>`;
      }
      el.innerHTML = `<div class="card ask"><b>Submit ${d.jobs.length === 1 ? "this application" : `these ${d.jobs.length} applications`}?</b>` +
                     `<ul>${jobs}</ul>${foot}</div>`;
    } else if (item.kind === "undo") {
      const done = d.state === "undone";
      el.innerHTML = `<div class="undo"><span>Changed <b>${esc(d.file)}</b>${done ? " — undone" : ""}</span>` +
                     (done || d.state === "kept" ? "" : `<button data-choice="undo">Undo</button>`) + `</div>`;
    } else if (item.kind === "note") {
      el.innerHTML = `<div class="note">${esc(d.shown || d.text)}</div>`;
    } else if (item.kind === "error") {
      el.innerHTML = `<div class="flash bad">${esc(d.text)}</div>`;
    }
    el.querySelectorAll("button[data-choice]").forEach(button => button.addEventListener("click", () => {
      button.disabled = true;
      post("/assistant/api/card", {item: item.id, choice: button.dataset.choice}).then(poll);
    }));
    return el;
  }

  function pretty(text) {
    try { return JSON.stringify(JSON.parse(text), null, 1); } catch (_) { return text; }
  }

  function place(item) {
    const fresh = render(item);
    const old = nodes.get(item.id);
    if (old) {
      if (old.querySelector("details")?.open) fresh.querySelector("details")?.setAttribute("open", "");
      old.replaceWith(fresh);
    } else {
      log.insertBefore(fresh, live);
    }
    nodes.set(item.id, fresh);
  }

  function showTasks(tasks) {
    tasksBox.innerHTML = tasks.map(t => {
      const job = t.job_id ? ` · <a href="/job/${t.job_id}" target="_blank">job ${t.job_id}</a>` : "";
      if (t.status === "waiting") {
        return `<div class="task waiting"><b>${esc(t.what)} needs you:</b> ${esc(t.question)}${job}` +
               `<form data-task="${t.id}" class="row"><input type="text" name="answer" autocomplete="off">` +
               `<button class="primary">Send</button></form></div>`;
      }
      return `<div class="task"><span class="spin"></span> ${esc(t.what)} <span class="pill ${esc(t.status)}">${esc(t.status)}</span>${job}</div>`;
    }).join("");
    tasksBox.querySelectorAll("form[data-task]").forEach(form => form.addEventListener("submit", event => {
      event.preventDefault();
      post("/assistant/api/reply", {task_id: form.dataset.task, answer: form.answer.value}).then(poll);
    }));
  }

  // ----- talking to the dashboard -----------------------------------------------------

  function post(path, fields) {
    const body = new URLSearchParams({token, ...fields});
    return fetch(path, {method: "POST", body}).then(r => r.json()).then(reply => {
      if (reply.error) alert(reply.error);
      return reply;
    });
  }

  function poll() {
    clearTimeout(timer);
    const params = new URLSearchParams({since: rev});
    if (chatId) params.set("chat", chatId);
    return fetch(`/assistant/api/poll?${params}`).then(r => r.json()).then(state => {
      stickToBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
      for (const item of state.items) place(item);
      rev = Math.max(rev, state.rev);
      running = state.running;
      live.innerHTML = running ? (state.partial ? `<div class="md">${markdown(state.partial)}</div>` :
                                  '<div class="thinking"><span></span><span></span><span></span></div>') : "";
      const taskKey = JSON.stringify(state.tasks);
      if (taskKey !== shownTasks) { shownTasks = taskKey; showTasks(state.tasks); }
      if (empty) empty.hidden = nodes.size > 0 || running;
      sendButton.hidden = running;
      stopButton.hidden = !running;
      if (stickToBottom && (state.items.length || running)) window.scrollTo(0, document.body.scrollHeight);
      const busy = running || state.tasks.length;
      timer = setTimeout(poll, running ? 500 : busy ? 2000 : 5000);
    }).catch(() => { timer = setTimeout(poll, 4000); });
  }

  function send() {
    const text = input.value.trim();
    if (!text || running) return;
    sendButton.disabled = true;
    post("/assistant/api/send", {chat: chatId || "", text, model: model.value}).then(reply => {
      sendButton.disabled = false;
      if (reply.error) return;
      input.value = "";
      input.style.height = "";
      if (!chatId) { location.href = `/assistant?chat=${reply.chat}`; return; }
      running = true;
      poll();
    });
  }

  sendButton.addEventListener("click", send);
  stopButton.addEventListener("click", () => chatId && post("/assistant/api/stop", {chat: chatId}).then(poll));
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); }
  });
  input.addEventListener("input", () => { input.style.height = ""; input.style.height = Math.min(input.scrollHeight, 220) + "px"; });
  model.addEventListener("change", () => chatId && post("/assistant/api/model", {chat: chatId, model: model.value}));
  document.querySelectorAll("[data-prompt]").forEach(button => button.addEventListener("click", () => {
    input.value = button.dataset.prompt; input.focus();
  }));
  document.querySelectorAll("[data-delete]").forEach(button => button.addEventListener("click", event => {
    event.preventDefault();
    if (!confirm("Delete this chat? Jobs and applications aren't affected.")) return;
    post("/assistant/api/delete", {chat: button.dataset.delete}).then(() => {
      location.href = Number(button.dataset.delete) === chatId ? "/assistant" : location.href;
    });
  }));
  input.focus();
  poll();
})();
