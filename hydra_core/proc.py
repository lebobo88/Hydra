"""UTF-8-safe subprocess helpers (E2-36).

``subprocess.run(..., text=True)`` decodes the child's stdout/stderr with
``locale.getpreferredencoding()``.  On Windows that is the ANSI codepage
(cp1252 on a US install), so any byte the codepage cannot map raises
``UnicodeDecodeError`` inside the reader thread — the thread dies and that
stream's content is lost silently.  Live evidence: a detached ingest run whose
codex-CLI stdout contained ``0x90`` killed ``Thread-5 (_readerthread)``.

Every text-mode subprocess call in this repo must therefore pin
``encoding="utf-8", errors="replace"``.  Use :func:`run_text` for new code;
``tests/test_no_bare_text_subprocess.py`` fails the build on any bare
``text=True``.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

__all__ = ["run_text"]


def run_text(cmd: Any, **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    """``subprocess.run`` in text mode with UTF-8 decoding forced.

    Pins ``text=True, encoding="utf-8", errors="replace"`` so an undecodable
    byte degrades to U+FFFD instead of killing the reader thread, and merges
    ``PYTHONIOENCODING=utf-8`` into the child environment so a Python child
    *encodes* its own output as UTF-8 too.  When ``env`` is not supplied it is
    derived from ``os.environ`` (equivalent to the inherited environment).
    """
    env = kwargs.pop("env", None)
    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONIOENCODING"] = "utf-8"
    kwargs.pop("text", None)
    kwargs.pop("encoding", None)
    kwargs.pop("errors", None)
    return subprocess.run(
        cmd,
        env=child_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )
