// logimap focus emitter — KWin JavaScript script.
//
// Installed via the org.kde.kwin.Scripting D-Bus service by
// logimap.focus.kwin_dbus.install_kwin_script().
//
// On every window activation this script calls a D-Bus method on
//   service:   org.logimap.Focus1
//   path:      /org/logimap/Focus1
//   interface: org.logimap.Focus1
//   method:    Notify(ss s s i) -> resourceClass, resourceName, caption, pid
//
// The Python daemon owns that service name; if the daemon is not running,
// the callInDBus() call is a no-op.

function emit(window) {
    if (!window) return;
    var rc = (window.resourceClass || "").toString();
    var rn = (window.resourceName || "").toString();
    var cap = (window.caption || "").toString();
    var pid = window.pid || 0;
    print("logimap-focus: emit " + rc + " | " + cap);
    try {
        callDBus(
            "org.logimap.Focus1",
            "/org/logimap/Focus1",
            "org.logimap.Focus1",
            "Notify",
            rc, rn, cap, pid
        );
    } catch (e) {
        print("logimap-focus: callDBus failed: " + e);
    }
}

print("logimap-focus: script loaded");

// Also re-emit when the active window's caption changes (e.g. Dolphin
// navigating to another folder), so `run:` keys see the current folder
// without waiting for the next window activation.
var tracked = null;

function onCaptionChanged() {
    if (tracked && workspace.activeWindow === tracked) {
        emit(tracked);
    }
}

function track(window) {
    if (tracked) {
        try { tracked.captionChanged.disconnect(onCaptionChanged); } catch (e) {}
    }
    tracked = window || null;
    if (tracked) {
        try { tracked.captionChanged.connect(onCaptionChanged); } catch (e) {
            print("logimap-focus: captionChanged unavailable: " + e);
        }
    }
}

workspace.windowActivated.connect(function(window) {
    track(window);
    emit(window);
});

// Emit once on load so the daemon paints the right profile on startup.
if (workspace.activeWindow) {
    track(workspace.activeWindow);
    emit(workspace.activeWindow);
}
