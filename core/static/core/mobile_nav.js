(function () {
  document.addEventListener('DOMContentLoaded', function () {
    var toggle = document.querySelector('.mobile-nav-toggle');
    var backdrop = document.querySelector('.sidebar-backdrop');
    var sidebar = document.getElementById('sidebar');
    var body = document.body;
    if (!toggle || !backdrop || !sidebar) return;

    function isOpen() {
      return body.classList.contains('mobile-nav-open');
    }

    function setOpen(open) {
      if (open) {
        body.classList.add('mobile-nav-open');
      } else {
        body.classList.remove('mobile-nav-open');
      }
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    }

    toggle.addEventListener('click', function () {
      setOpen(!isOpen());
    });

    backdrop.addEventListener('click', function () {
      setOpen(false);
    });

    document.addEventListener('keydown', function (event) {
      var key = event && (event.key || event.code);
      if ((key === 'Escape' || key === 'Esc') && isOpen()) {
        setOpen(false);
      }
    });

    // Close the menu when a navigation link is chosen.
    sidebar.addEventListener('click', function (event) {
      var el = event.target;
      while (el && el !== sidebar) {
        if (el.tagName && el.tagName.toLowerCase() === 'a') {
          setOpen(false);
          return;
        }
        el = el.parentNode;
      }
    });
  });
})();
