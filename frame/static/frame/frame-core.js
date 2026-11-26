/* Shared by the hub page (frame.js) and the phone page (guest.js). */
(function () {
  "use strict";
  var root = document.getElementById("fr");
  var MAX_W = 3840, MAX_H = 2160;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function toast(msg, bad) {
    var t = document.getElementById("fr-toast");
    t.textContent = msg;
    t.className = "fr-toast show" + (bad ? " bad" : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.className = "fr-toast"; }, 3500);
  }

  function api(url, body) {
    var opts = { credentials: "same-origin", headers: { "X-CSRFToken": root.dataset.csrf } };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) { var e = new Error(data.error || ("Error " + r.status)); e.status = r.status; throw e; }
        return data;
      });
    });
  }

  function photoUrl(id) { return root.dataset.photoUrl.replace("PHOTO", encodeURIComponent(id)); }

  /* Shrink to the TV's 4K on the phone: much faster on hotel-grade Wi-Fi, and
     under the hub's upload limit. Browsers apply EXIF rotation when decoding. */
  function shrink(file) {
    if (!window.createImageBitmap) return Promise.resolve(file);
    return createImageBitmap(file, { imageOrientation: "from-image" }).catch(function () {
      return createImageBitmap(file);
    }).then(function (bmp) {
      var w = bmp.width, h = bmp.height;
      var bw = w >= h ? MAX_W : MAX_H, bh = w >= h ? MAX_H : MAX_W;
      var s = Math.min(1, bw / w, bh / h);
      if (s === 1 && /jpe?g/i.test(file.type) && file.size < 12e6) { bmp.close && bmp.close(); return file; }
      var c = document.createElement("canvas");
      c.width = Math.round(w * s); c.height = Math.round(h * s);
      c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
      bmp.close && bmp.close();
      return new Promise(function (res) {
        c.toBlob(function (b) { res(b || file); }, "image/jpeg", 0.9);
      });
    }).catch(function () { return file; });  // let the hub try (and explain if it can't)
  }

  function uploadOne(file, tvs, onProgress) {
    return shrink(file).then(function (blob) {
      return new Promise(function (res, rej) {
        var fd = new FormData();
        fd.append("photo", blob, "photo.jpg");
        fd.append("tvs", tvs || "all");
        var x = new XMLHttpRequest();
        x.open("POST", root.dataset.uploadUrl);
        x.setRequestHeader("X-CSRFToken", root.dataset.csrf);
        x.upload.onprogress = function (e) { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
        x.onload = function () {
          var data = {};
          try { data = JSON.parse(x.responseText); } catch (e) { /* not JSON */ }
          if (x.status >= 200 && x.status < 300) res(data.photo);
          else rej(new Error(data.error || (x.status === 413 ? "That photo is too large" : "Upload failed")));
        };
        x.onerror = function () { rej(new Error("Upload failed: check the Wi-Fi")); };
        x.send(fd);
      });
    });
  }

  /* Upload files one by one; ``hooks`` gets start(i), progress(i, f), done(i, photo), fail(i, err). */
  function uploadAll(files, tvs, hooks) {
    var list = Array.prototype.slice.call(files);
    var i = 0;
    function next() {
      if (i >= list.length) return Promise.resolve();
      var n = i++;
      hooks.start && hooks.start(n);
      return uploadOne(list[n], tvs, function (f) { hooks.progress && hooks.progress(n, f); })
        .then(function (p) { hooks.done && hooks.done(n, p); }, function (e) { hooks.fail && hooks.fail(n, e); })
        .then(next);
    }
    return next();
  }

  // A TV's play button: starts (or restarts) its slideshow.
  function play(t) {
    var ok = t.online && t.paired;
    return '<button class="fr-play" data-restart="' + t.id + '"' + (ok ? "" : " disabled") +
      ' aria-label="Start the slideshow on ' + esc(t.name) + '"><i class="fas fa-play"></i></button>';
  }

  function tvNames(photo, tvs) {
    if (photo.all_tvs) return ["All TVs"];
    var byId = {};
    tvs.forEach(function (t) { byId[t.id] = t.name; });
    return photo.tvs.map(function (id) { return byId[id]; }).filter(Boolean);
  }

  /* Still going up: a TV it should be on is on and paired, but doesn't have it yet. */
  function sending(photo, tvs) {
    return tvs.some(function (t) {
      var target = photo.all_tvs || photo.tvs.indexOf(t.id) >= 0;
      return target && t.online && t.paired && photo.sent.indexOf(t.id) < 0;
    });
  }

  function day(iso) {
    if (!iso) return "";
    var d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }

  function chips(tvs, selected, attr) {
    var all = selected === "all";
    var h = '<button class="fr-chip' + (all ? " on" : "") + '" ' + attr + '="all">All TVs</button>';
    tvs.forEach(function (t) {
      var on = !all && selected.indexOf(t.id) >= 0;
      h += '<button class="fr-chip' + (on ? " on" : "") + '" ' + attr + '="' + t.id + '">' + esc(t.name) + "</button>";
    });
    return h;
  }

  /* Toggle a chip: "all", or a list of TV ids (never empty: falls back to "all"). */
  function toggle(selected, value) {
    if (value === "all") return "all";
    var id = Number(value);
    var cur = selected === "all" ? [] : selected.slice();
    var i = cur.indexOf(id);
    if (i >= 0) cur.splice(i, 1); else cur.push(id);
    return cur.length ? cur : "all";
  }

  function modal(html) {
    var m = document.getElementById("fr-modal");
    document.getElementById("fr-modal-card").innerHTML = html;
    m.hidden = false;
    return m;
  }

  function closeModal() { document.getElementById("fr-modal").hidden = true; }

  document.addEventListener("click", function (e) {
    var m = document.getElementById("fr-modal");
    if (e.target === m || (e.target.closest && e.target.closest("[data-close]"))) closeModal();
  });

  window.Frame = {
    root: root, esc: esc, toast: toast, api: api, photoUrl: photoUrl, uploadAll: uploadAll,
    tvNames: tvNames, play: play, sending: sending, day: day, chips: chips, toggle: toggle,
    modal: modal, closeModal: closeModal,
  };
})();
