/* Kasandra standardized risk disclosure — single source of truth.
   Injected into every public page footer. Skips pages that already
   render a full .disclaimer block (e.g. the home page) to avoid duplicates. */
(function () {
  function render() {
    if (document.getElementById('kas-risk')) return;
    if (document.querySelector('.disclaimer')) return; // page already has the full disclosure
    var EN = "Risk Disclosure. Trading foreign exchange, gold and CFDs is highly speculative, " +
      "carries a high level of risk, and may not be suitable for all investors. You could lose some " +
      "or all of your invested capital, so never trade with money you cannot afford to lose. Past " +
      "performance and any results shown are not indicative of future results. Kasandra Technologies " +
      "provides market information, technology and education only; nothing on this site is financial, " +
      "investment or trading advice, and we are not a licensed financial advisor or broker. You trade " +
      "entirely at your own risk through your own third-party broker account, where your funds remain " +
      "in your name and under your control. Kasandra is free to use; we may receive rebates or partner " +
      "compensation from the broker when you trade through our link.";
    var box = document.createElement('div');
    box.id = 'kas-risk';
    box.setAttribute('role', 'note');
    box.style.cssText =
      'max-width:960px;margin:34px auto 20px;padding:16px 18px;border-top:1px solid rgba(255,255,255,.10);' +
      'font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:11.5px;line-height:1.6;' +
      'color:#8a94a6;text-align:left;';
    box.innerHTML = '<b style="color:#aab3c2">Risk Disclosure.</b> ' +
      EN.replace('Risk Disclosure. ', '') +
      ' <a href="/risk" style="color:#e6c07a;text-decoration:none">Read the full risk disclosure &rarr;</a>';
    document.body.appendChild(box);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
