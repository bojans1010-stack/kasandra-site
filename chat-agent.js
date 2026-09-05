/* Kasandra chat: receive human-agent (admin) replies into the widget, and
   pop the widget open for proactive messages to logged-in members. No PII. */
(function () {
  var LAST_KEY = "KSAGENT_LAST";
  function lastId() { return parseInt(localStorage.getItem(LAST_KEY) || "0", 10) || 0; }
  function setLast(n) { try { localStorage.setItem(LAST_KEY, String(n)); } catch (e) {} }

  // Is this visitor a logged-in member? (so we can deliver proactive messages
  // even before they've opened the chat)
  var isMember = false;
  try {
    fetch("/api/me").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { isMember = !!(d && d.ok && d.email); }).catch(function () {});
  } catch (e) {}

  function openWidget() {
    var win = document.getElementById("kas-win");
    if (win && win.classList && !win.classList.contains("open")) win.classList.add("open");
  }
  var noted = false;
  function inject(text, announce) {
    var msgs = document.getElementById("kas-msgs");
    if (!msgs) return;
    if (announce && !msgs.querySelector("[data-agent-note]")) {
      var note = document.createElement("div");
      note.className = "kas-m kas-a"; note.setAttribute("data-agent-note", "1");
      note.style.opacity = ".8"; note.style.fontStyle = "italic";
      note.textContent = "You're now chatting with a Kasandra team member.";
      msgs.appendChild(note);
    }
    var m = document.createElement("div"); m.className = "kas-m kas-a";
    var d = document.createElement("div"); d.textContent = text; m.innerHTML = d.innerHTML;
    msgs.appendChild(m); msgs.scrollTop = msgs.scrollHeight;
  }

  function shouldPoll() {
    return document.visibilityState === "visible" && (isMember || localStorage.getItem("KSID"));
  }
  async function poll() {
    if (!shouldPoll()) return;
    var sid = localStorage.getItem("KSID") || "";
    try {
      var r = await fetch("/api/chat/poll?session=" + encodeURIComponent(sid) + "&after=" + lastId());
      if (!r.ok) return;
      var d = await r.json();
      var got = d.messages || [];
      if (got.length) openWidget();
      got.forEach(function (m) { inject(m.content, !noted); noted = true; if (m.id > lastId()) setLast(m.id); });
    } catch (e) {}
  }
  setInterval(poll, 5000);
})();
