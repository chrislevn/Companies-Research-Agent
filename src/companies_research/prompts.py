"""User-editable prompts.

The built-in prompts are a starting point, not a policy. What counts as a useful
brief depends on the industry, the language and the person reading it, so each
one can be replaced by a file on disk without touching the code.

Two levers, because they answer different questions:

* ``prompts/<name>.md`` **replaces** the built-in prompt outright — for when the
  default is wrong for your work.
* ``<NAME>_PROMPT_EXTRA`` **appends** to whichever prompt is in use — for house
  rules ("we sell to logistics firms; always check fleet size") that belong
  alongside the default rather than instead of it.

Files are read on each use rather than at import, so editing one takes effect on
the next lookup with no restart — the same live behaviour as :data:`SETTINGS`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .config import SETTINGS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Prompt:
    """A prompt plus where it came from, so the UI and CLI can say."""

    text: str
    source: str          # "built-in" or the file path
    extra: str = ""      # appended house rules, if any

    @property
    def customised(self) -> bool:
        return self.source != "built-in" or bool(self.extra)


def prompt_path(name: str) -> Path:
    return SETTINGS.prompts_dir / f"{name}.md"


def load(name: str, default: str) -> Prompt:
    """The prompt to use for ``name``, falling back to ``default``."""
    text, source = default, "built-in"

    path = prompt_path(name)
    try:
        if path.is_file():
            custom = path.read_text(encoding="utf-8").strip()
            if custom:
                text, source = custom, str(path)
            else:
                # An empty file is almost always an accident mid-edit, and
                # silently sending an empty system prompt is a bad way to find
                # out. Keep the default and say so.
                log.warning("%s is empty — using the built-in prompt", path)
    except OSError as exc:
        log.warning("Could not read %s (%s) — using the built-in prompt", path, exc)

    extra = (os.getenv(f"{name.upper()}_PROMPT_EXTRA") or "").strip()
    if extra:
        text = f"{text}\n\n{extra}"

    return Prompt(text=text, source=source, extra=extra)


def scaffold(name: str, default: str, *, overwrite: bool = False) -> Path:
    """Write the built-in prompt to disk so it can be edited.

    Returns the path. Refuses to clobber an existing file unless asked, because
    the whole point of the file is that someone put work into it.
    """
    path = prompt_path(name)
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default.rstrip() + "\n", encoding="utf-8")
    return path
