/* A guest's phone (or an admin's, for family photos): add, retarget and remove photos. */
(function () {
  "use strict";
  var F = window.Frame, root = F.root, esc = F.esc;
  var view = document.getElementById("fr-view");
  var sub = document.getElementById("fr-sub");
  var fileInput = document.getElementById("fr-file");
  var family = root.dataset.family === "1";
  var state = null, uploads = [], target = "all", sheet = null;

  function load() {
    return F.api(root.dataset.stateUrl).then(function (s) { state = s; render(); F.followPower(state.tvs, load); }).catch(function (e) {
      if (e.status === 404) { window.location.reload(); return; }  // stay over: the page says so
      F.toast(e.message, true);
    });
  }

  function render() {
    if (!state) return;
    var s = state.stay || {};
    sub.textContent = family ? "Shown on the TVs when no guests are staying."
      : "Shared with your group" + (s.guest ? ", " + s.guest : "") + ".";
    var h = "";
    if (state.tvs.length > 1) h += '<div class="fr-label">Show on</div><div class="fr-chips">' + F.chips(state.tvs, target, "data-target") + "</div>";
    h += '<button class="fr-add" data-act="add"><i class="fa-regular fa-images"></i><b>Add photos</b><span>from your library or camera</span></button>';
    h += '<div class="fr-label">' + (family ? "Family photos" : "Your group's photos") + " · " + state.photos.length + "</div>";
    var tiles = uploads.map(function (u) {
      return '<div class="fr-tile"><div class="fr-pending">' + (u.error ? esc(u.error) : Math.round(u.progress * 100) + "%") +
        '</div><div class="fr-bar" style="width:' + Math.round(u.progress * 100) + '%"></div></div>';
    }).join("") + state.photos.map(function (p) {
      var names = F.tvNames(p, state.tvs);
      var tag = names.length > 1 ? names.length + " TVs" : names[0] || "";
      return '<div class="fr-tile" data-photo="' + esc(p.id) + '" style="background-image:url(\'' + esc(p.thumb) + '\')">' +
        (F.sending(p, state.tvs) ? '<div class="fr-pending">Sending…</div>' : "") +
        '<button class="fr-x" data-photo="' + esc(p.id) + '" aria-label="Remove"><i class="fas fa-times"></i></button>' +
        (tag ? '<div class="fr-tags"><span class="fr-tag">' + esc(tag) + "</span></div>" : "") + "</div>";
    }).join("");
    h += tiles ? '<div class="fr-grid">' + tiles + "</div>" : '<div class="fr-empty">No photos yet.</div>';
    if (!family) {
      h += '<div class="fr-privacy"><i class="fas fa-lock"></i> All photos are <b>permanently deleted</b> after checkout' +
        (s.checkout ? " on " + esc(F.day(s.checkout)) : "") + ". Nobody outside your group sees them.</div>";
    }
    if (state.intervals) {
      h += '<div class="fr-label">Change photo every</div><div class="fr-chips">' + state.intervals.map(function (c) {
        return '<button class="fr-chip' + (c[0] === state.interval ? " on" : "") + '" data-interval="' + esc(c[0]) + '">' + esc(c[1]) + "</button>";
      }).join("") + "</div>";
    }
    if (state.tvs.length) {
      h += '<div class="fr-label">TVs</div>' + state.tvs.map(function (t) {
        var note = F.powerNote(t);
        return '<div class="fr-tv"><div class="fr-tv-main"><div class="fr-tv-name">' + esc(t.name) + '</div><div class="fr-tv-sub">' +
          (note ? '<span class="fr-dot' + (t.powering ? " warn" : " bad") + '"></span>' + esc(note)
            : t.asking_allow ? '<span class="fr-dot warn"></span>Press <b>Allow</b> on the TV'
            : !t.online ? '<span class="fr-dot"></span>Off' : t.uploading ? '<span class="fr-dot ok"></span>' + F.upload(t)
            : '<span class="fr-dot ok"></span>On') + "</div></div>" + F.power(t) + F.play(t) + "</div>";
      }).join("");
    }
    view.innerHTML = h;
  }

  function restart(id) {
    var t = state.tvs.filter(function (x) { return String(x.id) === id; })[0];
    F.api(root.dataset.restartUrl, { tv: Number(id) }).then(function () { F.toast("Starting the slideshow" + (t ? " on " + t.name : "")); })
      .catch(function (e) { F.toast(e.message, true); });
  }

  function changeInterval(v) {
    if (v === state.interval) return;
    var was = state.interval;
    state.interval = v;
    render();
    F.api(root.dataset.intervalUrl, { interval: v }).then(function () { F.toast("Saved"); })
      .catch(function (e) { state.interval = was; render(); F.toast(e.message, true); });
  }

  function openSheet(id) {
    var p = state.photos.filter(function (x) { return x.id === id; })[0];
    if (!p) return;
    sheet = { id: id, tvs: p.all_tvs ? "all" : p.tvs.slice() };
    drawSheet(p);
  }

  function drawSheet(p) {
    F.modal('<div style="display:flex;gap:1rem;align-items:center"><div class="fr-tile fr-zoom" data-view="' + esc(p.id) +
      '" title="View bigger" style="width:7rem;flex:none;background-image:url(\'' + esc(p.thumb) + '\')"></div><div><h3>Remove this photo?</h3><p>It comes off every TV right away.</p></div></div>' +
      (state.tvs.length > 1 ? '<div class="fr-label">Shown on</div><div class="fr-chips">' + F.chips(state.tvs, sheet.tvs, "data-sheet-tv") + "</div>" : "") +
      '<button class="fr-btn danger solid block" data-act="remove">Remove photo</button>' +
      '<button class="fr-btn block" data-close>Keep it</button>');
  }

  function startUpload(files) {
    if (!files.length) return;
    var base = uploads.length;
    files.forEach(function () { uploads.push({ progress: 0 }); });
    render();
    var errors = 0;
    F.uploadAll(files, target === "all" ? "all" : target.join(","), {
      progress: function (n, f) { uploads[base + n].progress = f; render(); },
      done: function (n, p) { uploads[base + n].done = true; if (p) state.photos.unshift(p); },
      fail: function (n, e) { errors++; uploads[base + n].done = true; F.toast(e.message, true); },
    }).then(function () {
      uploads = uploads.filter(function (u) { return !u.done; });
      if (!errors) F.toast(files.length > 1 ? files.length + " photos added" : "Photo added");
      load();
    });
  }

  fileInput.addEventListener("change", function () {
    var files = Array.prototype.slice.call(fileInput.files);
    fileInput.value = "";
    startUpload(files);
  });

  root.addEventListener("click", function (e) {
    var el = e.target.closest("button, .fr-tile");
    if (!el) return;
    var d = el.dataset;
    if (d.target) { target = F.toggle(target, d.target); render(); return; }
    if (d.interval) { changeInterval(d.interval); return; }
    if (d.restart) { restart(d.restart); return; }
    if (d.power) { F.switchPower(state.tvs, d.power, d.on === "1", render).then(load); return; }
    if (d.view) {
      var vp = state.photos.filter(function (x) { return x.id === d.view; })[0];
      if (vp) F.viewer(vp, el);
      return;
    }
    if (d.photo) { e.stopPropagation(); openSheet(d.photo); return; }
    if (d.sheetTv && sheet) {
      sheet.tvs = F.toggle(sheet.tvs, d.sheetTv);
      F.api(F.photoUrl(sheet.id), { op: "tvs", tvs: sheet.tvs }).then(function (r) {
        state.photos = state.photos.map(function (x) { return x.id === r.photo.id ? r.photo : x; });
        drawSheet(r.photo); render();
      }).catch(function (err) { F.toast(err.message, true); });
      return;
    }
    if (d.act === "add") { fileInput.click(); return; }
    if (d.act === "remove" && sheet) {
      var id = sheet.id;
      F.closeModal();
      F.api(F.photoUrl(id), { op: "delete" }).then(function () { F.toast("Photo removed"); })
        .catch(function (err) { F.toast(err.message, true); }).then(load);
    }
  });

  load();
  setInterval(function () { if (document.getElementById("fr-modal").hidden && !uploads.length) load(); }, 15000);
})();
