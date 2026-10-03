"""Pythonic ctypes wrapper around libsum (see sum.h).

ctypes leaves four jobs to the caller, and this module does each of them in
one place:

1. Declaring C signatures (``argtypes``/``restype``) so arguments are
   converted and checked instead of guessed.
2. Range-checking integers: ctypes silently truncates a Python int that does
   not fit the C type.
3. Turning C status codes into Python exceptions.
4. Owning callbacks: keeping the C function pointer alive while C can call
   it, and carrying exceptions raised inside it back to the caller, because
   ctypes would otherwise print and discard them while C keeps running. That
   includes the KeyboardInterrupt from Ctrl+C, which needs extra care (see
   _sigint_deferred).
"""

import contextlib
import ctypes
import functools
import operator
import signal
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType
from typing import SupportsIndex

__all__ = [
    "INT64_MAX",
    "INT64_MIN",
    "CProgressFn",
    "LibraryLoadError",
    "Progress",
    "SumError",
    "SumOverflowError",
    "SumStoppedError",
    "declare_signatures",
    "library_path",
    "load_library",
    "sum_range",
]

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

# Must match enum sum_status in sum.h.
_SUM_OK = 0
_SUM_ERR_OVERFLOW = 2
_SUM_STOPPED = 3

# C type: int (*)(int64_t index, int64_t partial_sum, void *user_data).
CProgressFn = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_int64, ctypes.c_int64, ctypes.c_void_p
)

# Python-side progress callback: (index, partial_sum) -> truthy to stop.
Progress = Callable[[int, int], object]


class LibraryLoadError(OSError):
    """The shared library is missing or cannot be loaded."""


class SumError(Exception):
    """Base class for errors reported by libsum."""


class SumOverflowError(SumError, OverflowError):
    """A partial sum did not fit in a C int64_t."""


class SumStoppedError(SumError):
    """The progress callback asked libsum to stop early.

    Attributes:
        partial: The sum up to and including the element at which the
            callback returned a truthy value.
    """

    def __init__(self, partial: int) -> None:
        """Initializes the error with the partial sum reached."""
        super().__init__(f"stopped by progress callback at partial={partial}")
        self.partial = partial


def library_path() -> Path:
    """Returns where ``make build`` puts the library for this platform."""
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    return Path(__file__).resolve().parent / "build" / f"libsum{suffix}"


def declare_signatures(lib: ctypes.CDLL) -> None:
    """Sets ``argtypes``/``restype`` for every libsum function.

    Without them ctypes guesses: a Python int is passed as a C int and the
    return value is read as a C int, which silently truncates 64-bit values.

    Args:
        lib: A handle to libsum (``ctypes.CDLL`` or ``ctypes.PyDLL``).
    """
    lib.sum_range.argtypes = (
        ctypes.c_int64,
        ctypes.c_int64,
        CProgressFn,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int64),
    )
    lib.sum_range.restype = ctypes.c_int


def load_library(path: Path) -> ctypes.CDLL:
    """Loads libsum and declares its C signatures.

    Args:
        path: Path to the shared library.

    Returns:
        The loaded library, ready to call.

    Raises:
        LibraryLoadError: The file is missing or is not a loadable library
            (for example, built for another architecture).
    """
    if not path.is_file():
        raise LibraryLoadError(
            f"{path} not found; run `make build` in {path.parent.parent}"
        )
    try:
        # CDLL releases the GIL for the duration of each call. PyDLL would
        # hold it, which is only right for C code that uses the Python C API.
        lib = ctypes.CDLL(str(path))
    except OSError as exc:
        raise LibraryLoadError(f"cannot load {path}: {exc}") from exc
    declare_signatures(lib)
    return lib


@functools.cache
def _library() -> ctypes.CDLL:
    # A failed load raises, and functools.cache does not cache exceptions, so
    # building the library and retrying works in a running process.
    return load_library(library_path())


def _as_int64(name: str, value: SupportsIndex) -> int:
    # operator.index accepts exactly what range() accepts: ints and int-like
    # objects, but not floats or strings.
    number = operator.index(value)
    if not INT64_MIN <= number <= INT64_MAX:
        # ctypes.c_int64(2**64 + 5) is silently 5; refuse instead.
        raise OverflowError(f"{name}={number} does not fit in a C int64_t")
    return number


