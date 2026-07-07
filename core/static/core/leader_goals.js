// Add/remove rows for the senior-leader self-review goals formset.
// Progressive enhancement: without JS the server-rendered blank row (extra=1)
// still lets a leader add one goal per save.
(function () {
  document.addEventListener('DOMContentLoaded', function () {
    // Grey out a standard's score row while it is marked "Not in job role".
    // Correctness is enforced server-side (score is nulled on save); this is
    // purely a visual cue.
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

    var container = document.querySelector('[data-formset]');
    if (!container) return;

    var prefix = container.getAttribute('data-prefix');
    var rows = container.querySelector('[data-formset-rows]');
    var template = container.querySelector('[data-formset-empty]');
    var addBtn = container.querySelector('[data-formset-add]');
    var totalInput = document.getElementById('id_' + prefix + '-TOTAL_FORMS');
    if (!rows || !template || !addBtn || !totalInput) return;

    addBtn.addEventListener('click', function () {
      var index = parseInt(totalInput.value, 10);
      var html = template.innerHTML.replace(/__prefix__/g, index);
      var wrapper = document.createElement('div');
      wrapper.innerHTML = html.trim();
      var row = wrapper.firstElementChild;
      if (!row) return;
      rows.appendChild(row);
      totalInput.value = index + 1;
      var firstField = row.querySelector('textarea, input:not([type=hidden])');
      if (firstField) firstField.focus();
    });

    // Removing ticks the row's DELETE box and hides it, so the management form
    // TOTAL_FORMS count stays consistent (Django deletes/ignores it on save).
    rows.addEventListener('click', function (event) {
      var btn = event.target.closest('[data-formset-remove]');
      if (!btn) return;
      var row = btn.closest('[data-formset-row]');
      if (!row) return;
      var del = row.querySelector('input[name$="-DELETE"]');
      if (del) del.checked = true;
      row.hidden = true;
    });
  });
})();
