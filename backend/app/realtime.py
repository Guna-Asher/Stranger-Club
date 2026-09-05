from __future__ import annotations

import json
import logging
import queue
import threading
from collections import defaultdict

from sqlalchemy.engine import make_url

logger = logging.getLogger("stranger_club.realtime")


def _libpq_url(database_url: str) -> str:
    """psycopg.connect() (used directly here, bypassing SQLAlchemy) expects
    a plain libpq connection string — it does not understand SQLAlchemy's
    "+driver" URL convention (e.g. postgresql+psycopg://...)."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)

NOTIFY_CHANNEL_PREFIX = "sc_event_"
MAX_SUBSCRIBERS_PER_EVENT = 500
MAX_TOTAL_SUBSCRIBERS = 2000
# How often the listener thread checks the pending LISTEN/UNLISTEN command
# queue between notifications — the ceiling on how long a brand-new topic's
# first subscriber waits before it's actually listening.
_POLL_INTERVAL_SECONDS = 0.5


class InProcessBroadcaster:
    """Development/SQLite fanout — correct only for a single process. Never
    used when DATABASE_URL is PostgreSQL (see main.py); kept for local dev
    parity with pre-Phase-3 behaviour."""

    def __init__(self):
        self.subscribers: dict[str, set[queue.Queue]] = defaultdict(set)
        self._total = 0

    def subscribe(self, public_id: str) -> queue.Queue | None:
        if self._total >= MAX_TOTAL_SUBSCRIBERS or len(self.subscribers[public_id]) >= MAX_SUBSCRIBERS_PER_EVENT:
            return None
        channel: queue.Queue = queue.Queue(maxsize=20)
        self.subscribers[public_id].add(channel)
        self._total += 1
        return channel

    def unsubscribe(self, public_id: str, channel: queue.Queue) -> None:
        if channel in self.subscribers.get(public_id, set()):
            self.subscribers[public_id].discard(channel)
            self._total -= 1

    def publish(self, public_id: str, event_type: str, payload: dict) -> None:
        message = {"type": event_type, "summary": payload}
        for channel in list(self.subscribers.get(public_id, set())):
            try:
                channel.put_nowait(message)
            except queue.Full:
                try:
                    channel.get_nowait(); channel.put_nowait(message)
                except queue.Empty:
                    pass

    def stop(self) -> None:
        pass


class PostgresBroadcaster:
    """Cross-instance realtime via PostgreSQL LISTEN/NOTIFY.

    A single background thread owns one dedicated, long-lived psycopg
    connection (outside the SQLAlchemy pool — LISTEN requires a connection
    that is never returned to a pool) and is the ONLY thread that ever
    touches it — psycopg connections are not safe for concurrent use from
    multiple threads. subscribe()/unsubscribe() (called from arbitrary
    request-handling threads) never call the connection directly; they push
    onto a thread-safe command queue that the listener thread drains
    between polling for notifications.

    publish() always goes through NOTIFY, even for this same instance's own
    subscribers — deliberately not delivered in-process directly, so there
    is exactly one delivery code path (avoids double-delivery from the
    issuing session also receiving its own NOTIFY back). The extra
    Postgres round-trip is milliseconds; this is a freshness optimisation,
    never the correctness path (see services.py — the database is always
    re-fetched on connect and on every SSE message).

    On listener reconnect (DB restart, network blip), every currently
    tracked channel is re-LISTENed and a synthetic RESYNC message is pushed
    to every locally-connected subscriber, forcing a fresh fetch — closing
    the window where notifications published elsewhere were lost while this
    instance's listener was down. Postgres does not queue notifications for
    a disconnected listener, so that loss is real and permanent for the
    messages themselves; RESYNC is what makes it harmless.
    """

    def __init__(self, database_url: str, *, psycopg_module=None):
        self._database_url = _libpq_url(database_url)
        self._psycopg = psycopg_module
        if self._psycopg is None:
            import psycopg
            self._psycopg = psycopg
        self._subscribers: dict[str, set[queue.Queue]] = defaultdict(set)
        self._lock = threading.Lock()
        self._total = 0
        self._conn = None
        self._commands: queue.Queue[tuple[str, str, threading.Event | None]] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sc-realtime-listener")
        self._thread.start()

    @staticmethod
    def _channel(public_id: str) -> str:
        return f"{NOTIFY_CHANNEL_PREFIX}{public_id}"

    def subscribe(self, public_id: str) -> queue.Queue | None:
        with self._lock:
            if self._total >= MAX_TOTAL_SUBSCRIBERS or len(self._subscribers[public_id]) >= MAX_SUBSCRIBERS_PER_EVENT:
                return None
            channel: queue.Queue = queue.Queue(maxsize=20)
            is_new_topic = not self._subscribers[public_id]
            self._subscribers[public_id].add(channel)
            self._total += 1
        if is_new_topic:
            # Block until PostgreSQL has actually confirmed the LISTEN,
            # bounded to _POLL_INTERVAL_SECONDS-scale latency — without
            # this, a publish() firing immediately after subscribe() (a real
            # and common sequence: a player's SSE connection opens right
            # after an action an organizer reviews within the same second)
            # could be sent before the LISTEN was registered and would be
            # permanently lost — PostgreSQL does not queue NOTIFY for a
            # not-yet-listening session.
            ack = threading.Event()
            self._commands.put(("LISTEN", public_id, ack))
            ack.wait(timeout=2)
        return channel

    def unsubscribe(self, public_id: str, channel: queue.Queue) -> None:
        is_now_empty = False
        with self._lock:
            if channel not in self._subscribers.get(public_id, set()):
                return
            self._subscribers[public_id].discard(channel)
            self._total -= 1
            if not self._subscribers[public_id]:
                del self._subscribers[public_id]
                is_now_empty = True
        if is_now_empty:
            self._commands.put(("UNLISTEN", public_id, None))

    def publish(self, public_id: str, event_type: str, payload: dict) -> None:
        body = json.dumps({"type": event_type, "summary": payload})
        if len(body) > 7900:  # stay well under Postgres's 8000-byte NOTIFY payload limit
            body = json.dumps({"type": event_type, "summary": None})
        try:
            with self._psycopg.connect(self._database_url, connect_timeout=3, autocommit=True) as conn:
                conn.execute("SELECT pg_notify(%s, %s)", (self._channel(public_id), body))
        except Exception:
            logger.warning("realtime_notify_failed public_id=%s", public_id, exc_info=True)

    # --- Listener thread internals — only ever touched from _run() ---------

    def _drain_commands(self) -> None:
        while True:
            try:
                action, public_id, ack = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._conn.execute(f'{action} "{self._channel(public_id)}"')
            except Exception:
                logger.warning("realtime_%s_failed public_id=%s", action.lower(), public_id, exc_info=True)
            finally:
                if ack is not None:
                    ack.set()

    def _handle_notify(self, notify) -> None:
        public_id = notify.channel[len(NOTIFY_CHANNEL_PREFIX):]
        try:
            message = json.loads(notify.payload)
        except (TypeError, ValueError):
            return
        with self._lock:
            channels = list(self._subscribers.get(public_id, set()))
        for channel in channels:
            try:
                channel.put_nowait(message)
            except queue.Full:
                try:
                    channel.get_nowait(); channel.put_nowait(message)
                except queue.Empty:
                    pass

    def _resync_all(self) -> None:
        with self._lock:
            topics = list(self._subscribers.items())
        for _public_id, channels in topics:
            for channel in list(channels):
                try:
                    channel.put_nowait({"type": "RESYNC", "summary": None})
                except queue.Full:
                    pass

    def _run(self) -> None:
        backoff = 1
        while not self._stop_event.is_set():
            try:
                self._conn = self._psycopg.connect(self._database_url, autocommit=True)
                logger.info("realtime_listener_connected")
                backoff = 1
                with self._lock:
                    topics = list(self._subscribers.keys())
                for public_id in topics:
                    self._commands.put(("LISTEN", public_id, None))
                self._drain_commands()
                self._resync_all()
                while not self._stop_event.is_set():
                    for notify in self._conn.notifies(timeout=_POLL_INTERVAL_SECONDS):
                        self._handle_notify(notify)
                    self._drain_commands()
            except Exception:
                if self._stop_event.is_set():
                    break
                logger.warning("realtime_listener_disconnected, reconnecting in %ss", backoff, exc_info=True)
                self._conn = None
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 30)

    def stop(self) -> None:
        self._stop_event.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._thread.join(timeout=2)
