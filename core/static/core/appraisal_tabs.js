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

    function currentDirtyForm() {
      var visible = document.querySelector('.tab-panel:not([hidden])');
      if (!visible) return null;
      var form = visible.querySelector('form[data-warn-unsaved]');
      if (!form || !window.UnsavedChanges || !window.UnsavedChanges.isDirty(form)) return null;
      return form;
    }

    tablist.addEventListener('click', function (event) {
      var tab = event.target.closest('.tab');
      if (!tab) return;
      event.preventDefault();
      var name = tab.getAttribute('data-tab');
      var href = tab.getAttribute('href');

      var dirtyForm = currentDirtyForm();
      if (dirtyForm) {
        window.UnsavedChanges.confirmLeave(dirtyForm, {
          onDiscard: function () {
            show(name, href);
          },
        });
        return;
      }
      show(name, href);
    });
  });
})();
