import io
import threading
from unittest.mock import Mock

import pytest

from demo.evaluation import progress as activity


class Terminal(io.StringIO):
    def __init__(self):
        super().__init__()
        self.dot_written = threading.Event()
        self.fail_on_dot = False

    def isatty(self):
        return True

    def write(self, text):
        if text == "." and self.fail_on_dot:
            self.dot_written.set()
            raise OSError("Terminal write failed")
        count = super().write(text)
        if text == ".":
            self.dot_written.set()
        return count


@pytest.fixture
def terminal(monkeypatch):
    output = Terminal()
    monkeypatch.setattr(activity, "_DOT_INTERVAL_SECONDS", 0.01)
    return output


def test_interactive_progress_grows_and_stops_before_returning(terminal, monkeypatch):
    monkeypatch.setattr(activity.sys, "stderr", terminal)
    with activity.progress("Evaluating responses"):
        assert terminal.dot_written.wait(timeout=2)
        workers = [thread for thread in threading.enumerate() if thread.name == "eval-progress"]
        assert len(workers) == 1
    assert not workers[0].is_alive()
    assert terminal.getvalue().startswith("Evaluating responses....")
    assert terminal.getvalue().endswith("\n")
    assert "\r" not in terminal.getvalue()
    assert "\x1b" not in terminal.getvalue()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_progress_stops_on_failure_or_interrupt(terminal, failure, monkeypatch):
    monkeypatch.setattr(activity.sys, "stderr", terminal)
    with pytest.raises(failure):
        with activity.progress("Reading chat logs"):
            assert terminal.dot_written.wait(timeout=2)
            workers = [thread for thread in threading.enumerate() if thread.name == "eval-progress"]
            raise failure("Stopped")
    assert workers
    assert all(not worker.is_alive() for worker in workers)
    assert terminal.getvalue().endswith("\n")


def test_progress_surfaces_terminal_errors(terminal, monkeypatch):
    monkeypatch.setattr(activity.sys, "stderr", terminal)
    terminal.fail_on_dot = True
    with pytest.raises(OSError, match="Terminal write failed"):
        with activity.progress("Evaluating responses"):
            assert terminal.dot_written.wait(timeout=2)
    assert not any(thread.name == "eval-progress" for thread in threading.enumerate())


def test_redirected_progress_is_one_plain_line_without_thread(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(activity.sys, "stderr", output)
    worker = Mock(side_effect=AssertionError("Non-interactive output must not animate"))
    monkeypatch.setattr(activity.threading, "Thread", worker)
    with activity.progress("Reading chat logs"):
        assert output.getvalue() == "Reading chat logs...\n"
    assert output.getvalue() == "Reading chat logs...\n"
    worker.assert_not_called()


def test_redirected_progress_preserves_exceptions(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(activity.sys, "stderr", output)
    with pytest.raises(RuntimeError, match="Query failed"):
        with activity.progress("Reading chat logs"):
            raise RuntimeError("Query failed")
    assert output.getvalue() == "Reading chat logs...\n"
