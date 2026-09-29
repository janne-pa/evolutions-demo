import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_DOT_INTERVAL_SECONDS = 1.0


@contextmanager
def progress(message: str) -> Iterator[None]:
    output = sys.stderr
    animated = output.isatty()
    print(f"{message}...", end="" if animated else "\n", file=output, flush=True)
    if not animated:
        yield
        return

    stopped = threading.Event()
    write_error: OSError | ValueError | None = None

    def animate() -> None:
        nonlocal write_error
        try:
            while not stopped.wait(_DOT_INTERVAL_SECONDS):
                print(".", end="", file=output, flush=True)
        except (OSError, ValueError) as error:
            write_error = error

    worker = threading.Thread(target=animate, name="eval-progress", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
        print(file=output, flush=True)
    if write_error is not None:
        raise write_error
