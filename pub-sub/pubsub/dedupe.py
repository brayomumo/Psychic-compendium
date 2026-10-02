"""A bounded record of recently processed message ids.

At-least-once delivery means duplicates are normal. They come from a
publisher retrying after a lost confirm, or from a redelivery after an ack was
lost. Duplicates arrive close together in time, so remembering the most recent
N ids catches them in a fixed amount of memory.

This is per process and in memory: it forgets everything on restart, and it
does not protect against two consumers processing the same message. A
production consumer records the id atomically with its side effect, for
example under a unique constraint in the same database transaction.
"""

import collections


class RecentIds:
    """Remembers the last ``capacity`` ids, oldest evicted first."""

    def __init__(self, capacity: int) -> None:
        """Creates an empty record.

        Args:
            capacity: maximum number of ids kept; must be positive.

        Raises:
            ValueError: if capacity is not positive.
        """
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._ids: collections.OrderedDict[str, None] = (
            collections.OrderedDict()
        )

    def __contains__(self, message_id: object) -> bool:
        """True if the id was added and has not been evicted yet."""
        return message_id in self._ids

    def __len__(self) -> int:
        """Number of ids currently remembered."""
        return len(self._ids)

    def add(self, message_id: str) -> None:
        """Records an id, evicting the oldest one when full."""
        self._ids[message_id] = None
        self._ids.move_to_end(message_id)
        if len(self._ids) > self._capacity:
            self._ids.popitem(last=False)
