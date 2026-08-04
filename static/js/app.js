/* QuizOps client behaviour.
 *
 * The client is display-only: it never decides whether an answer counts, when
 * an attempt expires, or what a score is. It only mirrors server state and
 * makes failure visible -- an exam UI must never imply an answer is saved when
 * it is not (spec 4.2).
 */
(function () {
  "use strict";

  function banner(show, text) {
    var el = document.getElementById("connection-banner");
    if (!el) return;
    if (text) el.querySelector("strong").nextSibling.textContent = " " + text;
    el.hidden = !show;
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (typeof window.htmx === "undefined") {
      banner(true, "The page could not load its scripts. Reload before answering.");
      document.querySelectorAll(".choice-form").forEach(function (form) {
        form.setAttribute("method", "post");
      });
    }

    document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
      el.addEventListener("change", function () {
        el.form.submit();
      });
    });

    startCountdown();
  });

  /* Keep every form's optimistic-concurrency token in step with the server.
   * The server echoes the authoritative revision on success *and* on conflict,
   * so a conflicted page can recover without a manual reload. */
  document.body.addEventListener("htmx:afterSwap", function (event) {
    var node = event.detail.target.querySelector("[data-revision]");
    if (!node) return;
    banner(false);
    var revision = node.dataset.revision;
    document.querySelectorAll(".revision-input").forEach(function (input) {
      input.value = revision;
    });
    var label = document.getElementById("revision-label");
    if (label) label.textContent = revision;

    if (node.dataset.status === "expired") {
      document.querySelectorAll(".choice-form :is(input, button)").forEach(function (el) {
        el.disabled = true;
      });
    }
    if (node.dataset.status === "locked" || node.dataset.locked === "1") {
      var form = event.detail.target.closest(".choice-form");
      if (form) {
        form.querySelectorAll("input, button").forEach(function (el) {
          el.disabled = true;
        });
      }
    }
  });

  /* A dropped request must be loud, not silent. */
  document.body.addEventListener("htmx:sendError", function () {
    banner(true, "Your last change did not reach the server. Check your connection.");
  });
  document.body.addEventListener("htmx:timeout", function () {
    banner(true, "The server did not respond in time. Your last change is not saved.");
  });
  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var status = event.detail.elt.querySelector(".save-status");
    if (status) status.textContent = "Saving…";
  });

  /* Display-only countdown against the server-issued cutoff (ADR-0005). It
   * never submits or scores; expiry is decided server-side on the next
   * request and by the Celery sweep. */
  function startCountdown() {
    var el = document.getElementById("countdown");
    if (!el || !el.dataset.cutoff) return;
    var cutoff = new Date(el.dataset.cutoff).getTime();
    if (isNaN(cutoff)) return;

    function tick() {
      var remaining = cutoff - Date.now();
      if (remaining <= 0) {
        el.textContent = "Time is up — submit now";
        el.classList.add("expired");
        return;
      }
      var totalSeconds = Math.floor(remaining / 1000);
      var hours = Math.floor(totalSeconds / 3600);
      var minutes = Math.floor((totalSeconds % 3600) / 60);
      var seconds = totalSeconds % 60;
      el.textContent =
        (hours > 0 ? hours + "h " : "") +
        String(minutes).padStart(2, "0") + "m " +
        String(seconds).padStart(2, "0") + "s remaining";
      el.classList.toggle("urgent", remaining < 120000);
      window.setTimeout(tick, 1000);
    }
    tick();
  }
})();
