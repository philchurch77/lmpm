/* Client-side word COUNTER (ES5).
   - Default guide: 1200 words. Override per field: data-max-words="150".
   - Opt out (no counter at all): data-max-words="0".

   This script MEASURES and never MUTATES. It used to call an enforce() helper
   that rewrote textarea.value to the first N words — including once at page
   load, before the user had touched anything. That silently deleted the tail of
   any stored record longer than the limit (most of all the bulk-imported
   SharePoint text), and because setting .value programmatically fires no input
   event, unsaved_changes.js never marked the form dirty and never warned. The
   user then saved an unrelated edit and the truncation was written to the
   database under a green "saved" message.

   It was destructive twice over: trimToMaxWords rebuilt the survivors with
   words.join(" "), so every newline and paragraph break in the *kept* text was
   flattened too, the browser's native undo stack was wiped, and the caret was
   thrown to the end of the field mid-sentence.

   There is no server-side word cap and every narrative field is an unbounded
   TextField, so the limit is guidance to the writer, not a constraint the data
   layer needs. Going over it is now shown, not enforced. */

(function () {
  "use strict";

  var DEFAULT_MAX_WORDS = 1200;

  function parseMaxWords(textarea) {
    var raw = textarea.getAttribute("data-max-words");
    if (raw === null || raw === "") return DEFAULT_MAX_WORDS;
    var max = parseInt(raw, 10);
    return isNaN(max) ? DEFAULT_MAX_WORDS : max;
  }

  function getWords(value) {
    if (!value) return [];
    var matches = value.trim().match(/\S+/g);
    return matches ? matches : [];
  }

  function createCounter(textarea) {
    var counter = document.createElement("span");
    counter.className = "word-counter";
    counter.setAttribute("aria-live", "polite");
    textarea.parentNode.insertBefore(counter, textarea.nextSibling);
    return counter;
  }

  function updateCounter(counter, textarea, maxWords) {
    var count = getWords(textarea.value).length;

    if (count > maxWords) {
      // Say plainly that this is a guide and nothing has been removed, so a
      // writer who is over the limit is not left wondering whether the app has
      // quietly taken something from them.
      counter.textContent =
        count + " / " + maxWords + " words — over the suggested limit (nothing is removed)";
      counter.className = "word-counter word-counter--limit";
      return;
    }

    counter.textContent = count + " / " + maxWords + " words";
    counter.className =
      count >= Math.ceil(maxWords * 0.85)
        ? "word-counter word-counter--near"
        : "word-counter";
  }

  function init() {
    var textareas = document.getElementsByTagName("textarea");
    for (var i = 0; i < textareas.length; i++) {
      (function (el) {
        if (el.disabled || el.readOnly) return;
        var maxWords = parseMaxWords(el);
        if (!maxWords || maxWords <= 0) return;

        var counter = createCounter(el);
        updateCounter(counter, el, maxWords);

        el.addEventListener("input", function () {
          updateCounter(counter, el, maxWords);
        });

        el.addEventListener("paste", function () {
          setTimeout(function () {
            updateCounter(counter, el, maxWords);
          }, 0);
        });
      })(textareas[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
