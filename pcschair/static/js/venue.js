// Live venue view, shared by the attendee and admin pages.
//
// The server pushes the venue's full state over a websocket whenever anything
// changes; this script renders it and sends user actions back over the socket.
(function () {
  "use strict";

  var config = window.PCSCHAIR;
  var AC_PAPERS_KEY = "pcsChairACPapers";
  var PCS2_BASE = "https://new.precisionconference.com/";

  var notifyAudio = new Audio(config.sounds.notify);
  var timerAudio = new Audio(config.sounds.timer);

  var state = null;          // latest {venue, papers} from the server
  var timerMs = null;        // state.venue.timer as epoch ms
  var clockOffset = 0;       // server clock minus client clock, in ms
  var alarmPlayedFor = null; // timer value whose expiry sound has already played

  // Websocket only: no long-polling fallback, so no sticky sessions are needed.
  var socket = io({ transports: ["websocket"] });

  // ---- Helpers ----

  function byId(id) {
    return document.getElementById(id);
  }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function play(audio) {
    var promise = audio.play();
    if (promise !== undefined) {
      promise.catch(function () {
        // Autoplay was prevented
      });
    }
  }

  function serverNow() {
    return Date.now() + clockOffset;
  }

  function readInt(input) {
    var value = parseInt(input.value, 10);
    return Number.isInteger(value) ? value : null;
  }

  function onClick(id, handler) {
    var node = byId(id);
    if (node) node.addEventListener("click", handler);
  }

  function showError(message) {
    var box = byId("action-error");
    box.textContent = message;
    box.classList.remove("hidden");
    clearTimeout(showError.timeout);
    showError.timeout = setTimeout(function () { box.classList.add("hidden"); }, 5000);
  }

  // Link to the paper on PCS 2, or null if the venue isn't configured for it.
  function paperUrl(paperId) {
    var venue = state.venue;
    if (!venue.pcs2_flag || !venue.pcs2_venue_name) return null;
    var role = config.admin
      ? (venue.sub_committee ? "/subchair" + venue.sub_committee_id : "/chair")
      : "/committee";
    return PCS2_BASE + venue.pcs2_venue_name + role + "/subs/" + paperId;
  }

  function paperLabel(paperId) {
    var url = paperUrl(paperId);
    var label = make(url ? "a" : "span", "paperLink", String(paperId));
    if (url) {
      label.href = url;
      label.target = "_pcs_paper";
    }
    return label;
  }

  // ---- AC papers (attendee page; stored only in this browser) ----

  function getACPapers() {
    try {
      var stored = localStorage.getItem(AC_PAPERS_KEY);
      return stored ? JSON.parse(stored) : [];
    } catch (e) {
      return [];
    }
  }

  function setACPapers(acPapers) {
    try {
      if (acPapers.length) localStorage.setItem(AC_PAPERS_KEY, JSON.stringify(acPapers));
      else localStorage.removeItem(AC_PAPERS_KEY);
    } catch (e) {
      // Storage unavailable; the list just won't persist
    }
    renderACPapers();
    if (state) renderQueue();
  }

  function renderACPapers() {
    var list = byId("acPaperList");
    list.textContent = "";
    getACPapers().forEach(function (paperId) {
      var item = make("li", "paper", String(paperId));
      var remove = make("button", "control removeimg");
      remove.setAttribute("aria-label", "Remove " + paperId + " from AC papers");
      remove.addEventListener("click", function () {
        setACPapers(getACPapers().filter(function (id) { return id !== paperId; }));
      });
      item.append(" ", remove);
      list.appendChild(item);
    });
  }

  // ---- Rendering ----

  function applyState(next) {
    var prev = state && state.venue;
    var venue = next.venue;

    if (prev && prev.active_paper) {
      var cleared = !venue.active_paper && !venue.breaktime;
      var switched = venue.active_paper && prev.paper_pcs_id !== venue.paper_pcs_id;
      if (cleared || switched) play(notifyAudio);
    }

    state = next;
    timerMs = venue.timer ? Date.parse(venue.timer) : null;
    render();
  }

  function render() {
    var venue = state.venue;

    byId("venue-name").textContent = venue.name || "";
    byId("break").classList.toggle("invisible", !venue.breaktime);
    byId("main").classList.toggle("invisible", !!venue.breaktime);

    if (config.admin) {
      byId("allowUserAdds").checked = !!venue.allow_adds;
      byId("scalert").classList.toggle("hidden", !(venue.sub_committee && venue.sub_committee_id < 0));
    } else {
      byId("add-paper").classList.toggle("hidden", !venue.allow_adds);
      byId("pcs2flag").textContent = venue.pcs2_flag ? "true" : "false";
      byId("pcs2venueName").textContent = venue.pcs2_venue_name || "";
    }

    renderPaperInfo();
    renderQueue();
    tick();
  }

  function renderPaperInfo() {
    var venue = state.venue;
    var info = byId("paper-info");
    info.textContent = "";

    if (!venue.active_paper) {
      info.appendChild(make("p", "no-active-paper", "No Active Paper"));
      return;
    }

    var id = make("p", "paper-id");
    var url = paperUrl(venue.paper_pcs_id);
    if (url) {
      var link = make("a", null, "Paper #" + venue.paper_pcs_id);
      link.href = url;
      link.target = "_pcs_paper";
      id.appendChild(link);
    } else {
      id.textContent = "Paper #" + venue.paper_pcs_id;
    }

    var authors = make("ul", "paper-authors");
    (venue.paper_authors || "").split("\n").forEach(function (author) {
      if (author) authors.appendChild(make("li", "author", author));
    });

    info.append(id, make("p", "paper-title", venue.paper_title || ""), authors);
  }

  function controlButton(imageClass, label, handler) {
    var button = make("button", "control " + imageClass);
    button.setAttribute("aria-label", label);
    button.addEventListener("click", handler);
    return button;
  }

  function renderQueue() {
    var papers = state.papers;
    var acPapers = config.admin ? [] : getACPapers();
    var queue = byId("paper-queue");

    byId("queue-count").textContent = String(papers.length);
    queue.textContent = "";

    papers.forEach(function (paper, i) {
      var paperId = paper.pcs_paper_id;
      var item = make("li", "paper");
      if (acPapers.indexOf(paperId) >= 0) item.classList.add("acPaper");

      if (config.admin) {
        item.appendChild(i > 0
          ? controlButton("upimg", "Move " + paperId + " up", function () { movePaper(paperId, true); })
          : make("span", "control clearimg"));
        item.appendChild(paperLabel(paperId));
        item.appendChild(i < papers.length - 1
          ? controlButton("downimg", "Move " + paperId + " down", function () { movePaper(paperId, false); })
          : make("span", "control clearimg"));
        item.append(" ");
        item.appendChild(controlButton("removeimg", "Remove " + paperId, function () { removePaper(paperId); }));
      } else {
        item.appendChild(paperLabel(paperId));
      }

      queue.appendChild(item);
    });
  }

  function tick() {
    var display = byId("timer");

    if (!state || timerMs === null || state.venue.breaktime) {
      display.classList.remove("expired");
      display.textContent = "0:00";
      return;
    }

    var diff = timerMs - serverNow();
    var expired = diff <= 0;
    var totalSeconds = Math.floor(Math.abs(diff) / 1000);
    var minutes = Math.floor(totalSeconds / 60);
    var seconds = totalSeconds % 60;

    display.classList.toggle("expired", expired);
    display.textContent = (expired ? "-" : "") + minutes + ":" + (seconds < 10 ? "0" : "") + seconds;

    // Sound once as the timer runs out, but not when loading a page whose timer expired long ago.
    if (expired && diff > -2000 && alarmPlayedFor !== timerMs) {
      alarmPlayedFor = timerMs;
      play(timerAudio);
    }
  }

  // ---- Actions ----

  function emit(event, payload) {
    payload.venue_id = config.venueId;
    socket.emit(event, payload, function (response) {
      if (response && response.error) showError(response.error);
    });
  }

  function updateVenue(data) {
    emit("update_venue", { venue: data });
  }

  function addPaper(pcsId) {
    emit("add_paper", { pcs_id: pcsId });
  }

  function removePaper(pcsId) {
    emit("remove_paper", { pcs_id: pcsId });
  }

  function movePaper(pcsId, up) {
    emit("move_paper", { pcs_id: pcsId, dir: up ? "up" : "down" });
  }

  // ---- Clock sync ----

  // NTP-style offset estimate over a few round trips, keeping the sample with the
  // smallest round-trip time since it bounds the error best.
  function syncClock() {
    var remaining = 5;
    var best = null;

    (function sample() {
      var t0 = Date.now();
      socket.emit("time", function (serverMs) {
        var t3 = Date.now();
        var roundTrip = t3 - t0;
        if (!best || roundTrip < best.roundTrip) {
          best = { roundTrip: roundTrip, offset: serverMs - (t0 + t3) / 2 };
        }
        if (--remaining > 0) {
          sample();
        } else {
          clockOffset = best.offset;
          console.log("Clock sync: round trip", best.roundTrip, "ms, offset", Math.round(best.offset), "ms");
        }
      });
    })();
  }

  // ---- Wiring ----

  socket.on("connect", function () {
    byId("connection-status").classList.add("hidden");
    // (Re)join on every connect so a dropped connection resumes with fresh state.
    socket.emit("join", { venue_id: config.venueId }, function (response) {
      if (response.error) showError(response.error);
      else applyState(response);
    });
    syncClock();
  });

  socket.on("disconnect", function () {
    byId("connection-status").classList.remove("hidden");
  });

  socket.on("state", applyState);

  // Returning via the back button can restore a stale clock; resync.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted && socket.connected) syncClock();
  });

  var volume = byId("volumeRange");
  function applyVolume() {
    notifyAudio.volume = timerAudio.volume = parseFloat(volume.value);
  }
  volume.addEventListener("input", applyVolume);
  applyVolume();

  onClick("addNewPaper", function () {
    var input = byId("newPaperId");
    var paperId = readInt(input);
    if (paperId !== null) {
      addPaper(paperId);
      input.value = "";
    }
  });

  if (config.admin) {
    onClick("takebreak", function () {
      updateVenue({ breaktime: true });
    });

    onClick("endbreak", function () {
      updateVenue({ breaktime: false, timer: null, active_paper: false, paper_pcs_id: 0 });
      play(notifyAudio);
    });

    onClick("clearTimer", function () {
      updateVenue({ timer: null });
    });

    onClick("setTimer", function () {
      var minutes = readInt(byId("timerNum"));
      if (minutes !== null) {
        updateVenue({ timer: new Date(serverNow() + minutes * 60 * 1000).toISOString() });
      }
    });

    onClick("clearActivePaper", function () {
      updateVenue({ active_paper: false, timer: null });
    });

    byId("allowUserAdds").addEventListener("change", function (event) {
      updateVenue({ allow_adds: event.target.checked });
    });

    onClick("submitSCID", function () {
      var input = byId("subcommitteeInput");
      var scId = readInt(input);
      if (scId === null) {
        input.value = "";
        input.placeholder = "Must be a number";
      } else {
        updateVenue({ sub_committee_id: scId });
      }
    });
  } else {
    onClick("addACPaper", function () {
      var input = byId("acPaperId");
      var paperId = readInt(input);
      if (paperId !== null) {
        var acPapers = getACPapers();
        if (acPapers.indexOf(paperId) < 0) acPapers.push(paperId);
        setACPapers(acPapers);
        input.value = "";
      }
    });

    onClick("clearAllACPapers", function () {
      setACPapers([]);
    });

    renderACPapers();
  }

  applyState(config.initialState);
  setInterval(tick, 250);
})();
