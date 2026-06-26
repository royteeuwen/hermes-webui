"""Coverage for opt-in Web Push (VAPID) background notifications (#3196).

Mostly static-source assertions (the established style for PWA/SW/settings
wiring in this repo), plus a few behavioural checks that the feature is a strict
no-op when ``HERMES_WEBUI_PUSH_ENABLED`` is not set.
"""

import importlib
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
STREAMING_PY = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
GATEWAY_CHAT_PY = (ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")
CRON_WATCHER_PY = (ROOT / "api" / "push_cron_watcher.py").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
SERVER_PY = (ROOT / "server.py").read_text(encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """This module reads source + exercises gating only; no HTTP server needed."""


# ── Service worker ────────────────────────────────────────────────────────────

def test_sw_registers_push_listener_before_notificationclick():
    push_idx = SW_JS.index("self.addEventListener('push'")
    click_idx = SW_JS.index("self.addEventListener('notificationclick'")
    assert push_idx < click_idx
    assert "self.registration.showNotification(title, options)" in SW_JS


def test_sw_push_suppressed_when_app_is_on_screen():
    handler = SW_JS[SW_JS.index("self.addEventListener('push'"):]
    handler = handler[: handler.index("self.addEventListener('notificationclick'")]
    # Must check for a focused/visible window before showing an OS notification,
    # and bail out (return) without showing one when the app is on screen.
    assert "self.clients.matchAll" in handler
    assert "focused" in handler and "visibilityState" in handler
    assert "return;" in handler


def test_sw_push_builds_data_url_shape_for_clickthrough():
    handler = SW_JS[SW_JS.index("self.addEventListener('push'"):]
    handler = handler[: handler.index("self.addEventListener('notificationclick'")]
    assert "event.data.json()" in handler
    assert "data: { url }" in handler
    assert "icon: 'static/favicon-192.png'" in handler
    assert "badge: 'static/favicon-32.png'" in handler


# ── Client subscribe/unsubscribe ──────────────────────────────────────────────

def test_messages_js_has_subscribe_helpers():
    assert "function urlBase64ToUint8Array(" in MESSAGES_JS
    assert "function subscribeToPush(" in MESSAGES_JS
    assert "function unsubscribeFromPush(" in MESSAGES_JS
    assert "pushManager.subscribe({userVisibleOnly:true,applicationServerKey:appKey})" in MESSAGES_JS
    assert "api/push/vapid-public-key" in MESSAGES_JS
    assert "api/push/subscribe" in MESSAGES_JS
    assert "api/push/unsubscribe" in MESSAGES_JS


def test_messages_js_degrades_without_pushmanager():
    assert "if(!('serviceWorker' in navigator)||!('PushManager' in window)) return Promise.resolve(false);" in MESSAGES_JS


def test_permission_grant_subscribes_when_push_enabled():
    assert "window._pushEnabled&&typeof subscribeToPush==='function'" in MESSAGES_JS


# ── Settings UI + flag wiring ─────────────────────────────────────────────────

def test_index_html_has_push_toggle():
    assert 'id="settingsPushEnabled"' in INDEX_HTML
    assert "iOS 16.4+" in INDEX_HTML


def test_boot_js_reads_back_push_flag():
    assert "window._pushEnabled=!!s.push_enabled;" in BOOT_JS
    assert "window._pushEnabled=false;" in BOOT_JS


def test_panels_js_persists_and_toggles_push():
    assert "payload.push_enabled=pushCb.checked;" in PANELS_JS
    assert "body.push_enabled=!!($('settingsPushEnabled')||{}).checked;" in PANELS_JS
    assert "settings.push_enabled" in PANELS_JS
    assert "unsubscribeFromPush()" in PANELS_JS


# ── Server routes ─────────────────────────────────────────────────────────────

def test_routes_define_push_endpoints():
    for route in (
        "/api/push/vapid-public-key",
        "/api/push/status",
        "/api/push/subscribe",
        "/api/push/unsubscribe",
        "/api/push/test",
    ):
        assert f'"{route}"' in ROUTES_PY, route


# ── Persisted setting ─────────────────────────────────────────────────────────

def test_config_has_push_enabled_setting():
    assert '"push_enabled": False' in CONFIG_PY
    # Allowlisted as a bool key so /api/settings can persist it.
    bool_block = CONFIG_PY[CONFIG_PY.index("_SETTINGS_BOOL_KEYS"):]
    assert '"push_enabled",' in bool_block


# ── Requirements ──────────────────────────────────────────────────────────────

def test_pywebpush_is_a_hard_dependency():
    assert "pywebpush" in REQUIREMENTS


# ── Server-side trigger hooks ─────────────────────────────────────────────────

def test_turn_complete_push_fires_near_done_emission():
    done_idx = STREAMING_PY.index("put('done', _done_payload)")
    after = STREAMING_PY[done_idx: done_idx + 600]
    assert "_maybe_push_turn_complete(s, session_id, failed=False)" in after


def test_run_failed_push_on_error_path():
    assert "_maybe_push_turn_complete(s, getattr(s, 'session_id', session_id), failed=True)" in STREAMING_PY
    # Not fired on user cancel/interrupt.
    assert "_exc_type not in ('cancelled', 'interrupted')" in STREAMING_PY


def test_approval_push_on_authoritative_notify_callback():
    cb_idx = STREAMING_PY.index("def _approval_notify_cb(approval_data):")
    cb = STREAMING_PY[cb_idx: cb_idx + 800]
    assert "_maybe_push_approval(session_id, approval_data)" in cb


def test_cron_push_watcher_started_in_server():
    assert "start_cron_push_watcher" in SERVER_PY
    assert "stop_cron_push_watcher" in SERVER_PY


# ── Gateway-mode hooks (chat backend = gateway bypasses streaming.py) ──────────

def test_gateway_mode_mirrors_push_hooks_in_put_gateway_event():
    # Gateway-mode chat never runs the in-process streaming path, so its terminal
    # events must be routed through the push helper from put_gateway_event.
    assert "_maybe_gateway_push(event, data, session_id, stream_id)" in GATEWAY_CHAT_PY
    assert "def _maybe_gateway_push(" in GATEWAY_CHAT_PY
    for ev in ('"done"', '"apperror"', '"approval"'):
        assert ev in GATEWAY_CHAT_PY


def test_gateway_push_helper_routes_terminal_events(monkeypatch):
    import api.gateway_chat as g
    import api.push as push

    sent = []
    monkeypatch.setattr(push, "push_enabled", lambda: True)
    monkeypatch.setattr(push, "send_web_push_to_all",
                        lambda title, body, url, tag=None: sent.append((title, body, url, tag)))
    # Real assistant content flows into the push body via STREAM_PARTIAL_TEXT.
    g.STREAM_PARTIAL_TEXT["s1"] = "The build is green and deployed."
    try:
        g._maybe_gateway_push("done", {}, "s1", "s1")
        g._maybe_gateway_push("apperror", {"message": "gateway exploded"}, "s1", "s1")
        g._maybe_gateway_push("approval", {"command": "rm -rf /tmp/x"}, "s1", "s1")
        g._maybe_gateway_push("token", {"text": "noise"}, "s1", "s1")  # non-terminal: ignored
    finally:
        g.STREAM_PARTIAL_TEXT.pop("s1", None)

    assert len(sent) == 3, sent
    titles = [s[0] for s in sent]
    assert titles == ["Response ready", "Run failed", "Approval needed"]
    assert sent[0][1] == "The build is green and deployed."   # actual content, not a ping
    assert sent[0][2] == "./session/s1"                        # deep-link
    assert "gateway exploded" in sent[1][1]
    assert "rm -rf /tmp/x" in sent[2][1]


def test_gateway_push_helper_is_noop_when_disabled(monkeypatch):
    import api.gateway_chat as g
    import api.push as push
    sent = []
    monkeypatch.setattr(push, "push_enabled", lambda: False)
    monkeypatch.setattr(push, "send_web_push_to_all",
                        lambda *a, **k: sent.append(a))
    g._maybe_gateway_push("done", {}, "s1", "s1")
    assert sent == []


# ── Cron push enrichment (deep-link + real output, not a bare ping) ───────────

def test_cron_push_deep_links_and_includes_output():
    # The watcher must resolve the run's session for the deep-link and use the
    # actual output as the body — the old code linked to "./" and said only
    # "Scheduled task finished."
    assert "def _cron_session_id(" in CRON_WATCHER_PY
    assert "def _cron_output_snippet(" in CRON_WATCHER_PY
    assert "def _cron_output_from_file(" in CRON_WATCHER_PY  # delivered output, both job kinds
    assert "sid = _cron_session_id(job_id)" in CRON_WATCHER_PY
    assert "_cron_output_snippet(job_id, sid)" in CRON_WATCHER_PY
    # No longer reads the never-populated session_id key off the job dict.
    assert "c.get('session_id'" not in CRON_WATCHER_PY
    # Failures surface the real error text.
    assert "last_error" in CRON_WATCHER_PY
    # Parity with cron delivery: silent runs are not pushed.
    assert "[SILENT]" in CRON_WATCHER_PY


def test_cron_push_suppresses_silent_runs(monkeypatch):
    import api.push_cron_watcher as w
    import api.push as push

    sent = []
    monkeypatch.setattr(push, "send_web_push_to_all",
                        lambda title, body, url, tag=None: sent.append((title, body)))
    monkeypatch.setattr(w, "_push_enabled", lambda: True)
    monkeypatch.setattr(w, "_cron_session_id", lambda job_id: "sess1")
    monkeypatch.setattr(w, "_list_completions", lambda: [
        {"job_id": "j1", "name": "pr-watch", "status": "ok",
         "completed_at": 200.0, "toast_notifications": True, "last_error": ""},
    ])

    def run_once(output):
        monkeypatch.setattr(w, "_cron_output_snippet", lambda job_id, sid: output)
        w._seen_completion_ts.clear()
        w._seen_completion_ts["j1"] = 100.0  # an older run we've already seen
        w._primed = True
        sent.clear()
        w._tick()

    run_once("[SILENT]")                       # agent asked to stay silent
    assert sent == [], "silent run must not push"

    run_once("3 PRs awaiting review")          # real content
    assert len(sent) == 1 and sent[0][1] == "3 PRs awaiting review"


# ── Behavioural gating (the no-op contract) ───────────────────────────────────

@pytest.fixture
def push_module(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("HERMES_WEBUI_PUSH_ENABLED", raising=False)
    import api.paths
    importlib.reload(api.paths)
    import api.push
    mod = importlib.reload(api.push)
    yield mod
    # Restore module state for other tests.
    monkeypatch.undo()
    importlib.reload(api.paths)
    importlib.reload(api.push)


def test_disabled_is_strict_noop(push_module, tmp_path):
    assert push_module.push_enabled() is False
    assert push_module.get_vapid_public_key() == ""
    assert push_module.add_subscription({"endpoint": "https://example/x"}) is False
    assert push_module.subscription_count() == 0
    # send_web_push_to_all returns immediately and writes nothing.
    push_module.send_web_push_to_all("t", "b", "./")
    assert not (tmp_path / "push_subscriptions.json").exists()
    assert not (tmp_path / "vapid_keys.json").exists()


def test_secret_files_are_owner_only(monkeypatch, tmp_path):
    """VAPID private key + subscription store must be 0600, never world-readable."""
    if os.name != "posix":
        pytest.skip("POSIX file modes only")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_WEBUI_PUSH_ENABLED", "1")
    import api.paths
    importlib.reload(api.paths)
    import api.push
    mod = importlib.reload(api.push)
    try:
        assert mod.get_vapid_public_key()                       # triggers key persist
        assert mod.add_subscription({"endpoint": "https://example/abc", "keys": {}}) is True
        for name in ("vapid_keys.json", "push_subscriptions.json"):
            f = tmp_path / name
            assert f.exists(), name
            assert (f.stat().st_mode & 0o777) == 0o600, f"{name} is {oct(f.stat().st_mode & 0o777)}"
    finally:
        monkeypatch.undo()
        importlib.reload(api.paths)
        importlib.reload(api.push)


def test_enabled_stores_subscription_without_sending(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_WEBUI_PUSH_ENABLED", "1")
    import api.paths
    importlib.reload(api.paths)
    import api.push
    mod = importlib.reload(api.push)
    try:
        assert mod.push_enabled() is True
        assert mod.add_subscription({"endpoint": "https://example/abc", "keys": {}}) is True
        assert mod.subscription_count() == 1
        assert (tmp_path / "push_subscriptions.json").exists()
        # De-dupe by endpoint.
        mod.add_subscription({"endpoint": "https://example/abc", "keys": {}})
        assert mod.subscription_count() == 1
        assert mod.remove_subscription("https://example/abc") is True
        assert mod.subscription_count() == 0
    finally:
        monkeypatch.undo()
        importlib.reload(api.paths)
        importlib.reload(api.push)
