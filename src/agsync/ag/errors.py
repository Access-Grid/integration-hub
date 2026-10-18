"""Telling an AccessGrid failure worth retrying from one that never will be.

Every failed provision costs three cycles before the retries run out, and
until now they all cost the same three whether the cause was a timeout or a
cardholder whose email address AccessGrid will never accept. That is the
wrong way round in both directions: a transient failure deserves more
patience than three cycles, and a permanent one deserves none — it should
stop immediately and be put in front of somebody, because nothing about
asking again will change the answer.

The SDK gives us only a message to go on. `AccessGridError` carries no
status code and no error type, so this reads the text, which is exactly as
fragile as it sounds. Two things keep that honest:

* **The default is to retry.** Anything unrecognised is treated as
  transient, so a message we have never seen costs a few cycles and then
  stops at the existing cap. Guessing "permanent" on an unfamiliar error
  would strand a credential that a retry would have fixed.
* **The classification is never the only record.** The message itself is
  stored and shown either way, so an operator sees what AccessGrid actually
  said rather than our opinion of it.
"""

from __future__ import annotations

import re
from enum import Enum


class ErrorKind(str, Enum):
    """What to do about a failure, rather than what caused it."""

    # Ask again later. Rate limits, server faults, anything about the
    # network, and everything unrecognised.
    RETRY = "retry"
    # Asking again cannot help. A rejected field, a state that forbids the
    # operation, something that already exists. Stop and tell somebody.
    PERMANENT = "permanent"


# Matched against the message, case-insensitively. Ordered most specific
# first, because "invalid" appears inside plenty of transient messages.
_PERMANENT = (
    # HID Origo refusing an operation for the pass's current state. Seen in
    # production: a pass already suspended cannot be suspended again, and no
    # amount of retrying changes that.
    r"pass_invalid_state",
    # AccessGrid rejecting a field outright. The email address on an issued
    # pass is the one that bites — it cannot be changed, because it is where
    # the pass was delivered.
    r"unexpected parameters",
    r"\bvalidation (failed|error)\b",
    r"\binvalid (email|phone|address|parameter|argument|template)\b",
    r"\balready (exists|issued|suspended|deleted)\b",
    r"\bnot found\b",
    r"\bforbidden\b",
    r"\bunauthorized\b",
    r"\bhttp (400|401|403|404|409|422)\b",
    r"\b(400|401|403|404|409|422)\b.*\b(bad request|conflict|unprocessable)\b",
)

_RETRY = (
    r"\btimed? ?out\b",
    r"\btimeout\b",
    r"\bconnection\b",
    r"\btemporar(y|ily)\b",
    r"\brate limit",
    r"\btoo many requests\b",
    r"\bhttp (408|425|429|5\d\d)\b",
    r"\b(500|502|503|504)\b",
    r"\bserver error\b",
)


def classify(error: object) -> ErrorKind:
    """Whether asking AccessGrid again could plausibly help.

    Takes an exception or anything stringable, so a caller can classify a
    message it has already turned into text for storage.
    """
    text = str(error or "").strip().lower()
    if not text:
        return ErrorKind.RETRY
    # Retryable patterns are checked first: a 503 whose body happens to
    # mention "not found" is still a 503.
    for pattern in _RETRY:
        if re.search(pattern, text):
            return ErrorKind.RETRY
    for pattern in _PERMANENT:
        if re.search(pattern, text):
            return ErrorKind.PERMANENT
    return ErrorKind.RETRY


def describe(kind: ErrorKind) -> str:
    """A phrase for the log, so the reasoning is visible and arguable."""
    if kind is ErrorKind.PERMANENT:
        return "asking again will not help, so this one stops here"
    return "worth another try on a later cycle"
