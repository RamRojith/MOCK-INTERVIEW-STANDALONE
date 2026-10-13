/* Mock Interview — Fullscreen Interview Room controller
 * =============================================================
 * Loaded only on mock_interview/room.html.
 *
 * - Adds the `mi-interview-shell` class so the room immediately renders as a
 *   dedicated distraction-free, viewport-sized layout (ERP chrome hidden) even
 *   before browser fullscreen is granted.
 * - Wires the "Enter / Exit Full Screen" control.
 * - Falls back gracefully when Fullscreen API is denied or unsupported:
 *   the immersive layout stays active and the control remains available so the
 *   student can retry. Exiting fullscreen with Esc never ends the interview.
 * =============================================================
 */
(function () {
  "use strict";

  var root = document.documentElement;
  root.classList.add("mi-interview-shell");

  var btn = document.getElementById("miFullscreenBtn");
  var icon = btn ? btn.querySelector("i") : null;

  function supported() {
    return !!(
      root.requestFullscreen ||
      root.webkitRequestFullscreen ||
      root.mozRequestFullScreen ||
      root.msRequestFullscreen
    );
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
    var fn =
      root.requestFullscreen ||
      root.webkitRequestFullscreen ||
      root.mozRequestFullScreen ||
      root.msRequestFullscreen;
    if (!fn) return false;
    try {
      var ret = fn.call(root);
      if (ret && typeof ret.catch === "function") ret.catch(function () {});
      return true;
    } catch (e) {
      return false;
    }
  }

  function exitFullscreen() {
    var fn = document.exitFullscreen || document.webkitExitFullscreen;
    if (fn) {
      try {
        fn.call(document);
      } catch (e) { /* ignore */ }
    }
  }

  function setState(active) {
    if (!btn || !icon) return;
    btn.title = active ? "Exit full screen (Esc)" : "Enter full screen";
    btn.setAttribute("aria-label", btn.title);
    icon.className = "fas fa-" + (active ? "compress" : "expand");
  }

  function onFullscreenChange() {
    setState(isFullscreen());
  }

  function bind() {
    document.addEventListener("fullscreenchange", onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", onFullscreenChange);
    document.addEventListener("mozfullscreenchange", onFullscreenChange);
    document.addEventListener("MSFullscreenChange", onFullscreenChange);
  }

  bind();

  if (btn) {
    btn.addEventListener("click", function () {
      if (isFullscreen()) {
        exitFullscreen();
      } else {
        requestFullscreen();
      }
    });
  }

  setState(isFullscreen());

  window.MockInterviewRoom = {
    isFullscreen: isFullscreen,
    requestFullscreen: requestFullscreen,
    exitFullscreen: exitFullscreen,
    supported: supported
  };
})();