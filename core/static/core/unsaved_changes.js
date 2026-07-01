(function () {
  // Forms opt in via data-warn-unsaved. Dirty state is tracked per form via
  // input/change events (covers text fields, textareas, selects, and the
  // segmented-pill radio groups) and cleared on a real submit, so a
  // deliberate save never triggers the leave-page warning.
  var DIRTY_FORMS = new Set();

  function markDirty(event) {
    DIRTY_FORMS.add(event.currentTarget);
  }

  function markClean(form) {
    DIRTY_FORMS.delete(form);
  }

  function isDirty(form) {
    return DIRTY_FORMS.has(form);
  }

  function firstDirtyForm() {
    var found = null;
    DIRTY_FORMS.forEach(function (form) {
      if (!found && document.body.contains(form)) found = form;
    });
    return found;
  }

  function trackForm(form) {
    form.addEventListener('input', markDirty);
    form.addEventListener('change', markDirty);
    form.addEventListener('submit', function () {
      markClean(form);
    });
  }

  function buildModal() {
    var backdrop = document.createElement('div');
    backdrop.className = 'unsaved-modal-backdrop';

    var modal = document.createElement('div');
    modal.className = 'unsaved-modal';
    modal.setAttribute('role', 'alertdialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'unsaved-modal-title');
    modal.innerHTML =
      '<p class="unsaved-modal-title" id="unsaved-modal-title">You have unsaved changes</p>' +
      '<p class="unsaved-modal-body">Save your changes before leaving this section, or they will be lost.</p>' +
      '<div class="unsaved-modal-actions">' +
      '<button type="button" class="button unsaved-modal-stay">Stay on this page</button>' +
      '<button type="button" class="button unsaved-modal-discard">Discard changes</button>' +
      '<button type="button" class="button unsaved-modal-save">Save and continue</button>' +
      '</div>';
    backdrop.appendChild(modal);
    return backdrop;
  }

  // Shows a custom modal for navigation we fully control in JS: switching
  // appraisal tabs, and (via the click interceptor below) any same-page link
  // click. Navigation we cannot intercept with a click handler — closing the
  // tab, typing a URL, browser back/forward, refreshing — falls through to
  // the native beforeunload dialog instead, since browsers do not allow a
  // custom "Save" button there, by design.
  function confirmLeave(form, options) {
    options = options || {};
    var backdrop = buildModal();
    document.body.appendChild(backdrop);

    function close() {
      backdrop.remove();
    }

    backdrop.querySelector('.unsaved-modal-stay').addEventListener('click', close);
    backdrop.querySelector('.unsaved-modal-discard').addEventListener('click', function () {
      markClean(form);
      close();
      if (options.onDiscard) options.onDiscard();
    });
    backdrop.querySelector('.unsaved-modal-save').addEventListener('click', function () {
      close();
      if (form.requestSubmit) {
        form.requestSubmit();
      } else {
        form.submit();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(
      document.querySelectorAll('form[data-warn-unsaved]'),
      trackForm
    );
  });

  // Intercept normal same-page link clicks (sidebar nav, "Open" links, sign
  // out, etc.) so they get the same custom Save/Discard/Stay modal as the
  // appraisal tabs, instead of relying solely on the browser's native — and
  // easy to miss — beforeunload dialog. A click handler can only catch actual
  // clicks; anything it can't see (closing the tab, typing a URL, browser
  // back/forward, refreshing) still falls through to that native dialog via
  // the beforeunload listener below, which is the one case the web platform
  // does not allow a custom "Save" button for, by design.
  document.addEventListener('click', function (event) {
    if (event.defaultPrevented) return;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

    var link = event.target.closest('a[href]');
    if (!link) return;
    if (link.target && link.target !== '_self') return;
    if (link.hasAttribute('data-skip-unsaved-check')) return;

    var href = link.getAttribute('href');
    if (!href || href.charAt(0) === '#') return;
    if (link.origin !== window.location.origin) return;

    var dirtyForm = firstDirtyForm();
    if (!dirtyForm) return;

    event.preventDefault();
    confirmLeave(dirtyForm, {
      onDiscard: function () {
        window.location.href = href;
      },
    });
  });

  window.addEventListener('beforeunload', function (event) {
    var anyDirty = false;
    DIRTY_FORMS.forEach(function (form) {
      if (document.body.contains(form)) anyDirty = true;
    });
    if (anyDirty) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  window.UnsavedChanges = {
    isDirty: isDirty,
    markClean: markClean,
    confirmLeave: confirmLeave,
  };
})();
