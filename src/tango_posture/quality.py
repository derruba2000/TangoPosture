import math

from .domain import Landmark


def reliable(p: Landmark | None, confidence: float) -> bool:
    return bool(
        p is not None
        and all(v is not None and math.isfinite(v) for v in (p.x, p.y, p.visibility, p.presence))
        and 0 <= p.x <= 1
        and 0 <= p.y <= 1
        and p.visibility is not None
        and confidence <= p.visibility <= 1
        and p.presence is not None
        and confidence <= p.presence <= 1
    )
