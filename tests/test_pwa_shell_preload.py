"""Cold-launch shell preload (feat/pwa-shell-preload).

A cold PWA launch previously showed a blank white screen for ~1-2s while the
render-blocking style.css downloaded and the deferred boot scripts parsed. These
tests pin the first-paint optimization: a minimal critical app-shell stylesheet
inlined in <head> (so the framed shell + a loading spinner paint immediately),
a spinner element removed once boot runs, and the non-critical xterm CDN
stylesheet made non-render-blocking.

The shell DOM itself already lives in index.html; this change is purely about
making first paint meaningful and cutting render-blocking <head> work without a
build step.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "static" / "index.html"
STYLE = REPO_ROOT / "static" / "style.css"


def _html():
    return INDEX.read_text(encoding="utf-8")


def test_critical_shell_css_inlined_before_main_stylesheet():
    """Critical shell CSS must be inline in <head> and appear before style.css
    so the browser can paint the shell before the render-blocking stylesheet."""
    html = _html()
    head_close = html.index("</head>")
    crit = html.index('<style id="critical-shell">')
    main_css = html.index("static/style.css?v=__WEBUI_VERSION__")
    assert crit < head_close, "critical-shell <style> must live in <head>"
    assert crit < main_css, "critical-shell CSS must precede style.css"


def test_critical_shell_defines_layout_and_theme_vars():
    """The inline CSS must lay out the shell and define the bg vars for both
    light and dark, because var(--*) is undefined until style.css loads — this
    is what prevents the white (or unstyled) flash."""
    html = _html()
    block = html[html.index('<style id="critical-shell">'):html.index("</style>")]
    # Both palettes so dark-mode users do not get a light flash and vice versa.
    assert "--bg:#0D0D1A" in block and "--sidebar:#141425" in block, "dark vars missing"
    assert "--bg:#FEFCF7" in block and "--sidebar:#FAF7F0" in block, "light vars missing"
    # Core shell layout selectors.
    for sel in (".layout", ".rail", ".sidebar", ".main", ".app-titlebar"):
        assert sel in block, f"critical CSS missing layout rule for {sel}"


def test_critical_shell_layout_matches_style_css_no_cls():
    """Key shell dimensions in the inline CSS must match style.css so the full
    stylesheet taking over does not cause layout shift (CLS)."""
    html = _html()
    block = html[html.index('<style id="critical-shell">'):html.index("</style>")]
    css = STYLE.read_text(encoding="utf-8")
    # Sidebar width and rail width are the load-bearing geometry.
    assert ".sidebar{width:300px" in block
    assert "width:300px" in css  # style.css .sidebar width
    assert ".rail{display:none;width:48px" in block
    assert ".rail{display:none;width:48px" in css
    # Titlebar height.
    assert ".app-titlebar{height:38px" in block
    # Rail revealed at the same >=641px breakpoint style.css uses.
    assert "@media(min-width:641px)" in block
    assert "@media(min-width:641px){.rail{display:flex;}" in css


def test_boot_spinner_present_in_main_and_hidden_when_ready():
    """A loading spinner must exist in the main pane and be hidden once the
    shell is marked ready (decoupled via a single attribute toggle)."""
    html = _html()
    assert 'id="shellBootSpinner"' in html, "boot spinner element missing"
    block = html[html.index('<style id="critical-shell">'):html.index("</style>")]
    assert "#shellBootSpinner" in block, "spinner has no critical CSS"
    assert "html[data-shell-ready] #shellBootSpinner{display:none" in block, \
        "spinner must hide when data-shell-ready is set"
    # Reduced-motion users get a static spinner.
    assert "prefers-reduced-motion" in block


def test_shell_ready_attribute_set_on_boot():
    """An inline head script must set data-shell-ready after the deferred boot
    scripts run (DOMContentLoaded), with a window load fallback."""
    html = _html()
    head_close = html.index("</head>")
    idx = html.index("data-shell-ready")
    assert idx < head_close, "shell-ready toggle must run from <head>"
    assert "DOMContentLoaded" in html
    assert "addEventListener('load'" in html or 'addEventListener("load"' in html


def test_xterm_cdn_css_is_non_render_blocking():
    """The terminal stylesheet is only needed when a terminal opens; it must not
    block first paint. Loaded via the media=print -> onload swap pattern with a
    <noscript> fallback."""
    html = _html()
    assert html.count("css/xterm.css") >= 2, "expected swap link + noscript fallback"
    # The primary link uses the non-blocking swap pattern.
    assert "media=\"print\" onload=\"this.media='all'\"" in html
    assert "<noscript>" in html


def test_main_render_blocking_js_still_deferred():
    """Guard the existing optimization: the app-shell JS modules stay deferred so
    they never block first paint."""
    html = _html()
    for mod in ("boot.js", "ui.js", "messages.js", "sessions.js", "panels.js"):
        # each module script tag carries defer
        marker = f"static/{mod}?v=__WEBUI_VERSION__"
        i = html.index(marker)
        tag = html[html.rindex("<script", 0, i):html.index(">", i) + 1]
        assert "defer" in tag, f"{mod} must stay deferred"


def test_boot_cover_is_full_viewport_and_content_gated():
    """The cold-launch cover must be a solid, full-viewport overlay held until
    real content renders — so an iOS notification launch (forced to start_url)
    never visibly flashes the home before forwarding to the session."""
    html = INDEX.read_text(encoding="utf-8")
    # full-viewport SOLID cover (not the half-transparent --main-bg token)
    assert "#shellBootSpinner{position:fixed;inset:0" in html
    assert "background:#0D0D1A;}" in html
    # reveal is gated on real content + a grace + a hard fallback, NOT just DOMContentLoaded
    assert "Loading conversation" in html         # session-render gate
    assert "getComputedStyle(es).display!=='none'" in html  # home-after-grace gate
    assert "HOME_GRACE+800" in html                # hard fallback sits just past the grace


def test_standalone_launch_holds_home_longer_than_browser_tab():
    """A notification cold-launch can't be distinguished from a normal launch at
    boot (both open start_url) and the navigate that forwards to the session lands
    1-2s later. So on an installed/standalone launch the cover must hold over home
    long enough to absorb that navigate (session renders under cover); a browser
    tab keeps the fast grace."""
    html = INDEX.read_text(encoding="utf-8")
    assert "navigator.standalone===true" in html, "must detect iOS standalone"
    assert "display-mode: standalone" in html, "must detect display-mode standalone"
    assert "standalone?2500:500" in html, "standalone holds home ~2.5s, tab 500ms"
    # the home-grace reveal is gated on the standalone-aware window, not a constant
    assert "(Date.now()-t0)>HOME_GRACE" in html


def test_notification_launch_holds_cover_until_session():
    """A notification cold-launch (iOS forces start_url, then forwards via an
    in-place navigate 1-2s later) must NOT lift the cover on the home/empty-state
    in between. sw.js writes a launch marker on notificationclick; the cover reads
    it at boot and, while present, stays covered until the session paints."""
    html = INDEX.read_text(encoding="utf-8")
    sw = (REPO_ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    # SW persists the marker on click, before focusing/opening a window.
    assert "__hermes_pending_nav__" in sw, "sw.js must write the launch marker"
    nc = sw[sw.index("addEventListener('notificationclick'"):]
    assert nc.index("__hermes_pending_nav__") < nc.index("matchAll"), \
        "marker must be written before the client match/focus logic"
    # The cover reads the marker and gates the empty-state reveal on it.
    assert "__hermes_pending_nav__" in html, "cover must read the launch marker"
    assert "if(notifMode)return;" in html, "empty-state reveal must be gated by notifMode"
