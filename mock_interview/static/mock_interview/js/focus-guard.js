/* Mock Interview Focus Guard
 * =============================================================
 * Detects when a student switches browser tabs or leaves the mock
 * interview window, shows a live warning, records every violation
 * on the server, and locks the interview after a configurable
 * number of strikes so the student cannot look up answers in other
 * tabs during the interview.
 *
 * Zero-dependency vanilla JavaScript built on native browser APIs:
 *   - Page Visibility API  (visibilitychange) -> tab switch / minimize
 *   - window focus/blur    (blur / focus)     -> Alt+Tab, another app
 *   - Fullscreen API       (fullscreenchange) -> optional fullscreen lock
 *
 * It does NOT depend on jQuery, React, npm or a bundler, so it works
 * with the existing Django static setup, including offline labs.
 *
 * Configure it before this script loads:
 *   window.MOCK_INTERVIEW_FOCUS_GUARD = {
 *     endpoint: "<django url for recording a violation>",
 *     csrf: "{{ csrf_token }}",
 *     startingViolations: 0,
 *     maxViolations: 3,
 *     enforceFullscreen: true,
 *     fullscreenTriggerSelector: "#recordButton",
 *     onViolation: function (count, max) {},
 *     onLockout: function () {}
 *   };
 * =============================================================
 */
