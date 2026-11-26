/* The Frame page on the hub: admins manage photos and TVs; guests see the QR code and their photos. */
(function () {
  "use strict";
  var F = window.Frame, root = F.root, esc = F.esc;
  var view = document.getElementById("fr-view");
  var fileInput = document.getElementById("fr-file");
  var state = null;
  var tab = "family";          // admin: family | guest
  var selected = {};           // admin: photo id -> true
  var uploads = [];            // [{progress, error}] while uploading
  var guestTvs = "all";        // guest: TVs for new photos
  var qrLoaded = "";
  // The hub's wall screen (wide, touch-only, like the hub keyboard's check) has no photos to pick.
  var mq = function (q) { return window.matchMedia && window.matchMedia(q).matches; };
  var onScreen = window.innerWidth >= 1280 && mq("(any-pointer: coarse)") && !mq("(any-hover: hover)");

  function load() {
    return F.api(root.dataset.stateUrl).then(function (s) {
      state = s;
      document.getElementById("fr-loading").hidden = true;
      render();
    }).catch(function (e) { F.toast(e.message, true); });
  }

  function render() {
    if (!state) return;
    if (state.role === "admin") view.innerHTML = adminHtml();
    else if (state.role === "guest") view.innerHTML = guestHtml();
    else view.innerHTML = noneHtml();
    loadQr();
  }

  function loadQr() {
    var box = document.getElementById("fr-qr");
    if (!box || !state.stay || !state.stay.link) return;
    if (qrLoaded === state.stay.link && box.dataset.done) return;
    qrLoaded = state.stay.link;
    box.innerHTML = '<img alt="QR code" src="' + root.dataset.qrUrl + "?v=" + encodeURIComponent(state.stay.link) + '">';
    box.dataset.done = "1";
  }

  function tile(p, opts) {
    var tags = F.tvNames(p, state.tvs).map(function (n) { return '<span class="fr-tag">' + esc(n) + "</span>"; }).join("");
    var pend = F.sending(p, state.tvs) ? '<div class="fr-pending">Sending to TVs…</div>' : "";
    return '<div class="fr-tile' + (opts.sel ? " sel" : "") + '" data-photo="' + esc(p.id) + '" style="background-image:url(\'' +
      esc(p.thumb) + '\')">' + pend + (opts.check ? '<span class="fr-check"><i class="fas fa-check"></i></span>' : "") +
      '<button class="fr-x" data-remove="' + esc(p.id) + '" aria-label="Remove"><i class="fas fa-' +
      (opts.check ? "ellipsis-h" : "times") + '"></i></button><div class="fr-tags">' + tags + "</div></div>";
  }

  function uploadTiles() {
    return uploads.map(function (u) {
      return '<div class="fr-tile"><div class="fr-pending">' + (u.error ? esc(u.error) : Math.round(u.progress * 100) + "%") +
        '</div><div class="fr-bar" style="width:' + Math.round(u.progress * 100) + '%"></div></div>';
    }).join("");
  }

  function tvStatus(t) {
    if (!t.paired) return t.pairing
      ? '<span class="fr-dot warn"></span>Press <b>Allow</b> on the TV'
      : '<span class="fr-dot warn"></span>Not paired yet · Frame asks again when it\'s on';
    if (!t.online) return '<span class="fr-dot"></span>Off · photos go up when it\'s on';
    if (t.error) return '<span class="fr-dot bad"></span>' + esc(t.error);
    return '<span class="fr-dot ok"></span>On';
  }

  // --- admin -----------------------------------------------------------------------

  function adminHtml() {
    var fam = state.photos.filter(function (p) { return p.kind === "family"; });
    var gst = state.photos.filter(function (p) { return p.kind === "guest"; });
    var list = tab === "family" ? fam : gst;
    var h = "";
    if (state.stay) {
      h += '<div class="fr-banner"><i class="fas fa-lock"></i><div><b>Guest stay' +
        (state.stay.guest ? ": " + esc(state.stay.guest) : "") + (state.stay.checkout ? " until " + F.day(state.stay.checkout) : "") +
        ".</b> Your photos are off the TVs and come back after midnight" + (state.stay.checkout ? " " + F.day(state.stay.checkout) : "") +
        ". " + state.stay.photos + " guest photo" + (state.stay.photos === 1 ? "" : "s") + " will be deleted then.</div>" +
        '<button class="fr-btn" data-act="end-stay">End guest stay now</button></div>';
    }
    h += '<div class="fr-tabs"><button class="fr-tab' + (tab === "family" ? " on" : "") + '" data-tab="family">Family photos · ' + fam.length +
      '</button><button class="fr-tab' + (tab === "guest" ? " on" : "") + '" data-tab="guest">Guest photos · ' + gst.length + "</button></div>";
    h += '<div class="fr-cols"><section class="fr-card"><div class="fr-card-head"><div class="fr-grow">';
    if (tab === "family") {
      h += "<h2>Family photos</h2><p>Shown on the TVs when no guests are staying.</p></div>" +
        '<button class="fr-btn' + (onScreen ? " primary" : "") + '" data-act="family-qr"><i class="fas fa-qrcode"></i> Add from phone</button>' +
        (onScreen ? "" : '<button class="fr-btn primary" data-act="upload"><i class="fas fa-plus"></i> Upload</button>') + "</div>";
    } else {
      h += "<h2>Guest photos</h2><p>" + (state.stay ? "This stay's photos. They are deleted when it ends." : "No guests are staying.") +
        "</p></div>" + (state.stay && state.stay.link ? '<button class="fr-btn" data-act="guest-qr"><i class="fas fa-qrcode"></i> Guests\' QR code</button>' : "") + "</div>";
    }
    var tiles = (tab === "family" ? uploadTiles() : "") + list.map(function (p) {
      return tile(p, { check: true, sel: !!selected[p.id] });
    }).join("");
    h += tiles ? '<div class="fr-grid">' + tiles + "</div>"
      : '<div class="fr-empty">' + (tab === "family" ? (onScreen ? "No family photos yet. Add them from your phone." : "No family photos yet. Upload some, or add them from your phone.") : "No guest photos.") + "</div>";
    var sel = list.filter(function (p) { return selected[p.id]; });
    if (sel.length) {
      var common = sel.every(function (p) { return p.all_tvs; }) ? "all"
        : sel.length === 1 ? sel[0].tvs : null;
      h += '<div class="fr-selbar"><b>' + sel.length + " selected</b><span>Show on</span><div class=\"fr-chips\">" +
        F.chips(state.tvs, common || [], "data-target") + '</div><span class="fr-grow"></span>' +
        '<button class="fr-link" data-act="clear">Clear</button><button class="fr-btn danger" data-act="delete-sel">Delete</button></div>';
    }
    h += "</section>" + tvPanel() + "</div>";
    return h;
  }

  var INTERVALS = { "3": "3 min", "15": "15 min", "60": "1 hour", "720": "12 hours", "1440": "1 day" };

  function tvPanel() {
    var h = '<aside class="fr-card"><div class="fr-card-head"><h2 class="fr-grow">TVs</h2></div>';
    if (!state.tvs.length) h += '<div class="fr-empty">No Frame TVs found yet. Turn them on; Frame looks for them every 10 minutes.</div>';
    state.tvs.forEach(function (t) {
      h += '<div class="fr-tv"><div class="fr-tv-art"></div><div class="fr-tv-main"><div class="fr-tv-name">' + esc(t.name) +
        '</div><div class="fr-tv-sub">' + tvStatus(t) + " · " + esc(t.host) + "</div>" +
        (t.unmanaged ? '<div class="fr-tv-sub">' + t.unmanaged + " other photo" + (t.unmanaged === 1 ? "" : "s") +
          " in My Photos (not from Frame; left alone, and guests can see them)</div>" : "") +
        "</div></div>";
    });
    var s = state.settings || {};
    h += '<div class="fr-rows"><div class="fr-row"><span>Change photo every</span><span>' + esc(INTERVALS[s.interval] || s.interval + " min") +
      '</span></div><div class="fr-row"><span>Order</span><span>' + (s.shuffle ? "Shuffle" : "In order") +
      '</span></div><div class="fr-row"><span>Matte for new photos</span><span>' + esc(s.matte === "none" ? "None" : s.matte) +
      '</span></div><div class="fr-row"><span>Guest uploads</span><span>' + (state.guest_uploads ? "On during guest stays" : "Off") +
      '</span></div></div><p class="fr-note">Change these, and which TVs Frame uses, in Settings → Apps → Frame.</p></aside>';
    return h;
  }

  function showQr(url, title, text) {
    F.modal("<h3>" + esc(title) + "</h3><p>" + esc(text) + '</p><div class="fr-qr"><img alt="QR code" src="' + url +
      '"></div><div class="fr-modal-actions"><button class="fr-btn" data-close>Done</button></div>');
  }

  // --- guests ------------------------------------------------------------------------

  function guestHtml() {
    var s = state.stay || {};
    var h = '<div class="fr-guest"><section class="fr-card fr-qrcard"><h2>Your photos on the TV</h2>' +
      "<p>Scan with your phone to add photos to the TVs in Art Mode.</p>" +
      '<div class="fr-qr" id="fr-qr"></div><span class="fr-pill"><i class="fas fa-wifi"></i> Connect to the guest Wi-Fi first</span>' +
      '<div class="fr-privacy"><i class="fas fa-lock"></i> Only your group can see these photos. All of them are <b>permanently deleted</b> after checkout' +
      (s.checkout ? " on <b>" + esc(F.day(s.checkout)) + "</b>" : "") + ".</div></section><section>";
    h += '<div class="fr-card-head"><div class="fr-grow"><h2>Your group\'s photos · ' + state.photos.length +
      '</h2><p>Tap × to remove one from every TV.</p></div><span class="fr-pill"><i class="fas fa-sync-alt"></i> New photos appear here as they arrive</span></div>';
    var tiles = uploadTiles() + state.photos.map(function (p) { return tile(p, {}); }).join("");
    h += tiles ? '<div class="fr-grid">' + tiles + "</div>" : '<div class="fr-empty">No photos yet. Scan the code to add some from your phone.</div>';
    h += '<div class="fr-tvcards">' + state.tvs.map(function (t) {
      var st = !t.online ? '<span class="fr-dot"></span>Off · photos go up when it\'s on'
        : t.paired ? '<span class="fr-dot ok"></span>Art Mode' + (state.photos.length ? " · showing your photos" : "")
          : '<span class="fr-dot warn"></span>Not set up';
      return '<div class="fr-tv"><div class="fr-tv-art"></div><div class="fr-tv-main"><div class="fr-tv-name">' + esc(t.name) +
        '</div><div class="fr-tv-sub">' + st + "</div></div></div>";
    }).join("") + "</div></section></div>";
    return h;
  }

  function noneHtml() {
    return '<div class="fr-empty" style="margin-top:3rem"><i class="fa-regular fa-image" style="font-size:2.5rem"></i>' +
      "<p>The Frame TVs show photos from this app." + (state.stay ? "" : " An admin can sign in to manage them.") + "</p></div>";
  }

  // --- actions -----------------------------------------------------------------------

  function confirmRemove(id) {
    var p = state.photos.filter(function (x) { return x.id === id; })[0];
    if (!p) return;
    F.modal('<div style="display:flex;gap:1rem;align-items:center"><div class="fr-tile" style="width:8rem;background-image:url(\'' +
      esc(p.thumb) + '\')"></div><div><h3>Remove this photo?</h3><p>It comes off every TV right away' +
      (p.kind === "guest" ? " and is deleted." : " and is deleted from the hub.") + "</p></div></div>" +
      '<div class="fr-modal-actions"><button class="fr-btn" data-close>Keep it</button><button class="fr-btn danger solid" data-confirm-remove="' +
      esc(p.id) + '">Remove photo</button></div>');
  }

  function removePhotos(ids) {
    return Promise.all(ids.map(function (id) { return F.api(F.photoUrl(id), { op: "delete" }); }))
      .then(function () { selected = {}; F.toast(ids.length > 1 ? ids.length + " photos removed" : "Photo removed"); })
      .catch(function (e) { F.toast(e.message, true); })
      .then(load);
  }

  function startUpload(files) {
    if (!files.length) return;
    var base = uploads.length;
    for (var i = 0; i < files.length; i++) uploads.push({ progress: 0 });
    render();
    var errors = 0;
    F.uploadAll(files, state.role === "guest" ? (guestTvs === "all" ? "all" : guestTvs.join(",")) : "all", {
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
    if (d.tab) { tab = d.tab; selected = {}; render(); return; }
    if (d.remove) {
      e.stopPropagation();
      if (state.role === "admin") { selected = {}; selected[d.remove] = true; render(); }
      else confirmRemove(d.remove);
      return;
    }
    if (d.confirmRemove) { F.closeModal(); removePhotos([d.confirmRemove]); return; }
    if (d.photo && state.role === "admin") {
      if (selected[d.photo]) delete selected[d.photo]; else selected[d.photo] = true;
      render(); return;
    }
    if (d.target) {
      var ids = Object.keys(selected);
      var cur = "all";
      if (ids.length === 1) { var p = state.photos.filter(function (x) { return x.id === ids[0]; })[0]; cur = p && !p.all_tvs ? p.tvs : "all"; }
      var next = d.target === "all" ? "all" : F.toggle(cur === "all" ? [] : cur, d.target);
      Promise.all(ids.map(function (id) { return F.api(F.photoUrl(id), { op: "tvs", tvs: next }); }))
        .then(load).catch(function (err) { F.toast(err.message, true); });
      return;
    }
    switch (d.act) {
      case "upload": fileInput.click(); break;
      case "clear": selected = {}; render(); break;
      case "delete-sel":
        var ids2 = Object.keys(selected);
        if (window.confirm("Delete " + ids2.length + " photo" + (ids2.length === 1 ? "" : "s") + "? They come off every TV.")) removePhotos(ids2);
        break;
      case "family-qr":
        showQr(root.dataset.qrUrl + "?for=family&t=" + Date.now(), "Add from your phone",
          "Scan on the home Wi-Fi to upload family photos. The link works for 30 minutes.");
        break;
      case "guest-qr":
        showQr(root.dataset.qrUrl + "?t=" + Date.now(), "Guests' QR code", "The link guests use to add photos during this stay.");
        break;
      case "end-stay":
        if (!window.confirm("End this guest stay now? Its photos are deleted for good and your photos go back on the TVs.")) return;
        F.api(root.dataset.stayUrl, { op: "end" }).then(function () { F.toast("Guest stay ended"); load(); })
          .catch(function (err) { F.toast(err.message, true); });
        break;
    }
  });

  load();
  setInterval(function () { if (document.getElementById("fr-modal").hidden && !uploads.length) load(); }, 10000);
})();
