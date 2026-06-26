"""Web Push (VAPID) background notifications for Hermes WebUI (#3196).

Opt-in and gated behind ``HERMES_WEBUI_PUSH_ENABLED``. When that env var is not
truthy, every public function here is a strict no-op: no keys are generated, no
subscriptions are read, and no network calls are made. This keeps default
behaviour byte-for-byte unchanged.

State lives under the WebUI ``STATE_DIR`` (``HERMES_WEBUI_STATE_DIR``):
  * ``vapid_keys.json``        — the server VAPID keypair (base64url), generated
                                 once unless overridden by env.
  * ``push_subscriptions.json`` — the browser ``PushSubscription`` objects.

VAPID keys may be supplied via env so they survive a wiped state dir / match an
external generator:
  * ``HERMES_WEBUI_VAPID_PUBLIC_KEY``  (base64url, uncompressed P-256 point)
  * ``HERMES_WEBUI_VAPID_PRIVATE_KEY`` (base64url DER/raw, as py_vapid emits)
  * ``HERMES_WEBUI_VAPID_SUBJECT``     (``mailto:`` or ``https:`` contact)
"""

import json
import logging
import os
import threading

from api.paths import PUSH_SUBSCRIPTIONS_FILE, VAPID_KEYS_FILE

logger = logging.getLogger(__name__)

# Mirror api.config's _SETTINGS_WRITE_LOCK idiom: a single module-level lock
# serialises all reads/writes of the on-disk JSON files so concurrent request
# threads never corrupt them.
_PUSH_WRITE_LOCK = threading.Lock()

_DEFAULT_VAPID_SUBJECT = "mailto:admin@localhost"


