"""Which cameras should Guard AI watch? (plug-and-play §"recommend the cameras").

A non-technical merchant shouldn't have to understand AI stream allocation.
Guard scores every working camera and pre-selects the best ones for the
licence's camera count. V1 signals, all honest and explainable:

  · the camera's name on the recorder ("Entrance", "Shop floor", "Store room")
  · how many people Guard saw in its picture during setup (busy = worth watching)
  · whether it works at all (offline and incompatible cameras are never picked)

The UI says "Recommended", never "Guard knows". The merchant can always change it.

With 2+ slots the pick covers both halves of theft protection when it can:
one camera on the products and one on the way out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (pattern, score, role). First match on the lower-cased name wins, so the
# specific phrases come before the general ones.
RULES: list[tuple[re.Pattern[str], int, str]] = [(re.compile(p), s, r) for p, s, r in [
    (r"back\s*door|rear\s*door|back\s*exit|fire\s*exit", 45, "EXIT"),
    (r"\b(exit|entrance|entry|door|doorway|gate|front)\b", 50, "EXIT"),
    (r"main\s*(shop|store|floor|hall)|shop\s*floor|sales\s*floor|showroom", 45, "PRODUCTS"),
    (r"\b(shelf|shelves|aisle|display|rack|gondola|section)\b", 40, "PRODUCTS"),
    (r"store\s*room|storeroom|stock\s*room|stockroom|warehouse|\bstore\b|\bstock\b|backstore|depot", 35, "STOCK"),
    (r"\b(shop|supermarket|mart|pharmacy|boutique)\b", 35, "PRODUCTS"),
    (r"\b(cash|cashier|till|counter|pos|checkout)\b", 20, "CASHIER"),
    (r"\b(outside|outdoor|street|road|compound|parking|car\s*park|yard|fence|perimeter)\b", 5, "OUTSIDE"),
    (r"\b(office|toilet|bath|restroom|kitchen|bedroom|staff\s*room|generator|gen)\b", -20, "OTHER"),
]]

ROLE_REASON = {
    "EXIT": "Covers a way out",
    "PRODUCTS": "Covers your products",
    "STOCK": "Covers your stock",
    "CASHIER": "Covers the till",
    "OUTSIDE": "Outside the shop",
    "OTHER": "Low theft risk",
}


@dataclass
class Scored:
    id: str
    name: str
    score: int
    role: str
    usable: bool
    reason: str
    recommended: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "score": self.score, "role": self.role, "usable": self.usable,
                "reason": self.reason, "recommended": self.recommended}


def classify(name: str | None) -> tuple[int, str]:
    n = (name or "").lower()
    for pat, score, role in RULES:
        if pat.search(n):
            return score, role
    return 10, "UNKNOWN"


def score_cameras(cameras: list[dict], people: dict[str, int]) -> list[Scored]:
    out = []
    for c in cameras:
        usable = bool(c.get("online")) and c.get("compatibility") != "INCOMPATIBLE" and (
            c.get("has_sub_stream") or c.get("has_main_stream"))
        base, role = classify(c.get("name"))
        seen = int(people.get(c["id"], 0))
        score = base + min(30, 10 * seen)
        if not usable:
            reason = "Not sending video"
        elif role in ROLE_REASON:
            reason = ROLE_REASON[role]
        elif seen:
            reason = f"{seen} {'person' if seen == 1 else 'people'} seen here"
        else:
            reason = "CCTV camera"
        out.append(Scored(c["id"], c.get("name") or f"Camera {c.get('channel_number', '')}".strip(), score, role,
                          usable, reason))
    return out


def recommend(cameras: list[dict], people: dict[str, int], slots: int) -> list[Scored]:
    """All cameras, best first, with the top `slots` usable ones marked recommended."""
    scored = score_cameras(cameras, people)
    order = {c["id"]: i for i, c in enumerate(cameras)}
    ranked = sorted(scored, key=lambda s: (not s.usable, -s.score, order[s.id]))
    usable = [s for s in ranked if s.usable]
    picked: list[Scored] = []
    if slots >= 2:
        # One on the products, one on the way out, when the shop has both.
        for role in ("PRODUCTS", "EXIT"):
            best = next((s for s in usable if s.role == role and s not in picked), None)
            if best:
                picked.append(best)
    for s in usable:
        if len(picked) >= slots:
            break
        if s not in picked:
            picked.append(s)
    for s in picked[:max(0, slots)]:
        s.recommended = True
    return ranked
