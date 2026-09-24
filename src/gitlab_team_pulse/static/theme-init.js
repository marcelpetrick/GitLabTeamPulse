// Applied synchronously before first paint to avoid a light/dark flash.
(function () {
  var theme = null;
  try { theme = window.sessionStorage.getItem("teampulse-theme"); } catch (e) { theme = null; }
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", theme);
})();
