// Pages with work running update in place: no reload, no flash, scroll and open sections kept.
(() => {
  if (!document.body.hasAttribute("data-live")) return;
  const tick = async () => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
    if (!typing && !document.hidden) {
      try {
        const html = await (await fetch(location.href, {cache: "no-store"})).text();
        const doc = new DOMParser().parseFromString(html, "text/html");
        const fresh = doc.querySelector("main"), now = document.querySelector("main");
        if (fresh && now && fresh.innerHTML !== now.innerHTML) {
          const open = [...now.querySelectorAll("details")].map((d) => d.open);
          // Forms marked data-keep stay as you left them: what you typed, the file you chose.
          const kept = [...now.querySelectorAll("[data-keep][id]")];
          now.innerHTML = fresh.innerHTML;
          kept.forEach((old) => now.querySelector("#" + old.id)?.replaceWith(old));
          now.querySelectorAll("details").forEach((d, i) => { if (open[i]) d.open = true; });
          // Scripts in the page (toggles, copy buttons) bind to the new content again.
          now.querySelectorAll("script[src]").forEach((old) => {
            const s = document.createElement("script"); s.src = old.src; old.replaceWith(s);
          });
        }
        if (!doc.body.hasAttribute("data-live")) return;  // Nothing running any more.
      } catch (_) { /* The next tick tries again. */ }
    }
    setTimeout(tick, 2500);
  };
  setTimeout(tick, 2500);
})();
// The background-work tray, on every page: it follows the work while any is running.
(() => {
  const poll = async () => {
    const tray = document.getElementById("tray");
    if (!tray) return;
    if (!document.hidden) {
      try {
        const html = await (await fetch("/tray", {cache: "no-store"})).text();
        const open = tray.querySelector("details")?.open ?? true;
        tray.outerHTML = html;
        const details = document.querySelector("#tray details");
        if (details) details.open = open;
      } catch (_) { /* The next poll tries again. */ }
    }
    setTimeout(poll, document.getElementById("tray")?.hasAttribute("data-active") ? 3000 : 15000);
  };
  setTimeout(poll, 3000);
})();
// Resume PDFs dropped on an upload box. A file dropped anywhere else is ignored, rather than
// the browser opening it in place of the page.
(() => {
  const isPdf = (f) => /\.pdf$/i.test(f.name) || f.type === "application/pdf";
  const files = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  const show = (zone, chosen, problem) => {
    const out = zone.querySelector(".picked");
    out.classList.toggle("bad", !!problem);
    out.textContent = problem || [...chosen].map((f) => f.name).join(", ");
  };
  for (const type of ["dragenter", "dragover"]) {
    document.addEventListener(type, (e) => {
      if (!files(e)) return;
      e.preventDefault();
      const zone = e.target.closest?.(".dropzone");
      document.querySelectorAll(".dropzone.over").forEach((z) => z !== zone && z.classList.remove("over"));
      zone?.classList.add("over");
      e.dataTransfer.dropEffect = zone ? "copy" : "none";
    });
  }
  document.addEventListener("dragleave", (e) => {
    const zone = e.target.closest?.(".dropzone");
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove("over");
  });
  document.addEventListener("drop", (e) => {
    if (!files(e)) return;
    e.preventDefault();
    document.querySelectorAll(".dropzone.over").forEach((z) => z.classList.remove("over"));
    const zone = e.target.closest?.(".dropzone");
    if (!zone) return;
    const pdfs = [...e.dataTransfer.files].filter(isPdf);
    if (!pdfs.length) return show(zone, [], "That isn't a PDF. Save your resume as a PDF and drop that.");
    const box = new DataTransfer();
    pdfs.forEach((f) => box.items.add(f));
    zone.querySelector("input[type=file]").files = box.files;
    show(zone, pdfs);
  });
  document.addEventListener("change", (e) => {
    const zone = e.target.closest?.(".dropzone");
    if (zone && e.target.type === "file") show(zone, e.target.files);
  });
})();
