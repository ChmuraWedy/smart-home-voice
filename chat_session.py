"""Per-panel chat state, isolated from standalone Classic Chat sessions."""
import asyncio
from dataclasses import dataclass, field
from secrets import token_urlsafe
from time import monotonic

SESSION_TTL = 1800
MAX_SESSIONS = 256
MAX_TURNS = 80


@dataclass(frozen=True)
class ClarificationOption:
    label: str
    path: str | None = None
    room_key: tuple | None = None
    channel: str | None = None
    read_only: bool = False


@dataclass
class PendingClarification:
    transcript: str
    choices: dict[str, ClarificationOption] = field(default_factory=dict)


@dataclass
class Conversation:
    turns: list[dict[str, str]] = field(default_factory=list)
    targets: tuple = ()
    pending: PendingClarification | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class EmbedSession:
    dashboard_key: str
    conversation: Conversation = field(default_factory=Conversation)


class Sessions:
    def __init__(self):
        self._items = {}

    def _prune(self):
        now = monotonic()
        for key, (deadline, _) in list(self._items.items()):
            if deadline <= now:
                del self._items[key]

    def get(self, key):
        self._prune()
        entry = self._items.get(key)
        return entry[1] if entry else None

    def put(self, key, value):
        self._prune()
        while len(self._items) >= MAX_SESSIONS:
            del self._items[next(iter(self._items))]
        self._items[key] = (monotonic() + SESSION_TTL, value)
        return value

    def open(self, key):
        session_id = token_urlsafe(32)
        self.put(session_id, EmbedSession(key))
        return session_id

    def close(self, key):
        self._items.pop(key, None)