def push_enabled() -> bool:
    """Return True only when the feature is explicitly switched on via env."""
    return str(os.getenv("HERMES_WEBUI_PUSH_ENABLED", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _vapid_subject() -> str:
    return (os.getenv("HERMES_WEBUI_VAPID_SUBJECT", "") or "").strip() or _DEFAULT_VAPID_SUBJECT


# ── VAPID keypair ─────────────────────────────────────────────────────────────

def _generate_vapid_keypair() -> dict:
    """Generate a fresh VAPID keypair, returning {'public': ..., 'private': ...}.

    Values are base64url strings as py_vapid emits them (the shape pywebpush and
    the browser ``applicationServerKey`` both expect).
    """
    from py_vapid import Vapid  # imported lazily so import errors stay local

    vapid = Vapid()
    vapid.generate_keys()
    # py_vapid's helper method names vary across versions, so derive the
    # base64url encodings directly from the underlying cryptography key objects
    # — the stable, version-independent path.
    return {
        "public": _b64_public_from_vapid(vapid),
        "private": _b64_private_from_vapid(vapid),
    }


def _b64_public_from_vapid(vapid) -> str:
    import base64

    from cryptography.hazmat.primitives import serialization

    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_private_from_vapid(vapid) -> str:
    import base64

    # The canonical VAPID private key is the raw 32-byte P-256 scalar, base64url
    # encoded — the form pywebpush's vapid_private_key accepts (Vapid.from_string).
    private_value = vapid.private_key.private_numbers().private_value
    raw = private_value.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _write_secret_json(path, obj) -> None:
    """Write JSON to ``path`` atomically with owner-only (0600) permissions.

    Both files this guards are sensitive: ``vapid_keys.json`` holds the private
    key (which can forge pushes to every subscriber) and ``push_subscriptions``
    carries per-client push-auth secrets. Create with mode 0600 from the start
    (via ``os.open``) so the file is never briefly world-readable, write to a
    temp file, then atomically replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # enforce perms even if the file pre-existed looser
    except OSError:
        logger.debug("Could not chmod %s to 0600", path, exc_info=True)


def _load_or_create_keys() -> dict:
    """Return the active VAPID keypair as {'public','private'}.

    Precedence: explicit env vars > persisted file > freshly generated (then
    persisted). Never raises — on any failure returns an empty dict so callers
    degrade to a no-op instead of crashing a request.
    """
    env_pub = (os.getenv("HERMES_WEBUI_VAPID_PUBLIC_KEY", "") or "").strip()
    env_priv = (os.getenv("HERMES_WEBUI_VAPID_PRIVATE_KEY", "") or "").strip()
    if env_pub and env_priv:
        return {"public": env_pub, "private": env_priv}

    with _PUSH_WRITE_LOCK:
        try:
            if VAPID_KEYS_FILE.exists():
                data = json.loads(VAPID_KEYS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("public") and data.get("private"):
                    return {"public": data["public"], "private": data["private"]}
        except Exception:
            logger.debug("Failed to read VAPID keys from %s", VAPID_KEYS_FILE, exc_info=True)

        try:
            keys = _generate_vapid_keypair()
            _write_secret_json(VAPID_KEYS_FILE, keys)
            return keys
        except Exception:
            logger.warning("Failed to generate/persist VAPID keys", exc_info=True)
            return {}


def get_vapid_public_key() -> str:
    """Return the base64url VAPID public key the browser subscribes with.

    Empty string when the feature is disabled or key material is unavailable.
    """
    if not push_enabled():
        return ""
    return _load_or_create_keys().get("public", "")


# ── Subscription store ────────────────────────────────────────────────────────

def _load_subscriptions() -> list:
    try:
        if PUSH_SUBSCRIPTIONS_FILE.exists():
            data = json.loads(PUSH_SUBSCRIPTIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        logger.debug("Failed to read push subscriptions", exc_info=True)
    return []


def _save_subscriptions(subs: list) -> None:
    _write_secret_json(PUSH_SUBSCRIPTIONS_FILE, subs)


def _endpoint(sub) -> str:
    return str(sub.get("endpoint", "")) if isinstance(sub, dict) else ""


def subscription_count() -> int:
    if not push_enabled():
        return 0
    with _PUSH_WRITE_LOCK:
        return len(_load_subscriptions())


def add_subscription(sub: dict) -> bool:
    """Persist a browser PushSubscription. De-dupes by endpoint. No-op when off."""
    if not push_enabled():
        return False
    if not isinstance(sub, dict) or not _endpoint(sub):
        return False
    with _PUSH_WRITE_LOCK:
        subs = _load_subscriptions()
        ep = _endpoint(sub)
        subs = [s for s in subs if _endpoint(s) != ep]
        subs.append(sub)
        _save_subscriptions(subs)
    return True


def remove_subscription(endpoint: str) -> bool:
    """Remove a subscription by endpoint. No-op when off."""
    if not push_enabled():
        return False
    endpoint = str(endpoint or "")
    if not endpoint:
        return False
    with _PUSH_WRITE_LOCK:
        subs = _load_subscriptions()
        kept = [s for s in subs if _endpoint(s) != endpoint]
        if len(kept) != len(subs):
            _save_subscriptions(kept)
            return True
    return False


def _prune_endpoints(endpoints: set) -> None:
    if not endpoints:
        return
    with _PUSH_WRITE_LOCK:
        subs = _load_subscriptions()
        kept = [s for s in subs if _endpoint(s) not in endpoints]
        if len(kept) != len(subs):
            _save_subscriptions(kept)


# ── Sending ───────────────────────────────────────────────────────────────────

def _dispatch(fn) -> None:
    """Run *fn* on a daemon thread so callers (SSE turn loop) never block."""
    threading.Thread(target=fn, daemon=True).start()


def _send_now(title: str, body: str, url: str, tag) -> None:
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        # pywebpush is a hard requirement; this only fires in a broken install.
        logger.warning("pywebpush not installed — Web Push disabled despite being enabled")
        return

    keys = _load_or_create_keys()
    private_key = keys.get("private")
    if not private_key:
        logger.warning("No VAPID private key available — cannot send Web Push")
        return

    with _PUSH_WRITE_LOCK:
        subs = list(_load_subscriptions())
    if not subs:
        return

    payload = json.dumps(
        {"title": title, "body": body, "url": url, "tag": tag},
        ensure_ascii=False,
    )
    claims = {"sub": _vapid_subject()}
    stale: set = set()
    for sub in subs:
        if not isinstance(sub, dict) or not _endpoint(sub):
            continue
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims=dict(claims),
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                # Subscription gone — prune it.
                stale.add(_endpoint(sub))
            else:
                logger.debug("Web Push send failed (status=%s): %s", status, exc)
        except Exception:
            logger.debug("Unexpected Web Push send error", exc_info=True)

    _prune_endpoints(stale)


def send_web_push_to_all(title: str, body: str, url: str, tag=None) -> None:
    """Fan a notification out to every stored subscription, on a worker thread.

    Strict no-op when ``HERMES_WEBUI_PUSH_ENABLED`` is not truthy. Never blocks
    or raises in the caller.
    """
    if not push_enabled():
        return
    _dispatch(lambda: _send_now(title, body, url, tag))
