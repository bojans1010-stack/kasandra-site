/* Kasandra -> PIP Trading move banner (owner OK 20 Oct 2026). One file, no dependencies; included by the scanner
   (chart/index.html) and the site (site-steroids). x hides it until the next day (localStorage, failures ignored). */
(function () {
  try {
    var D = document, L = (navigator.language || 'en').toLowerCase().slice(0, 2);
    var sr = ['sr', 'hr', 'bs', 'mk', 'sh', 'me', 'sl'].indexOf(L) >= 0;
    var K = 'pipmove_hide_until';
    try { if (+localStorage.getItem(K) > Date.now()) return; } catch (e) {}
    var URL = 'https://pip-trading.com/pip-lab/pip-home/?utm_source=kasandra&utm_medium=banner&utm_campaign=move';
    var T = sr
      ? ['Kasandra prelazi na PIP Trading.', ' Isti skener, plus akademija, zajednica, vesti, trening na istoriji i ', 'Autocopy (uskoro)', '.', 'Pogledaj šta te čeka →', 'Zatvori']
      : ['Kasandra is moving to PIP Trading.', ' The same scanner, plus the academy, community, news, a replay gym and ', 'Autocopy (coming soon)', '.', "See what's waiting →", 'Close'];
    var css = '#pipmove{position:relative;z-index:2147483000;display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;' +
      'box-sizing:border-box;width:100%;margin:0;padding:10px 14px;background:linear-gradient(90deg,#10261f,#0f1420 60%);' +
      'border-bottom:1px solid #1d4a3e;border-left:4px solid #19b39b;color:#e9eef5;font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif}' +
      '#pipmove .t{flex:1 1 300px;min-width:0}#pipmove b{color:#fff}#pipmove .g{color:#E6C07A;font-weight:700}' +
      '#pipmove a{background:#19b39b;color:#04120e;font-weight:700;text-decoration:none;padding:7px 13px;border-radius:8px;white-space:nowrap}' +
      '#pipmove button{background:none;border:0;color:#8a97a8;font-size:20px;line-height:1;cursor:pointer;padding:0 4px}' +
      /* the scanner fills the screen (#app height 100%): with the bar on, the page is a column and #app takes the rest */
      'body.pm-on{display:flex;flex-direction:column}body.pm-on>#pipmove{flex:0 0 auto}body.pm-on>#app{flex:1 1 auto;min-height:0;height:auto}';
    function show() {
      if (D.getElementById('pipmove')) return;
      var st = D.createElement('style'); st.textContent = css; D.head.appendChild(st);
      var bar = D.createElement('div'); bar.id = 'pipmove'; bar.setAttribute('role', 'region'); bar.setAttribute('aria-label', 'PIP Trading');
      var t = D.createElement('div'); t.className = 't';
      t.appendChild(D.createTextNode('🚀 ')); var b = D.createElement('b'); b.textContent = T[0]; t.appendChild(b);
      t.appendChild(D.createTextNode(T[1])); var g = D.createElement('span'); g.className = 'g'; g.textContent = T[2]; t.appendChild(g);
      t.appendChild(D.createTextNode(T[3]));
      var a = D.createElement('a'); a.href = URL; a.target = '_blank'; a.rel = 'noopener'; a.textContent = T[4];
      var x = D.createElement('button'); x.type = 'button'; x.setAttribute('aria-label', T[5]); x.textContent = '×';
      x.onclick = function () {
        try { var n = new Date(); n.setHours(24, 0, 0, 0); localStorage.setItem(K, String(n.getTime())); } catch (e) {}
        bar.parentNode && bar.parentNode.removeChild(bar); D.body.classList.remove('pm-on');
      };
      bar.appendChild(t); bar.appendChild(a); bar.appendChild(x);
      D.body.insertBefore(bar, D.body.firstChild); D.body.classList.add('pm-on');
    }
    if (D.body) show(); else D.addEventListener('DOMContentLoaded', show);
  } catch (e) { /* never break the page */ }
})();
