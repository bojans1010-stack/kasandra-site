/* Kasandra chat: receive human-agent (admin takeover) replies into the widget.
   Polls only after a chat has started (localStorage.KSID exists). No PII. */
(function () {
  var LAST_KEY = "KSAGENT_LAST";
  function lastId() { return parseInt(localStorage.getItem(LAST_KEY) || "0", 10) || 0; }
  function setLast(n) { try { localStorage.setItem(LAST_KEY, String(n)); } catch (e) {} }

  function inject(text, first) {
    var msgs = document.getElementById("kas-msgs");
    if (!msgs) return;
    if (first) {
      var note = document.createElement("div");
      note.className = "kas-m kas-a";
      note.style.opacity = ".75";
      note.style.fontStyle = "italic";
      note.textContent = "You're now chatting with a Kasandra team member.";
      if (!msgs.querySelector("[data-agent-note]")) { note.setAttribute("data-agent-note", "1"); msgs.appendChild(note); }
    }
    var m = document.createElement("div");
    m.className = "kas-m kas-a";
    var d = document.createElement("div"); d.textContent = text; m.innerHTML = d.innerHTML;
    msgs.appendChild(m);
    msgs.scrollTop = msgs.scrollHeight;
  }

  var noted = false;
  async function poll() {
    var sid = localStorage.getItem("KSID");
    if (!sid) return;
    try {
      var r = await fetch("/api/chat/poll?session=" + encodeURIComponent(sid) + "&after=" + lastId());
      if (!r.ok) return;
      var d = await r.json();
      (d.messages || []).forEach(function (m) {
        inject(m.content, !noted); noted = true;
        if (m.id > lastId()) setLast(m.id);
      });
    } catch (e) {}
  }
  setInterval(poll, 5000);
})();
