// Progressive enhancement for the senior-leader self-review (Section 2).
// Grey out a standard's score row while it is marked "Not in job role".
// Correctness is enforced server-side (score is nulled on save); this is
// purely a visual cue.
(function () {
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-leader-standard]').forEach(function (card) {
      var radios = card.querySelectorAll('input[name$="-not_applicable"]');
      if (!radios.length) return;
      function sync() {
        var na = card.querySelector('input[name$="-not_applicable"]:checked');
        card.classList.toggle('is-na', !!na && na.value === 'true');
      }
      radios.forEach(function (radio) {
        radio.addEventListener('change', sync);
      });
      sync();
    });
  });
})();
