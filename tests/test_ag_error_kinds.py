"""Which AccessGrid failures are worth asking about again.

Every failed provision used to cost the same three cycles whether the cause
was a timeout or a cardholder whose email address AccessGrid will never
accept. That is wrong in both directions: a transient failure deserves more
patience than three cycles, and a permanent one deserves none — nothing
about asking again changes the answer, so it should stop and be put in front
of somebody.

The SDK gives only a message to go on, so this reads text, which is as
fragile as it sounds. The safeguard is the default: **anything unrecognised
is retried.** Guessing "permanent" on an unfamiliar message would strand a
credential that a retry would have fixed, and that is the expensive mistake.
"""

from __future__ import annotations

import pytest

from agsync.ag import AccessGridError
from agsync.ag.errors import ErrorKind, classify, describe

# =====================================================================
# Permanent
# =====================================================================


@pytest.mark.parametrize("message", [
    # Seen in production, on a pass HID would not suspend twice.
    'Origo 400: {"error": "PASS_INVALID_STATE"}',
    # AccessGrid refusing to change the address a pass was delivered to.
    "Unexpected parameters provided: email",
    "Validation failed: card_number",
    "invalid email address",
    "Card already exists for this template",
    "404 not found",
    "HTTP 422 unprocessable entity",
])
def test_these_will_never_succeed(message):
    assert classify(AccessGridError(message)) is ErrorKind.PERMANENT


def test_the_reasoning_is_put_into_words_for_the_log():
    """So the conclusion is visible and arguable rather than implicit."""
    assert "stops here" in describe(ErrorKind.PERMANENT)
    assert "another try" in describe(ErrorKind.RETRY)


# =====================================================================
# Worth retrying
# =====================================================================


@pytest.mark.parametrize("message", [
    "Request timed out",
    "timeout waiting for response",
    "Connection reset by peer",
    "429 too many requests",
    "HTTP 503 service unavailable",
    "500 server error",
    "The service is temporarily unavailable",
])
def test_these_might_succeed_later(message):
    assert classify(AccessGridError(message)) is ErrorKind.RETRY


# =====================================================================
# The default, which is the safeguard
# =====================================================================


@pytest.mark.parametrize("message", [
    "",
    "something nobody has seen before",
    "Origo 418: I am a teapot",
])
def test_anything_unrecognised_is_retried(message):
    """Guessing "permanent" on an unfamiliar message would strand a
    credential that a retry would have fixed."""
    assert classify(AccessGridError(message)) is ErrorKind.RETRY


def test_a_server_error_wins_over_a_phrase_inside_it():
    """A 503 whose body happens to mention "not found" is still a 503."""
    assert classify(
        AccessGridError("HTTP 503: upstream said not found")
    ) is ErrorKind.RETRY


def test_anything_stringable_can_be_classified():
    """Callers often have the message already, having stored it."""
    assert classify("PASS_INVALID_STATE") is ErrorKind.PERMANENT
    assert classify(None) is ErrorKind.RETRY
