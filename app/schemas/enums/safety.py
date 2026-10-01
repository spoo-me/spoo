"""Safety verdict enums.

Tiering is keyed to reputation and legal cost, not to "is it spammy":
TOXIC destinations (credential phishing, malware, CSAM, AiTM proxies) are
blocked automatically; GRAY (spammy but visible marketing junk) lives on,
at most behind friction; BENIGN passes; UNCERTAIN awaits a human.
"""

from __future__ import annotations

from enum import Enum


class VerdictTier(str, Enum):
    TOXIC = "toxic"
    GRAY = "gray"
    BENIGN = "benign"
    UNCERTAIN = "uncertain"
