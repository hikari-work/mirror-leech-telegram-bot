"""Reading the wait out of a telegram flood error.

``RPCError.value`` is whatever telegram sent with the error, converted to an int
when it looked like a number and kept as-is when it did not. For a flood wait it
always looked like one -- the error is named ``FLOOD_WAIT_<seconds>`` -- but the
attribute keeps the wider type, so every ``sleep(f.value * slack)`` in the bot
was arithmetic on something the signature says could be a string. That read
happens in five places, on three different multipliers, so it lives here once.
"""

from pyrogram.errors import FloodPremiumWait, FloodWait


def flood_seconds(flood: FloodWait | FloodPremiumWait) -> float:
    """How long telegram asked us to wait, in seconds.

    Only the number telegram sent: each caller adds its own slack on top, and
    they do not agree on how much. A flood wait carrying anything else is not
    something telegram sends, and 0 keeps such an answer from killing the
    transfer the way multiplying a string used to.
    """
    return float(flood.value) if isinstance(flood.value, int) else 0.0
