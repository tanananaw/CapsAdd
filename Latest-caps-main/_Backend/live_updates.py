"""Live updates for open pages, over Server-Sent Events (/api/live).

Two kinds of event:
  'data'          - something pages display changed (a booking, a status,
                    attendance, the roster...). Every page just re-fetches
                    what it shows; the event carries no payload.
  'notifications' - one specific account got an in-app notification.

In-process only (one server process, like the rest of this app). A page
whose stream drops simply reconnects (EventSource does that itself), and
each page keeps a slow fallback poll for anything missed meanwhile.
"""
import threading

# POSTs that don't change anything another page shows - announcing them
# would make every open page refetch for nothing (and /api/predict-ewt is
# itself called while a page refreshes, so it would loop).
READ_ONLY_POSTS = ('/api/predict-ewt', '/api/staff-evaluation', '/api/notifications/')

HEARTBEAT_SECONDS = 15
_MAX_BACKLOG = 200

_condition = threading.Condition()
_events = []  # (sequence number, kind, frozenset of user_ids or None for everyone)
_sequence = 0


def publish(kind, user_ids=None):
    global _sequence
    with _condition:
        _sequence += 1
        _events.append((_sequence, kind, frozenset(user_ids) if user_ids else None))
        del _events[:-_MAX_BACKLOG]
        _condition.notify_all()


def is_announced_write(method, path):
    return method != 'GET' and path.startswith('/api/') and not path.startswith(READ_ONLY_POSTS)


def stream(user_id):
    """Generator of SSE text for one open page. Events published in quick
    succession are coalesced (one 'data' is enough to trigger a refetch);
    a comment line every HEARTBEAT_SECONDS keeps proxies from closing an
    idle connection and lets the server notice a closed tab."""
    with _condition:
        last_seen = _sequence
    yield 'retry: 5000\n\n'
    while True:
        with _condition:
            _condition.wait_for(lambda: _sequence > last_seen, timeout=HEARTBEAT_SECONDS)
            fell_behind = bool(_events) and _events[0][0] > last_seen + 1
            pending = [e for e in _events if e[0] > last_seen]
            last_seen = _sequence

        kinds = ['data', 'notifications'] if fell_behind else []
        for _, kind, targets in pending:
            if (targets is None or user_id in targets) and kind not in kinds:
                kinds.append(kind)
        if not kinds:
            yield ': keep-alive\n\n'
        for kind in kinds:
            yield f'event: {kind}\ndata: {{}}\n\n'
