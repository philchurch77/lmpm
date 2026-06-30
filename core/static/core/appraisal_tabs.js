(function () {
  document.addEventListener('DOMContentLoaded', function () {
    var tablist = document.querySelector('.tabs[role="tablist"]');
    if (!tablist) return;
    var tabs = Array.prototype.slice.call(tablist.querySelectorAll('.tab'));
    if (!tabs.length) return;

    function show(name, href) {
      tabs.forEach(function (tab) {
        var selected = tab.getAttribute('data-tab') === name;
        tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      });
      document.querySelectorAll('.tab-panel').forEach(function (panel) {
        panel.hidden = panel.id !== 'panel-' + name;
      });
      if (href && window.history && window.history.replaceState) {
        window.history.replaceState(null, '', href);
      }
    }

    tablist.addEventListener('click', function (event) {
      var tab = event.target.closest('.tab');
      if (!tab) return;
      event.preventDefault();
      show(tab.getAttribute('data-tab'), tab.getAttribute('href'));
    });
  });
})();
