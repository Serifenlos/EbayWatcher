/*
 * Heuhaufen – kleine Helfer im Browser. Alles funktioniert auch ohne JavaScript.
 *
 * Das ist die QUELLE. CodeKit verkleinert sie nach build/app/static/js/app-min.js.
 * Tailwind durchsucht diese Datei ebenfalls nach Klassen und Attributen (siehe app.css).
 */
(function () {
  "use strict";

  // Bis November 2026 hieß das Tool „Ebay Watcher“. Was der Browser sich unter dem alten Namen gemerkt hat
  // (Ansicht, Seitengröße, Sortierung), einmal unter dem neuen Namen übernehmen.
  try {
    ["view", "page-size", "sort"].forEach(function (name) {
      var old = localStorage.getItem("ebay-watcher-" + name);
      if (old === null) return;
      if (localStorage.getItem("heuhaufen-" + name) === null) localStorage.setItem("heuhaufen-" + name, old);
      localStorage.removeItem("ebay-watcher-" + name);
    });
  } catch (error) {
    // Speicher gesperrt (privates Surfen): dann gelten die Vorgaben
    console.info("Heuhaufen: gemerkte Einstellungen nicht übernommen (" + error.name + ")");
  }

  // ------------------------------------------------------------------
  // Übersicht: Karten oder Liste (wird auf diesem Gerät gemerkt)
  // Schaltet das Attribut data-view um; die Klassen view-cards:… und view-list:… reagieren darauf.

  var collection = document.querySelector("[data-collection]");
  var viewButtons = document.querySelectorAll("button[data-set-view]");
  var VIEW_KEY = "heuhaufen-view";

  // Im privaten Surfen kann der Browser den Speicher sperren. Dann gilt einfach die Standardansicht,
  // und in der Browser-Konsole steht ein kurzer Hinweis.
  function rememberView(view) {
    try {
      localStorage.setItem(VIEW_KEY, view);
    } catch (error) {
      console.info("Heuhaufen: Ansicht wird nicht gespeichert (" + error.name + ")");
    }
  }

  function savedView() {
    try {
      return localStorage.getItem(VIEW_KEY);
    } catch (error) {
      console.info("Heuhaufen: gespeicherte Ansicht nicht lesbar (" + error.name + ")");
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
  // Restlaufzeiten aktuell halten (ui.countdown_span), gleiche Regeln wie util.countdown:
  // „8:54“, ab einem Tag „2 Tg 3:20“, unter data-seconds (Erinnerung des Gesuchs) „0:09:42“.
  // In der letzten Stunde bekommt die Zeit data-soon, das die Klassen data-soon:… hervorheben.

  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function format(left, secondsBelow) {
    var days = Math.floor(left / 86400);
    var rest = left % 86400;
    var hours = Math.floor(rest / 3600);
    var minutes = Math.floor((rest % 3600) / 60);
    if (days >= 1) return days + " Tg " + hours + ":" + pad(minutes);
    if (left < secondsBelow) return hours + ":" + pad(minutes) + ":" + pad(rest % 60);
    return hours + ":" + pad(minutes);
  }

  function tick() {
    var now = Date.now() / 1000;
    document.querySelectorAll("[data-end]").forEach(function (el) {
      var left = Math.floor(Number(el.dataset.end) - now);
      var time = el.querySelector("[data-left]");
      if (left <= 0) {
        if (time) el.textContent = el.hasAttribute("data-cap") ? "Beendet" : "beendet";
        return;
      }
      if (!time) return;
      var text = format(left, Number(el.dataset.seconds) || 600);
      if (time.textContent !== text) time.textContent = text;
      time.toggleAttribute("data-soon", left < 3600);
    });
  }

  tick();
  setInterval(tick, 1000);   // jede Sekunde, damit kurz vor Ende die Sekunden laufen

  // ------------------------------------------------------------------
  // Seiten: lange Listen zeigen nur einen Teil der Angebote (Vorgabe 20, Feld „pro Seite“, auf diesem
  // Gerät gemerkt). Ausgeblendete Zeilen laden ihre Bilder erst, wenn ihre Seite drankommt.

  var PAGE_KEY = "heuhaufen-page-size";
  var DEFAULT_PAGE_SIZE = 20;

  function savedPageSize() {
    try {
      var n = parseInt(localStorage.getItem(PAGE_KEY), 10);
      return n >= 1 && n <= 200 ? n : DEFAULT_PAGE_SIZE;
    } catch (error) {
      console.info("Heuhaufen: Seitengröße nicht lesbar (" + error.name + ")");
      return DEFAULT_PAGE_SIZE;
    }
  }

  function rememberPageSize(n) {
    try {
      localStorage.setItem(PAGE_KEY, String(n));
    } catch (error) {
      console.info("Heuhaufen: Seitengröße wird nicht gespeichert (" + error.name + ")");
    }
  }

  var pageSize = savedPageSize();

  function pagerOf(list) {
    var next = list.nextElementSibling;
    return next && next.hasAttribute("data-pager") ? next : null;
  }

  // Seitenzahlen: erste, letzte und die beiden Nachbarn der aktuellen, dazwischen „…“
  function pageNumbers(page, pages) {
    var out = [];
    for (var p = 1; p <= pages; p++) {
      if (p === 1 || p === pages || Math.abs(p - page) <= 1) {
        out.push(p);
      } else if (out[out.length - 1] !== 0) {
        out.push(0);
      }
    }
    return out;
  }

  function pageButton(pager, text, label, target, current) {
    var button = pager.querySelector("template[data-page-button]").content.firstElementChild.cloneNode(true);
    button.textContent = text;
    button.setAttribute("aria-label", label);
    if (current) button.setAttribute("aria-current", "page");
    if (target === null) {
      button.disabled = true;
    } else {
      button.dataset.page = String(target);
    }
    return button;
  }

  function paginate(list, page) {
    var pager = pagerOf(list);
    if (!pager) return;
    var rows = list.querySelectorAll(":scope > li[data-offer]");
    var pages = Math.max(1, Math.ceil(rows.length / pageSize));
    var current = Math.min(Math.max(1, page || Number(list.dataset.page) || 1), pages);
    list.dataset.page = String(current);
    rows.forEach(function (row, i) {
      row.hidden = i < (current - 1) * pageSize || i >= current * pageSize;
    });

    pager.hidden = rows.length <= pageSize;
    pager.querySelector("[data-page-size]").value = String(pageSize);
    var first = (current - 1) * pageSize + 1;
    pager.querySelector("[data-range]").textContent =
      first + "–" + Math.min(current * pageSize, rows.length) + " von " + rows.length;

    var box = pager.querySelector("[data-pages]");
    box.textContent = "";
    box.appendChild(pageButton(pager, "‹", "Vorige Seite", current > 1 ? current - 1 : null, false));
    pageNumbers(current, pages).forEach(function (p) {
      if (p === 0) {
        box.appendChild(pager.querySelector("template[data-page-gap]").content.firstElementChild.cloneNode(true));
      } else {
        box.appendChild(pageButton(pager, String(p), "Seite " + p, p, p === current));
      }
    });
    box.appendChild(pageButton(pager, "›", "Nächste Seite", current < pages ? current + 1 : null, false));
  }

  function bindPagers(root) {
    root.querySelectorAll("ol[data-sortable]").forEach(function (list) {
      var pager = pagerOf(list);
      if (!pager) return;
      pager.querySelector("[data-pages]").addEventListener("click", function (event) {
        var button = event.target.closest("button[data-page]");
        if (!button) return;
        paginate(list, Number(button.dataset.page));
        if (list.getBoundingClientRect().top < 0) list.scrollIntoView();
      });
      pager.querySelector("[data-page-size]").addEventListener("change", function (event) {
        var n = parseInt(event.target.value, 10);
        if (!(n >= 1)) n = DEFAULT_PAGE_SIZE;
        pageSize = Math.min(200, n);
        rememberPageSize(pageSize);
        document.querySelectorAll("ol[data-sortable]").forEach(function (other) { paginate(other, 1); });
      });
      paginate(list, 1);
    });
  }

  bindPagers(document);

  // ------------------------------------------------------------------
  // Detailseite: Angebote sortieren (wird auf diesem Gerät gemerkt).
  // Jede Zeile trägt data-sort-end, data-sort-price, data-sort-new und data-sort-distance.

  var SORT_KEY = "heuhaufen-sort";
  var sortControl = document.querySelector("[data-sort-control]");
  var sortButtons = document.querySelectorAll("button[data-sort]");

  function rememberSort(key) {
    try {
      localStorage.setItem(SORT_KEY, key);
    } catch (error) {
      console.info("Heuhaufen: Sortierung wird nicht gespeichert (" + error.name + ")");
    }
  }

  function savedSort() {
    try {
      return localStorage.getItem(SORT_KEY);
    } catch (error) {
      console.info("Heuhaufen: gespeicherte Sortierung nicht lesbar (" + error.name + ")");
      return null;
    }
  }

  // Leere Werte (z. B. keine Endzeit) kommen immer ans Ende
  function sortValue(el, name, descending) {
    var raw = el.getAttribute("data-sort-" + name);
    if (raw === null || raw === "") return Infinity;
    var value = Number(raw);
    return descending ? -value : value;
  }

  var SORTS = {
    end: [["end", false], ["price", false]],
    price: [["price", false], ["end", false]],
    "new": [["new", true], ["end", false]],
    distance: [["distance", false], ["end", false]]
  };

  function compareBy(rules) {
    return function (a, b) {
      for (var i = 0; i < rules.length; i++) {
        var x = sortValue(a, rules[i][0], rules[i][1]);
        var y = sortValue(b, rules[i][0], rules[i][1]);
        if (x !== y) return x < y ? -1 : 1;
      }
      return 0;
    };
  }

  function currentSort() {
    var pressed = document.querySelector('button[data-sort][aria-pressed="true"]');
    return pressed ? pressed.dataset.sort : "end";
  }

  function applySort(key, root) {
    var rules = SORTS[key] || SORTS.end;
    (root || document).querySelectorAll("ol[data-sortable]").forEach(function (list) {
      var rows = Array.prototype.slice.call(list.querySelectorAll(":scope > li[data-offer]"));
      rows.sort(compareBy(rules)).forEach(function (row) { list.appendChild(row); });
      paginate(list, 1);
    });
  }

  function setSort(key) {
    sortButtons.forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.sort === key)); });
    applySort(key);
    rememberSort(key);
  }

  if (sortControl) {
    sortControl.hidden = false;
    var wanted = savedSort();
    var available = Array.prototype.some.call(sortButtons, function (b) { return b.dataset.sort === wanted; });
    if (wanted && wanted !== "end" && available) setSort(wanted);
    sortButtons.forEach(function (b) {
      b.addEventListener("click", function () { setSort(b.dataset.sort); });
    });
  }

  // ------------------------------------------------------------------
  // Eingeklappte Listen („Mit Versand über dem Limit“, „Ausgeblendet“) erst beim Aufklappen laden

  function loadFold(fold) {
    if (fold.hasAttribute("data-loaded")) return;
    var target = fold.querySelector("[data-lazy-target]");
    fold.setAttribute("data-loaded", "");
    fetch(fold.dataset.lazy, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.text();
      })
      .then(function (html) {
        target.innerHTML = html;
        bindPagers(target);
        applySort(currentSort(), target);
        bindHideForms(target);
        tick();
      })
      .catch(function (error) {
        fold.removeAttribute("data-loaded");
        console.info("Heuhaufen: Liste konnte nicht geladen werden (" + error.message + ")");
      });
  }

  document.querySelectorAll("details[data-lazy]").forEach(function (fold) {
    fold.addEventListener("toggle", function () { if (fold.open) loadFold(fold); });
  });

  // ------------------------------------------------------------------
  // Ausblenden ohne Neuladen der Seite: Zeile entfernen und Zähler anpassen, danach einige Sekunden
  // „Rückgängig“ anbieten (search_detail.html, [data-undo]). Ausblenden geht auch in der Galerie.
  // Ohne JavaScript schickt das Formular ganz normal ab.

  var UNDO_SECONDS = 10;
  var undoTimer = null;

  function changeCount(name, delta) {
    var el = document.querySelector('[data-count="' + name + '"]');
    if (el) el.textContent = String(Math.max(0, Number(el.textContent) + delta));
  }

  // Liste „Ausgeblendet“: Zähler anpassen, ohne Einträge verstecken, beim nächsten Aufklappen neu laden
  function hiddenChanged(delta) {
    var fold = document.getElementById("ausgeblendet");
    if (!fold) return;
    changeCount("hidden", delta);
    var count = fold.querySelector('[data-count="hidden"]');
    fold.hidden = !count || Number(count.textContent) === 0;
    fold.removeAttribute("data-loaded");
    if (fold.open && !fold.hidden) loadFold(fold);
  }

  function post(url, form) {
    return fetch(url, { method: "POST", body: new FormData(form), credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
      });
  }

  function offerUndo(form, row, ol, next, list) {
    var box = document.querySelector("[data-undo]");
    if (!box) return;
    window.clearTimeout(undoTimer);
    box.hidden = false;
    box.querySelector("[data-undo-button]").onclick = function () {
      window.clearTimeout(undoTimer);
      box.hidden = true;
      post(form.action.replace(/\/hide$/, "/unhide"), form)
        .then(function () {
          // Zeile an ihren alten Platz zurück und deren Seite zeigen
          ol.insertBefore(row, next && next.parentNode === ol ? next : null);
          var rows = Array.prototype.slice.call(ol.querySelectorAll(":scope > li[data-offer]"));
          paginate(ol, Math.floor(rows.indexOf(row) / pageSize) + 1);
          if (list) changeCount(list.dataset.list, 1);
          hiddenChanged(-1);
          tick();
        })
        .catch(function () { window.location.reload(); });
    };
    undoTimer = window.setTimeout(function () { box.hidden = true; }, UNDO_SECONDS * 1000);
  }

  function hideOffer(form) {
    var row = form.closest("li");
    var ol = row.parentElement;
    var next = row.nextElementSibling;
    var list = row.closest("[data-list]");
    post(form.action, form)
      .then(function () {
        row.remove();
        paginate(ol);
        if (list) changeCount(list.dataset.list, -1);
        hiddenChanged(1);
        offerUndo(form, row, ol, next, list);
      })
      .catch(function () { form.submit(); });
  }

  function bindHideForms(root) {
    root.querySelectorAll("form[data-hide]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        hideOffer(form);
      });
    });
  }

  bindHideForms(document);

  // ------------------------------------------------------------------
  // Bilder-Galerie: Klick aufs Vorschaubild oder den Titel zeigt alle Bilder des Angebots (PhotoSwipe,
  // photoswipe.com). /galerie/… liefert Adressen und Maße der Bilder aus dem Bild-Cache. PhotoSwipe selbst
  // (js/photoswipe.js, gebündelt aus node_modules) lädt erst beim ersten Klick, die Adresse steht in
  // <body data-photoswipe>. Ohne JavaScript oder mit Befehls-/Strg-Taste führen Bild und Titel zur Plattform
  // (eBay …, Name in data-platform). In der Leiste oben: Ausblenden (wie das Auge in der Liste), Angaben
  // ein/aus, bei der Plattform öffnen.
  // Seitenleiste (includes/gallery_panel.html, kommt fertig mit /galerie/…): beim Öffnen eingeblendet,
  // der Knopf ⓘ blendet sie aus und ein. Ab 64rem Breite steht sie rechts, darunter unten als Schublade.

  var GALLERY_OPTIONS = {
    bgOpacity: 1,
    showHideAnimationType: "fade",
    wheelToZoom: true,
    tapAction: false,          // Tippen aufs Bild am Handy: nichts tun (Doppeltippen zoomt weiter)
    preload: [1, 2],
    closeTitle: "Schließen (Esc)",
    zoomTitle: "Vergrößern (Z)",
    arrowPrevTitle: "Vorheriges Bild",
    arrowNextTitle: "Nächstes Bild",
    errorMsg: "Dieses Bild lässt sich gerade nicht laden.",
    indexIndicatorSep: " / "
  };
  // Knopf „Bei eBay öffnen“ (bzw. der Plattform) oben in der Galerie (Pfeil aus dem Kasten, 32er-Raster wie die PhotoSwipe-Symbole)
  var EBAY_ICON = '<path id="pswp__icn-ebay" d="M9 11h6v2h-4v8h8v-4h2v6H9zM17 9h6v6h-2v-2.6l-5.3 5.3-1.4-1.4 5.3-5.3H17z"/>';
  var HIDE_ICON = '<g id="pswp__icn-hide"><path fill-rule="evenodd" d="M5.5 16c2.4-4.2 6-6.3 10.5-6.3s8.1 2.1 10.5 6.3c-2.4 4.2-6 6.3-10.5 6.3S7.9 20.2 5.5 16zm2.4 0c2 3 4.7 4.4 8.1 4.4s6.1-1.4 8.1-4.4c-2-3-4.7-4.4-8.1-4.4S9.9 13 7.9 16z"/><circle cx="16" cy="16" r="2.6"/><path d="M8.2 6.8l17 17-1.4 1.4-17-17z"/></g>';
  var INFO_ICON = '<path id="pswp__icn-info" fill-rule="evenodd" d="M16 6.5a9.5 9.5 0 1 1 0 19 9.5 9.5 0 0 1 0-19zm0 2a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15zM15 14h2v7h-2zm0-3.5h2v2h-2z"/>';
  var PANEL_WIDTH = 380;                                   // w-95 in gallery_panel.html
  var wideGallery = window.matchMedia("(min-width: 64rem)");   // lg: Leiste rechts statt unten
  var photoSwipe = null;

  function loadPhotoSwipe() {
    if (!photoSwipe) {
      photoSwipe = new window.Promise(function (resolve, reject) {
        var script = document.createElement("script");
        script.src = document.body.dataset.photoswipe;
        script.onload = function () {
          if (window.PhotoSwipe) resolve(window.PhotoSwipe);
          else reject(new Error("PhotoSwipe fehlt"));
        };
        script.onerror = function () {
          photoSwipe = null;
          reject(new Error("PhotoSwipe nicht geladen"));
        };
        document.head.appendChild(script);
      });
    }
    return photoSwipe;
  }

  function loadImages(link) {
    return fetch(link.dataset.gallery, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      });
  }

  // Am Handy ist die Seitenleiste eine Schublade am unteren Rand (includes/gallery_panel.html). s.top ist ihre
  // Oberkante: zwischen peekTop (nur der Kopf schaut heraus) und minTop (Galerie auf Mindesthöhe).
  var SHEET_MIN = 0.3;   // Galerie am Handy mindestens 30 % der Bildschirmhöhe

  function sheetBounds(pswp, s) {
    var height = pswp.element.clientHeight;
    var minTop = Math.round(height * SHEET_MIN);
    return { height: height, minTop: minTop, peekTop: Math.max(minTop, height - Math.min(s.peekHeight, height * 0.6)) };
  }

  // Platz fürs Bild: neben der Seitenleiste bzw. über der Schublade, damit sie nichts verdeckt
  function galleryPadding(pswp, s) {
    if (!s.panel || s.panel.hidden) return { top: 0, bottom: 0, left: 0, right: 0 };
    if (wideGallery.matches) return { top: 60, bottom: 16, left: 16, right: PANEL_WIDTH + 16 };
    return { top: 56, bottom: pswp.element.clientHeight - s.top + 8, left: 0, right: 0 };
  }

  function addPanel(pswp, html) {
    var s = { panel: null, body: null, peek: null, info: null, top: 0, peekHeight: 0, frame: 0, anim: 0, drag: null,
              zoomHidden: false, zoomLevel: null };
    pswp.options.paddingFn = function () { return galleryPadding(pswp, s); };

    // Bild an die Schublade anpassen, höchstens einmal je Bildschirmaufbau
    function followSheet() {
      if (s.frame) return;
      s.frame = window.requestAnimationFrame(function () {
        s.frame = 0;
        pswp.updateSize(true);
      });
    }

    function setTop(top) {
      var b = sheetBounds(pswp, s);
      s.top = Math.min(b.peekTop, Math.max(b.minTop, top));
      s.panel.style.height = (b.height - s.top) + "px";
      followSheet();
    }

    function animateTo(target) {
      window.cancelAnimationFrame(s.anim);
      var from = s.top;
      var start = null;
      function step(time) {
        if (start === null) start = time;
        var p = Math.min(1, (time - start) / 260);
        setTop(from + (target - from) * (1 - Math.pow(1 - p, 3)));
        if (p < 1) s.anim = window.requestAnimationFrame(step);
      }
      s.anim = window.requestAnimationFrame(step);
    }

    // Größe neu bestimmen (Öffnen, Drehen, Fenstergröße, Ein- und Ausblenden): am Rechner Leiste rechts, am
    // Handy Schublade. Die Schublade bleibt, wo sie war; nur beim ersten Mal schaut nur der Kopf heraus.
    function relayout() {
      if (!s.panel) return;
      if (wideGallery.matches) {
        s.panel.style.height = "";
      } else if (!s.panel.hidden) {
        var first = !s.peekHeight;
        s.peekHeight = s.peek.getBoundingClientRect().bottom - s.panel.getBoundingClientRect().top + s.body.scrollTop + 20;
        var b = sheetBounds(pswp, s);
        s.top = first ? b.peekTop : Math.min(b.peekTop, Math.max(b.minTop, s.top));
        s.panel.style.height = (b.height - s.top) + "px";
      }
      pswp.updateSize(true);
    }

    function showPanel(show) {
      s.panel.hidden = !show;
      s.info.setAttribute("aria-pressed", String(show));
      pswp.element.dataset.panel = show ? "open" : "closed";
      relayout();
    }

    // Lupe (Knopf oder Taste Z): Zum Vergrößern verschwindet die Leiste, das Bild bekommt den ganzen Bildschirm.
    // Beim Verkleinern kommt sie so zurück, wie sie war. Ebenso, wenn man anders zurückzoomt (Doppeltippen,
    // Finger, Mausrad) oder zum nächsten Bild blättert.
    var toggleZoom = pswp.toggleZoom;
    pswp.toggleZoom = function () {
      var slide = pswp.currSlide;
      if (slide && s.panel) {
        var zoomedIn = slide.currZoomLevel !== slide.zoomLevels.initial;
        if (!zoomedIn && !s.panel.hidden) {
          s.zoomHidden = true;
          showPanel(false);
        } else if (zoomedIn && s.zoomHidden) {
          s.zoomHidden = false;
          showPanel(true);
        }
      }
      toggleZoom.call(pswp);
    };

    function restoreAfterZoom() {
      if (!s.zoomHidden) return;
      s.zoomHidden = false;
      window.requestAnimationFrame(function () { showPanel(true); });   // nicht mitten in PhotoSwipes Animation
    }

    pswp.on("zoomPanUpdate", function (event) {
      var level = event.slide.currZoomLevel;
      var initial = event.slide.zoomLevels.initial;
      if (s.zoomLevel !== null && s.zoomLevel > initial + 0.001 && level <= initial + 0.001) restoreAfterZoom();
      s.zoomLevel = level;
    });
    pswp.on("change", function () {
      s.zoomLevel = null;
      restoreAfterZoom();
    });

    // Wischen auf der Schublade: erst die Schublade schieben, ist sie oben, den Text scrollen (das macht der
    // Browser). Zurück nach unten geht es, sobald der Text wieder am Anfang steht.
    function onTouchStart(event) {
      if (wideGallery.matches || event.touches.length !== 1) { s.drag = null; return; }
      window.cancelAnimationFrame(s.anim);
      var y = event.touches[0].clientY;
      s.drag = { y0: y, top0: s.top, scroll0: s.body.scrollTop, mode: "", samples: [{ y: y, t: event.timeStamp }] };
    }

    function onTouchMove(event) {
      var d = s.drag;
      if (!d) return;
      var y = event.touches[0].clientY;
      var b = sheetBounds(pswp, s);
      if (!d.mode) {
        // gleich bei der ersten Bewegung entscheiden, sonst fängt Safari schon an, den Text zu scrollen
        if (y === d.y0) return;
        var open = s.top <= b.minTop + 1;
        d.mode = !open || (y > d.y0 && s.body.scrollTop <= 0) ? "sheet" : "text";
      }
      if (d.mode !== "sheet") return;
      event.preventDefault();
      var want = d.top0 + (y - d.y0);
      if (want < b.minTop) s.body.scrollTop = d.scroll0 + (b.minTop - want);   // oben angekommen: Text weiter
      setTop(want);
      d.samples.push({ y: y, t: event.timeStamp });
      if (d.samples.length > 5) d.samples.shift();
    }

    function onTouchEnd() {
      var d = s.drag;
      s.drag = null;
      if (!d || d.mode !== "sheet" || d.samples.length < 2) return;
      // Schwung: kräftig nach oben oder unten gewischt, dann ganz auf bzw. zu
      var first = d.samples[0];
      var last = d.samples[d.samples.length - 1];
      var speed = (last.y - first.y) / Math.max(1, last.t - first.t);
      var b = sheetBounds(pswp, s);
      if (speed < -0.4 && s.top > b.minTop) animateTo(b.minTop);
      else if (speed > 0.4 && s.top < b.peekTop) animateTo(b.peekTop);
    }

    // Mausrad (schmales Fenster am Rechner): wie Wischen. Nie an PhotoSwipe weitergeben, sonst zoomt das Bild.
    function onWheel(event) {
      event.stopPropagation();
      if (wideGallery.matches) return;
      var b = sheetBounds(pswp, s);
      if ((event.deltaY > 0 && s.top > b.minTop) || (event.deltaY < 0 && s.body.scrollTop <= 0 && s.top < b.peekTop)) {
        event.preventDefault();
        setTop(s.top - event.deltaY);
      }
    }

    pswp.on("uiRegister", function () {
      pswp.ui.registerElement({
        name: "info",
        order: 8,
        isButton: true,
        title: "Angaben ein- oder ausblenden",
        ariaLabel: "Angaben zum Angebot ein- oder ausblenden",
        html: { isCustomSVG: true, inner: INFO_ICON, outlineID: "pswp__icn-info" },
        onInit: function (el) {
          s.info = el;
          el.setAttribute("aria-pressed", "true");
        },
        onClick: function () {
          s.zoomHidden = false;   // von Hand ein- oder ausgeblendet: so bleibt es
          showPanel(s.panel.hidden);
        }
      });
      pswp.ui.registerElement({
        name: "panel",
        appendTo: "root",
        onInit: function (el) {
          el.innerHTML = html;
          s.panel = el.querySelector("[data-gallery-panel]");
          s.body = el.querySelector("[data-sheet-body]");
          s.peek = el.querySelector("[data-sheet-peek]");
          pswp.element.dataset.panel = "open";
          s.panel.addEventListener("touchstart", onTouchStart, { passive: true });
          s.panel.addEventListener("touchmove", onTouchMove, { passive: false });
          s.panel.addEventListener("touchend", onTouchEnd);
          s.panel.addEventListener("touchcancel", onTouchEnd);
          s.panel.addEventListener("wheel", onWheel, { passive: false });
          // Griff: ganz hochschieben bzw. zurück zur großen Galerie
          el.querySelector("[data-sheet-handle]").addEventListener("click", function () {
            var b = sheetBounds(pswp, s);
            animateTo(s.top > (b.minTop + b.peekTop) / 2 ? b.minTop : b.peekTop);
          });
          if (el.isConnected) relayout();
          tick();
        }
      });
    });
    // Nach dem Einblenden noch einmal messen: während der Öffnen-Animation passt PhotoSwipe die Bildgröße nicht an
    pswp.on("afterInit", function () { window.requestAnimationFrame(relayout); });
    pswp.on("openingAnimationEnd", relayout);
    wideGallery.addEventListener("change", relayout);
    window.addEventListener("resize", relayout);
    pswp.on("destroy", function () {
      window.cancelAnimationFrame(s.anim);
      wideGallery.removeEventListener("change", relayout);
      window.removeEventListener("resize", relayout);
    });
  }

  function platformName(link) {
    return link.getAttribute("data-platform") || "eBay";
  }

  // Vorschaubild und Titel einer Zeile öffnen dieselbe Galerie (includes/offer.html)
  function openGallery(link) {
    var row = link.closest("li[data-offer]");
    var links = row ? Array.prototype.slice.call(row.querySelectorAll("a[data-gallery]")) : [link];
    var thumb = row ? row.querySelector("a[data-gallery] img") : link.querySelector("img");
    var hideForm = row ? row.querySelector("form[data-hide]") : null;
    links.forEach(function (a) { a.setAttribute("aria-busy", "true"); });
    window.Promise.all([loadPhotoSwipe(), loadImages(link)])
      .then(function (results) {
        var PhotoSwipe = results[0];
        var items = results[1].items || [];
        if (!items.length && !results[1].panel) throw new Error("keine Bilder");
        if (!items.length) {   // keine Bilder, aber Beschreibung: Galerie trotzdem öffnen
          items = [{ html: '<p class="grid h-full place-items-center px-6 text-center text-ui text-white/70">Zu diesem Angebot liefert ' + platformName(link) + ' gerade keine Bilder.</p>' }];
        } else if (thumb) {
          items[0].msrc = thumb.currentSrc || thumb.src;   // Vorschaubild, bis das große Bild da ist
        }
        var pswp = new PhotoSwipe(Object.assign({ dataSource: items }, GALLERY_OPTIONS));
        if (results[1].panel) addPanel(pswp, results[1].panel);
        pswp.on("uiRegister", function () {
          if (hideForm) {
            // Ausblenden: Galerie schließen, Zeile entfernen, unten „Rückgängig“ anbieten
            pswp.ui.registerElement({
              name: "hide",
              order: 7,
              isButton: true,
              title: "Ausblenden",
              ariaLabel: "Angebot ausblenden",
              html: { isCustomSVG: true, inner: HIDE_ICON, outlineID: "pswp__icn-hide" },
              onClick: function () {
                pswp.close();
                hideOffer(hideForm);
              }
            });
          }
          pswp.ui.registerElement({
            name: "ebay",
            order: 9,
            isButton: true,
            tagName: "a",
            title: "Bei " + platformName(link) + " öffnen",
            ariaLabel: "Bei " + platformName(link) + " öffnen",
            html: { isCustomSVG: true, inner: EBAY_ICON, outlineID: "pswp__icn-ebay" },
            onInit: function (el) {
              el.href = link.href;
              el.target = "_blank";
              el.rel = "noopener noreferrer";
            }
          });
        });
        pswp.init();
      })
      .catch(function (error) {
        // Ohne Galerie wenigstens das Angebot bei der Plattform zeigen, wie ohne JavaScript
        console.info("Heuhaufen: Galerie nicht verfügbar (" + error.message + ")");
        var tab = window.open(link.href, "_blank");
        if (tab) tab.opener = null;
        else window.location.assign(link.href);   // Pop-up gesperrt: dann eben in diesem Tab
      })
      .then(function () { links.forEach(function (a) { a.removeAttribute("aria-busy"); }); });
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[data-gallery]") : null;
    if (!link || !document.body.dataset.photoswipe) return;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;   // neuer Tab: Plattform
    event.preventDefault();
    if (link.getAttribute("aria-busy") !== "true") openGallery(link);
  });

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

  // ------------------------------------------------------------------
  // Nutzer: Text der Einladung kopieren (Knopf erscheint nur mit JavaScript)

  function copyText(field) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(field.value);
    }
    return new Promise(function (resolve, reject) {   // http im Heimnetz: Clipboard-API fehlt
      field.focus();
      field.select();
      if (document.execCommand("copy")) resolve(); else reject(new Error("copy"));
    });
  }

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    var field = document.getElementById(button.dataset.copy);
    var done = button.parentNode.querySelector("[data-copy-done]");
    if (!field) return;
    button.hidden = false;
    button.addEventListener("click", function () {
      copyText(field)
        .then(function () {
          if (done) {
            done.hidden = false;
            window.setTimeout(function () { done.hidden = true; }, 2500);
          }
        })
        .catch(function (error) {
          console.info("Kopieren nicht möglich, Text ist markiert:", error);
          field.focus();
          field.select();
        });
    });
  });
})();
