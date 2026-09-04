/* Kasandra live-visitor heartbeat. Anonymous per-tab id, pings the server
   so the admin can see how many people are on the site right now. No PII. */
(function () {
  try {
    var K = 'kas_vid';
    var vid = sessionStorage.getItem(K);
    if (!vid) {
      vid = Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
      sessionStorage.setItem(K, vid);
    }
    function ping() {
      try { fetch('/api/ping?vid=' + encodeURIComponent(vid), { keepalive: true }).catch(function () {}); }
      catch (e) {}
    }
    ping();
    setInterval(ping, 20000);
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') ping();
    });
  } catch (e) {}
})();
