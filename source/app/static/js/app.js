/*
 * Ebay Watcher – kleine Helfer im Browser. Alles funktioniert auch ohne JavaScript.
 *
 * Das ist die QUELLE. CodeKit verkleinert sie nach build/app/static/js/app-min.js.
 * Tailwind durchsucht diese Datei ebenfalls nach Klassen und Attributen (siehe app.css).
 */
(function () {
  "use strict";

  // ------------------------------------------------------------------
  // Übersicht: Karten oder Liste (wird auf diesem Gerät gemerkt)
  // Schaltet das Attribut data-view um; die Klassen view-cards:… und view-list:… reagieren darauf.

  var collection = document.querySelector("[data-collection]");
  var viewButtons = document.querySelectorAll("button[data-set-view]");
  var VIEW_KEY = "ebay-watcher-view";

  // Im privaten Surfen kann der Browser den Speicher sperren. Dann gilt einfach die Standardansicht,
  // und in der Browser-Konsole steht ein kurzer Hinweis.
  function rememberView(view) {
    try {
      localStorage.setItem(VIEW_KEY, view);
    } catch (error) {
      console.info("Ebay Watcher: Ansicht wird nicht gespeichert (" + error.name + ")");
    }
  }

  function savedView() {
    try {
      return localStorage.getItem(VIEW_KEY);
    } catch (error) {
      console.info("Ebay Watcher: gespeicherte Ansicht nicht lesbar (" + error.name + ")");
      return null;
    }
  }

  function setView(view) {
    if (!collection) return;
    collection.setAttribute("data-view", view);
    viewButtons.forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.setView === view));
    });
    rememberView(view);
  }

  if (collection) {
    var saved = savedView();
    if (saved === "list" || saved === "cards") setView(saved);
    viewButtons.forEach(function (b) {
      b.addEventListener("click", function () { setView(b.dataset.setView); });
    });
  }

  // ------------------------------------------------------------------
  // Restlaufzeiten aktuell halten. In der letzten Stunde bekommt die Zeit
  // das Attribut data-soon, das die Klassen data-soon:… hervorheben.

  function format(left) {
    if (left <= 0) return "beendet";
    if (left < 60) return "unter 1 Min";
    var minutes = Math.floor(left / 60);
    var hours = Math.floor(minutes / 60);
    minutes = minutes % 60;
    var days = Math.floor(hours / 24);
    hours = hours % 24;
    if (days >= 1) return days + (days === 1 ? " Tag " : " Tage ") + hours + " Std";
    if (hours >= 1) return hours + " Std " + minutes + " Min";
    return minutes + " Min";
  }

  function tick() {
    var now = Date.now() / 1000;
    document.querySelectorAll("[data-end]").forEach(function (el) {
      var left = Math.floor(Number(el.dataset.end) - now);
      el.textContent = format(left);
      el.toggleAttribute("data-soon", left > 0 && left < 3600);
    });
  }

  tick();
  setInterval(tick, 15000);

  // ------------------------------------------------------------------
  // Sicherheitsabfrage vor dem Löschen

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // ------------------------------------------------------------------
  // Gesuch-Formular: Erinnerung nur zeigen, wenn Auktionen dabei sind

  var formatInputs = document.querySelectorAll('input[name="format"]');

  function syncFormat() {
    var checked = document.querySelector('input[name="format"]:checked');
    var value = checked ? checked.value : "auction";
    document.querySelectorAll("[data-when-format]").forEach(function (el) {
      el.hidden = el.dataset.whenFormat.split(" ").indexOf(value) === -1;
    });
  }

  formatInputs.forEach(function (i) { i.addEventListener("change", syncFormat); });
  if (formatInputs.length) syncFormat();
})();