@contextlib.contextmanager
def _sigint_deferred() -> Iterator[Callable[[], bool]]:
    """Holds back SIGINT while C may call into Python, then re-delivers it.

    Python runs signal handlers at the next bytecode boundary. During a ctypes
    call that is usually the first instruction of a callback, before any
    ``try`` in it is entered, so the KeyboardInterrupt escapes the callback,
    ctypes prints it as unraisable, and C carries on: Ctrl+C is lost. While
    this context is active, SIGINT only sets a flag; callbacks poll it and ask
    C to stop, and on exit the signal goes back to Python's default handler,
    which raises KeyboardInterrupt here, outside any C frame.

    Only Python's default handler is replaced. An ignored SIGINT must not stop
    C, and a custom handler is the application's to run, so both are left in
    place (a custom handler that raises is then subject to the race above).

    Yields:
        A function returning True once SIGINT has arrived.
    """
    pending = False

    def record(_signum: int, _frame: FrameType | None) -> None:
        nonlocal pending
        pending = True

    previous = signal.getsignal(signal.SIGINT)
    if previous is not signal.default_int_handler:
        yield lambda: False
        return
    try:
        signal.signal(signal.SIGINT, record)
    except ValueError:
        # Not the main thread. Python only runs handlers in the main thread,
        # so callbacks on this thread never see a KeyboardInterrupt anyway.
        yield lambda: False
        return
    try:
        yield lambda: pending
    finally:
        signal.signal(signal.SIGINT, previous)
        if pending:
            signal.raise_signal(signal.SIGINT)


def sum_range(
    start: SupportsIndex,
    stop: SupportsIndex,
    progress: Progress | None = None,
) -> int:
    """Returns ``sum(range(start, stop))``, computed in C.

    Args:
        start: First element, inclusive.
        stop: End of the range, exclusive. ``stop <= start`` sums to 0.
        progress: Called as ``progress(index, partial_sum)`` after each
            element is added, from the calling thread. Return a truthy value
            to stop early. Exceptions it raises stop the C loop and are
            re-raised here. With a callback, Ctrl+C stops the loop at the
            next callback; without one, Ctrl+C waits until C returns.

    Returns:
        The sum.

    Raises:
        TypeError: ``start`` or ``stop`` is not an integer.
        OverflowError: ``start`` or ``stop`` does not fit in int64_t.
        SumOverflowError: A partial sum does not fit in int64_t.
        SumStoppedError: ``progress`` returned a truthy value.
        LibraryLoadError: The library has not been built.
        KeyboardInterrupt: SIGINT arrived during the call (with the default
            handler installed).
    """
    c_start = _as_int64("start", start)
    c_stop = _as_int64("stop", stop)
    lib = _library()

    error: BaseException | None = None
    result = ctypes.c_int64()
    with _sigint_deferred() as interrupted:
        c_progress = CProgressFn()  # NULL: no callback.
        if progress is not None:
            callback = progress

            def trampoline(index: int, partial: int, _data: int | None) -> int:
                nonlocal error
                if interrupted():
                    return 1
                try:
                    wants_stop = callback(index, partial)
                except BaseException as exc:  # Cannot unwind through C.
                    error = exc
                    return 1
                return 1 if wants_stop or interrupted() else 0

            # This reference keeps the C function pointer alive until
            # sum_range returns. Were it collected while C still held the
            # pointer, C would jump into freed memory. libsum never keeps the
            # pointer after returning, so the call is the whole lifetime.
            c_progress = CProgressFn(trampoline)

        status: int = lib.sum_range(
            c_start, c_stop, c_progress, None, ctypes.byref(result)
        )
    # Leaving the block re-raises a deferred Ctrl+C, before anything below.

    if error is not None:
        try:
            raise error
        finally:
            # Break the frame <-> traceback reference cycle.
            error = None
    if status == _SUM_OK:
        return result.value
    if status == _SUM_STOPPED:
        raise SumStoppedError(result.value)
    if status == _SUM_ERR_OVERFLOW:
        raise SumOverflowError(
            f"sum of range({c_start}, {c_stop}) overflows int64_t"
        )
    # SUM_ERR_INVALID_ARG cannot happen: `result` is never NULL.
    raise SumError(f"libsum returned unexpected status {status}")
