"""Server-side cron-completion detector for Web Push (#3196).

The client-side cron poll (``/api/cron/recent``) stops when the tab is hidden,
so background push for finished cron jobs needs a server-side trigger. This
daemon polls the same cron job state ``_handle_cron_recent`` reads, tracks the
last-seen completion timestamp per job in memory, and fires a Web Push when a
job newly completes (honouring the per-job ``toast_notifications`` flag).

Strict no-op when ``HERMES_WEBUI_PUSH_ENABLED`` is not truthy: the thread still
starts but every tick early-returns before touching cron state. Resilient: each
tick is wrapped in try/except so a transient cron read error can't kill it.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# Poll cadence: cron resolution is minutes, so 45s comfortably catches a
# completion without hammering the job store.
_POLL_INTERVAL_SECONDS = 45

_watcher_thread = None
_watcher_stop = threading.Event()
# job_id -> last completion timestamp we've already pushed for.
_seen_completion_ts: dict = {}
# Set on first tick so we don't blast a push for every job that completed before
# the server started (we only notify on completions observed while running).
_primed = False


def _push_enabled() -> bool:
    try:
        from api.push import push_enabled
        return push_enabled()
    except Exception:
        return False


def _list_completions() -> list:
    """Return [{job_id, name, status, completed_at, toast_notifications}] or []."""
    import datetime

    from cron.jobs import list_jobs

    jobs = list_jobs(include_disabled=True)
    out = []
    for job in jobs:
        last_run = job.get("last_run_at")
        if not last_run:
            continue
        if isinstance(last_run, str):
            try:
                ts = datetime.datetime.fromisoformat(
                    last_run.replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, TypeError):
                continue
        else:
            try:
                ts = float(last_run)
            except (ValueError, TypeError):
                continue
        out.append({
            "job_id": str(job.get("id", "") or ""),
            "name": job.get("name", "Unknown"),
            "status": job.get("last_status", "unknown"),
            "completed_at": ts,
            "toast_notifications": job.get("toast_notifications") is not False,
            "last_error": str(job.get("last_error") or "").strip(),
        })
    return out


def _cron_session_id(job_id: str) -> str:
    """Newest persisted cron session id for a job (``cron_{job_id}_{ts}``), or ''.

    Lets the push deep-link land on the actual run instead of the home view.
    Best-effort: returns '' on any lookup failure.
    """
    try:
        from api.routes import _latest_cron_session_info_for_jobs
        info = _latest_cron_session_info_for_jobs([job_id], [job_id]) or {}
        return str((info.get(job_id) or {}).get("session_id") or "")
    except Exception:
        logger.debug("cron push watcher: session lookup failed", exc_info=True)
        return ""


def _cron_output_snippet(session_id: str) -> str:
    """Short preview of the cron run's last assistant message, or ''."""
    if not session_id:
        return ""
    try:
        from api.models import get_session
        from api.streaming import _last_assistant_snippet
        session = get_session(session_id)
        if session is None:
            return ""
        return _last_assistant_snippet(session)
    except Exception:
        logger.debug("cron push watcher: snippet build failed", exc_info=True)
        return ""


def _tick() -> None:
    global _primed
    if not _push_enabled():
        return

    try:
        completions = _list_completions()
    except ImportError:
        return
    except Exception:
        logger.debug("cron push watcher: failed to read cron state", exc_info=True)
        return

    from api.push import send_web_push_to_all

    newly_seen = {}
    for c in completions:
        job_id = c.get("job_id") or ""
        ts = c.get("completed_at") or 0.0
        if not job_id:
            continue
        prev = _seen_completion_ts.get(job_id)
        newly_seen[job_id] = ts
        if prev is not None and ts > prev:
            # A genuinely newer run completed while we were watching.
            if not _primed:
                continue
            if not c.get("toast_notifications", True):
                continue
            name = c.get("name") or "Cron job"
            status = str(c.get("status") or "").strip().lower()
            ok = status not in ("failed", "error", "timeout")
            sid = _cron_session_id(job_id)
            url = f"./session/{sid}" if sid else "./"
            title = f"Cron complete: {name}" if ok else f"Cron failed: {name}"
            if ok:
                # Prefer the run's actual output; fall back to a generic line.
                body = _cron_output_snippet(sid) or "Scheduled task finished."
            else:
                err = str(c.get("last_error") or "").strip()
                err = " ".join(err.split())
                body = (err[:140] + ("…" if len(err) > 140 else "")) if err else "Scheduled task ended with an error."
            try:
                send_web_push_to_all(title, body, url, tag=f"cron-{job_id}")
            except Exception:
                logger.debug("cron push watcher: send failed", exc_info=True)

    # Replace the seen map so removed jobs don't linger.
    _seen_completion_ts.clear()
    _seen_completion_ts.update(newly_seen)
    _primed = True


def _run() -> None:
    while not _watcher_stop.is_set():
        try:
            _tick()
        except Exception:
            logger.debug("cron push watcher tick failed", exc_info=True)
        _watcher_stop.wait(_POLL_INTERVAL_SECONDS)


def start_cron_push_watcher() -> bool:
    """Start the daemon poller. Returns True if started. Idempotent."""
    global _watcher_thread
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return False
    _watcher_stop.clear()
    _watcher_thread = threading.Thread(
        target=_run, name="push-cron-watcher", daemon=True
    )
    _watcher_thread.start()
    return True


def stop_cron_push_watcher() -> None:
    _watcher_stop.set()
