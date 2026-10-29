const box = document.getElementById("timeline").parentElement;
for (const [id, cls] of [["show-raw", "show-raw"], ["only-checks", "only-checks"]]) {
  const input = document.getElementById(id);
  try { input.checked = localStorage.getItem(id) === "1"; } catch (_) {}
  const apply = () => { box.classList.toggle(cls, input.checked);
    try { localStorage.setItem(id, input.checked ? "1" : "0"); } catch (_) {} };
  input.addEventListener("change", apply); apply();
}
