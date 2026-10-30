document.querySelectorAll("form.send").forEach(f => f.addEventListener("submit", () =>
  setTimeout(() => location.reload(), 1500)));
document.querySelectorAll("button[data-copy]").forEach(b => b.addEventListener("click", () => {
  navigator.clipboard.writeText(document.getElementById(b.dataset.copy).value).then(() => {
    b.textContent = "Copied"; setTimeout(() => b.textContent = "Copy LinkedIn note", 1500); });
}));
