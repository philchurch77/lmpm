/* Plain-text bullet helper (ES5). Opt in per textarea with data-bullets
   (set server-side only on boxes the viewer can edit; see
   line_management/forms.py _offer_bullets).

   - Enter at the end of a line that starts "- " and has text after it
     continues the list: inserts a newline and "- ".
   - Enter on a line that is only "- " ends the list: removes the marker,
     leaving the empty line.

   House rule (CLAUDE.md "Data safety"): client-side helpers MEASURE, they
   never MUTATE stored text. This script only ever inserts at the caret, in
   direct response to the user's own keystroke, through the browser's
   undo-aware path (execCommand insertText), so Ctrl+Z reverses it and the
   input event marks the form dirty for unsaved_changes.js. It does nothing at
   page load except attach one listener, never assigns .value, and never
   touches text it did not just insert. What is stored stays plain text. */

(function () {
  "use strict";

  var MARKER = "- ";

  function isBulletBox(el) {
    return (
      el &&
      el.tagName === "TEXTAREA" &&
      el.hasAttribute("data-bullets") &&
      !el.disabled &&
      !el.readOnly
    );
  }

  // Replace [start, end) with text, keeping native undo where the browser allows.
  function insertAt(el, start, end, text) {
    el.setSelectionRange(start, end);
    var done = false;
    try {
      // "delete" removes the selected marker; insertText with "" is not
      // honoured the same way in every browser.
      done = text
        ? document.execCommand("insertText", false, text)
        : document.execCommand("delete", false, null);
    } catch (e) {
      done = false;
    }
    if (!done) {
      // Fallback loses undo, so announce the edit ourselves.
      el.setRangeText(text, start, end, "end");
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return;
    if (event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
    var el = event.target;
    if (!isBulletBox(el)) return;

    var pos = el.selectionStart;
    if (pos !== el.selectionEnd) return;
    var value = el.value;
    // Only at the end of a line: Enter mid-line splits it as normal.
    if (pos < value.length && value.charAt(pos) !== "\n") return;

    var lineStart = value.lastIndexOf("\n", pos - 1) + 1;
    var line = value.slice(lineStart, pos);

    if (line === MARKER) {
      event.preventDefault();
      insertAt(el, lineStart, pos, "");
    } else if (/^- \S/.test(line)) {
      event.preventDefault();
      insertAt(el, pos, pos, "\n" + MARKER);
    }
  });
})();
