// Progressive enhancement for the senior-leader self-review (Section 2).
// Grey out a standard's score row while its "Not in job role" box is ticked.
// Correctness is enforced server-side (score is nulled on save); this is
// purely a visual cue.
(function () {
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-leader-standard]').forEach(function (card) {
      var box = card.querySelector('input[type="checkbox"][name$="-not_applicable"]');
      if (!box) return;
      function sync() {
        card.classList.toggle('is-na', box.checked);
      }
      box.addEventListener('change', sync);
      sync();
    });
  });
})();
