/* Client-side word limit guard with live counter (ES5).
   - Default: 300 words (matches server-side MAX_TEXTAREA_WORDS).
   - Override per field: data-max-words="150".
   - Opt out (no limit, no counter): data-max-words="0".
*/

(function () {
  "use strict";

  var DEFAULT_MAX_WORDS = 300;

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

  function trimToMaxWords(value, maxWords) {
    var words = getWords(value);
    if (words.length <= maxWords) return value;
    return words.slice(0, maxWords).join(" ");
  }

  function enforce(textarea, maxWords) {
    var next = trimToMaxWords(textarea.value, maxWords);
    if (next !== textarea.value) textarea.value = next;
  }

  function createCounter(textarea) {
    var counter = document.createElement("span");
    counter.className = "word-counter";
    textarea.parentNode.insertBefore(counter, textarea.nextSibling);
    return counter;
  }

  function updateCounter(counter, textarea, maxWords) {
    var count = getWords(textarea.value).length;
    counter.textContent = count + " / " + maxWords + " words";
    if (count >= maxWords) {
      counter.className = "word-counter word-counter--limit";
    } else if (count >= Math.ceil(maxWords * 0.85)) {
      counter.className = "word-counter word-counter--near";
    } else {
      counter.className = "word-counter";
    }
  }

  function init() {
    var textareas = document.getElementsByTagName("textarea");
    for (var i = 0; i < textareas.length; i++) {
      (function (el) {
        if (el.disabled || el.readOnly) return;
        var maxWords = parseMaxWords(el);
        if (!maxWords || maxWords <= 0) return;

        enforce(el, maxWords);
        var counter = createCounter(el);
        updateCounter(counter, el, maxWords);

        el.addEventListener("input", function () {
          enforce(el, maxWords);
          updateCounter(counter, el, maxWords);
        });

        el.addEventListener("paste", function () {
          setTimeout(function () {
            enforce(el, maxWords);
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