(function () {
  "use strict";

  var cfg = window.MOCK_INTERVIEW_FOCUS_GUARD || {};
  var endpoint = cfg.endpoint || "";
  var csrfToken = cfg.csrf || "";
  var maxViolations = Number(cfg.maxViolations) || 3;
  var startCount = Number(cfg.startingViolations) || 0;
  var enforceFullscreen = cfg.enforceFullscreen !== false;
  var triggerSelector = cfg.fullscreenTriggerSelector || "#recordButton";
  var onViolation = typeof cfg.onViolation === "function" ? cfg.onViolation : null;
  var onLockout = typeof cfg.onLockout === "function" ? cfg.onLockout : null;

  var violations = startCount;
  var locked = false;
  var navigating = false;
  var blurTimer = null;
  var lastEventAt = 0;
  var MIN_EVENT_GAP_MS = 1200;
  var DIALOG_BLUR_GRACE_MS = 800;

  /* ---------- helpers -------------------------------------------------- */

  function isHidden() {
    return document.hidden !== undefined ? document.hidden : document.visibilityState === "hidden";
  }

  function isFullscreen() {
    return !!(
      document.fullscreenElement ||
      document.webkitFullscreenElement ||
      document.mozFullScreenElement ||
      document.msFullscreenElement
    );
  }

  function requestFullscreen() {
    var el = document.documentElement;
    var fn =
      el.requestFullscreen ||
      el.webkitRequestFullscreen ||
      el.mozRequestFullScreen ||
      el.msRequestFullscreen;
    if (fn) {
      try {
        var ret = fn.call(el);
        if (ret && typeof ret.catch === "function") ret.catch(function () {});
      } catch (e) { /* unsupported / blocked -> detection still works */ }
    }
  }

  function showBanner(message, type) {
    var old = document.getElementById("mi-focus-guard-banner");
    if (old) old.remove();
    var b = document.createElement("div");
    b.id = "mi-focus-guard-banner";
    b.setAttribute("role", "alert");
    var bg = type === "danger" ? "#b91c1c" : type === "warning" ? "#d97706" : "#1a4b8c";
    b.style.cssText =
      "position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:99999;" +
      "max-width:min(92vw,640px);background:" + bg + ";color:#fff;font:600 14px/1.45 " +
      "'Segoe UI',system-ui,sans-serif;padding:12px 18px;border-radius:12px;" +
      "box-shadow:0 10px 30px rgba(0,0,0,.35);transition:opacity .3s;text-align:center;";
    b.textContent = message;
    document.body.appendChild(b);
    setTimeout(function () {
      b.style.opacity = "0";
      setTimeout(function () { if (b.parentNode) b.parentNode.removeChild(b); }, 350);
    }, 4200);
  }

  function lockoutView() {
    var ov = document.createElement("div");
    ov.id = "mi-focus-guard-lockout";
    ov.style.cssText =
      "position:fixed;inset:0;z-index:100000;display:flex;align-items:center;justify-content:center;" +
      "background:rgba(15,23,42,.82);backdrop-filter:blur(3px);padding:24px;";
    var card = document.createElement("div");
    card.style.cssText =
      "max-width:430px;width:100%;background:#fff;border-radius:18px;padding:30px 26px;text-align:center;" +
      "box-shadow:0 24px 60px rgba(0,0,0,.45);font-family:'Segoe UI',system-ui,sans-serif;";
    var icon = document.createElement("div");
    icon.textContent = "⚠";
    icon.style.cssText = "font-size:40px;line-height:1;";
    var title = document.createElement("h3");
    title.textContent = "Interview locked";
    title.style.cssText = "margin:14px 0 8px;font-weight:700;font-size:20px;color:#1e293b;";
    var msg = document.createElement("p");
    msg.textContent =
      "You switched tabs or left the interview window too many times. You can no longer " +
      "continue answering. Please contact your faculty to review this attempt.";
    msg.style.cssText = "margin:0;color:#64748b;font-size:14px;line-height:1.55;";
    card.appendChild(icon);
    card.appendChild(title);
    card.appendChild(msg);
    ov.appendChild(card);
    document.body.appendChild(ov);
    document.querySelectorAll("a,button,input,textarea,select").forEach(function (el) {
      el.blur();
    });
  }

  /* ---------- core ------------------------------------------------------ */

  function lockout() {
    if (locked) return;
    locked = true;
    showBanner("Maximum violations reached. The interview is now locked.", "danger");
    lockoutView();
    if (typeof onLockout === "function") onLockout();
  }

  function submitViolation() {
    if (!endpoint) {
      if (violations >= maxViolations) lockout();
      return;
    }
    try {
      fetch(endpoint, {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken, "Content-Type": "application/json" },
        body: JSON.stringify({ event: "tab_switch" }),
        credentials: "same-origin"
      })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (res) {
          if (res && typeof res.count === "number") violations = Number(res.count);
          if (res && res.locked) lockout();
        })
        .catch(function () {});
    } catch (e) { /* fetch unavailable -> client-side strike count still applies */ }
  }

  function registerViolation() {
    if (locked || navigating) return;
    var now = new Date().getTime();
    if (now - lastEventAt < MIN_EVENT_GAP_MS) return; // blur + visibility pair counts once
    lastEventAt = now;
    violations += 1;
    var remaining = maxViolations - violations;
    showBanner(
      "Do not switch tabs or leave this interview window. Warning " + violations +
        " of " + maxViolations + (remaining > 0 ? " (" + remaining + " left)." : "."),
      remaining <= 0 ? "danger" : "warning"
    );
    if (typeof onViolation === "function") onViolation(violations, maxViolations);
    submitViolation();
    if (violations >= maxViolations) lockout();
  }

  /* ---------- events ---------------------------------------------------- */

  document.addEventListener("visibilitychange", function () {
    if (isHidden()) registerViolation(); // tab switched / minimized
  });

  window.addEventListener("blur", function () {
    // Ignore brief focus loss caused by the confirm() dialogs used for
    // Skip/End. If focus returns quickly the student never actually left;
    // a longer absence (or a hidden document) counts as a violation.
    if (isHidden()) return;
    if (blurTimer) window.clearTimeout(blurTimer);
    blurTimer = window.setTimeout(function () { blurTimer = null; registerViolation(); }, DIALOG_BLUR_GRACE_MS);
  });

  window.addEventListener("focus", function () {
    if (blurTimer) { window.clearTimeout(blurTimer); blurTimer = null; }
  });

  document.addEventListener("fullscreenchange", function () {
    if (!enforceFullscreen || isHidden() || navigating) return;
    if (!isFullscreen()) {
      registerViolation();
      showBanner(
        "Fullscreen mode is required during the interview. Click \u201cRecord answer\u201d to return to fullscreen.",
        "warning"
      );
    }
  });

  // Ask for fullscreen from the first user gesture inside the interview room.
  document.addEventListener(
    "click",
    function (ev) {
      if (!enforceFullscreen || isFullscreen() || isHidden() || navigating) return;
      var el = ev.target;
      if (el && el.closest && triggerSelector && el.closest(triggerSelector)) requestFullscreen();
    },
    true // capture phase so it runs before the room's own click handlers
  );

  /* ---------- public API ------------------------------------------------ */

  window.MockInterviewFocusGuard = {
    isLocked: function () { return locked; },
    getViolations: function () { return violations; },
    requestFullscreen: requestFullscreen,
    isFullscreen: isFullscreen,
    registerViolation: registerViolation,
    lockout: lockout,
    // Called before a normal page transition (submit/skip/end) so that a
    // blur fired during navigation is never mistaken for a violation.
    suppress: function () { navigating = true; },
    resume: function () { navigating = false; }
  };
})();