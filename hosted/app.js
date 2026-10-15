const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
};
let seeded = false, lastLog = "", lastPendingId = null, lastSide = "";

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.error || "Request failed.");
  return data;
}

function renderLog(log) {
  const key = JSON.stringify(log.length && log[log.length - 1]) + log.length;
  if (key === lastLog) return;
  lastLog = key;
  const box = $("log");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.replaceChildren(...log.map((e) => el("div", { className: `entry ${e.kind}`,
    textContent: (e.kind === "tool" ? "→ " : e.kind === "lead" ? "+ lead: " : "") + e.text })));
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderPending(p) {
  const box = $("pending");
  if (!p) { box.style.display = "none"; lastPendingId = null; return; }
  if (p.id === lastPendingId) return;
  lastPendingId = p.id;
  box.style.display = "block";
  const input = el("textarea", { rows: 2, placeholder: p.type === "approval"
    ? "Optional: a correction for the agent to make first" : "Your answer" });
  const send = async (body) => {
    try { await post("/reply", { id: p.id, ...body }); box.style.display = "none"; }
    catch (error) { $("status").textContent = error.message; }
  };
  if (p.type === "question") {
    box.replaceChildren(el("h2", { textContent: "The agent needs you" }),
      el("p", { textContent: p.text }), input,
      el("div", { className: "row" }, el("button", { textContent: "Send", onclick: () => send({ text: input.value }) })));
  } else {
    const answers = Object.entries(p.answers).map(([k, v]) => `${k}: ${v}`).join("\n");
    box.replaceChildren(el("h2", { textContent: `Ready to submit: ${p.job}` }),
      el("p", { textContent: p.summary }), el("pre", { textContent: answers }), input,
      el("div", { className: "row" },
        el("button", { textContent: "Approve and submit", onclick: () => send({ decision: "approve" }) }),
        el("button", { className: "ghost", textContent: "Send correction",
          onclick: () => input.value.trim() && send({ decision: "correct", text: input.value }) }),
        el("button", { className: "bad", textContent: "Don't submit", onclick: () => send({ decision: "decline" }) })));
  }
  input.focus();
}

function renderSide(s) {
  const key = JSON.stringify([s.inbox, s.jobs, s.checks, s.documents]);
  if (key === lastSide) return;
  lastSide = key;
  if (s.inbox.length) $("inbox").replaceChildren(...s.inbox.map((m) => el("div", { className: "mail", textContent: m })));
  if (s.jobs.length) $("jobs").replaceChildren(el("table", {}, ...s.jobs.map((j) => el("tr", {},
    el("td", {}, el("span", { className: `pill ${j.status}`, textContent: j.status })),
    el("td", {}, el("div", { textContent: `${j.title} at ${j.company}` }),
      j.reason ? el("div", { className: "muted", textContent: j.reason.slice(0, 220) }) : null)))));
  if (s.checks.length) $("checks").replaceChildren(...s.checks.map((c) => el("div", { className: "check" },
    el("div", { textContent: c.job }),
    ...Object.entries(c).filter(([k]) => k !== "job").map(([k, ok]) =>
      el("div", {}, ok ? el("b", { textContent: "PASS " }) : el("i", { textContent: "FAIL " }), k)))));
  if (s.documents.length) $("docs").replaceChildren(...s.documents.map((d) =>
    el("details", {}, el("summary", { textContent: `${d.kind}: ${d.job}` }), el("pre", { textContent: d.text }))));
}

async function refresh() {
  try {
    const response = await fetch("/state", { cache: "no-store" });
    if (!response.ok) throw Error("Return to the portal.");
    const s = await response.json();
    if (!seeded) {
      $("profile").value = JSON.stringify(s.sample.profile, null, 2);
      $("resume").value = s.sample.resume;
      seeded = true;
    }
    $("model").textContent = s.model ? `Model: ${s.model}` : "";
    $("go").disabled = s.busy;
    const status = $("status");
    status.className = s.error ? "status error" : "status";
    status.textContent = s.error || (s.pending ? "Waiting for you." : s.busy ? "Working…" : "");
    renderLog(s.log);
    renderPending(s.pending);
    renderSide(s);
  } catch (error) {
    $("status").textContent = error.message;
  }
}

$("run").onsubmit = async (event) => {
  event.preventDefault();
  let profile;
  try { profile = JSON.parse($("profile").value); }
  catch { $("status").textContent = "The profile is not valid JSON."; return; }
  try {
    await post("/run", { request: $("request").value, profile, resume: $("resume").value,
      max_jobs: Number($("max").value) });
  } catch (error) { $("status").textContent = error.message; }
  refresh();
};
refresh();
setInterval(refresh, 800);
