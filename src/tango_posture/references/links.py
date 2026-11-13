"""Validated video provenance and explicit source-clock alignment."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})


_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


_FACEBOOK_HOSTS = frozenset({"facebook.com", "www.facebook.com", "m.facebook.com"})


_FACEBOOK_REEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")


_TIME_TOKEN_RE = re.compile(
    r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+(?:\.\d+)?)s?)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class YouTubeSourceLink:
    """Validated, canonicalised YouTube provenance link with optional nonnegative time anchor."""

    canonical_url: str
    video_id: str
    anchor_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_url": self.canonical_url,
            "video_id": self.video_id,
            "anchor_seconds": (
                round(float(self.anchor_seconds), 3)
                if self.anchor_seconds is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> YouTubeSourceLink | None:
        if not isinstance(payload, dict):
            return None
        video_id = str(payload.get("video_id") or "").strip()
        canonical_url = str(payload.get("canonical_url") or "").strip()
        if not _YOUTUBE_ID_RE.match(video_id) or not canonical_url:
            return None
        raw_anchor = payload.get("anchor_seconds")
        anchor: float | None = None
        if isinstance(raw_anchor, int | float) and math.isfinite(raw_anchor) and raw_anchor >= 0:
            anchor = float(raw_anchor)
        return cls(
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
            video_id=video_id,
            anchor_seconds=anchor,
        )


@dataclass(frozen=True)
class FacebookReelSourceLink:
    """Validated Facebook Reel URL and stable reel identifier."""

    canonical_url: str
    reel_id: str

    @property
    def video_id(self) -> str:
        return self.reel_id

    def to_dict(self) -> dict[str, str]:
        return {"canonical_url": self.canonical_url, "reel_id": self.reel_id}

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> FacebookReelSourceLink | None:
        if not isinstance(payload, dict):
            return None
        reel_id = str(payload.get("reel_id") or "").strip()
        if not _FACEBOOK_REEL_ID_RE.fullmatch(reel_id):
            return None
        parsed = parse_facebook_reel_url(str(payload.get("canonical_url") or ""))
        if parsed is None or parsed.reel_id != reel_id:
            return None
        return cls(
            canonical_url=parsed.canonical_url,
            reel_id=reel_id,
        )


@dataclass(frozen=True)
class YouTubeClockAlignment:
    """Explicit alignment between a local video source timestamp and a YouTube timestamp."""

    local_source_seconds: float
    youtube_seconds: float
    confirmed_same_edit: bool
    source_sha256: str
    youtube_video_id: str

    def is_valid_for(
        self,
        *,
        source_sha256: str,
        youtube_video_id: str,
        clip_in: float,
        clip_out: float,
        local_source_seconds: float | None = None,
    ) -> bool:
        if not self.confirmed_same_edit:
            return False
        if not source_sha256 or not youtube_video_id:
            return False
        if self.source_sha256 != source_sha256 or self.youtube_video_id != youtube_video_id:
            return False
        if not (
            math.isfinite(self.local_source_seconds)
            and math.isfinite(self.youtube_seconds)
            and self.local_source_seconds >= 0.0
            and self.youtube_seconds >= 0.0
        ):
            return False
        if not (clip_in - 1e-6 <= self.local_source_seconds <= clip_out + 1e-6):
            return False
        if local_source_seconds is not None:
            if not math.isfinite(local_source_seconds):
                return False
            if not (clip_in - 1e-6 <= local_source_seconds <= clip_out + 1e-6):
                return False
            mapped = self.youtube_seconds + (local_source_seconds - self.local_source_seconds)
            if mapped < -1e-6:
                return False
        return True

    def youtube_timestamp_at(
        self,
        local_source_seconds: float,
        *,
        source_sha256: str,
        youtube_video_id: str,
        clip_in: float,
        clip_out: float,
    ) -> float | None:
        if not self.is_valid_for(
            source_sha256=source_sha256,
            youtube_video_id=youtube_video_id,
            clip_in=clip_in,
            clip_out=clip_out,
            local_source_seconds=local_source_seconds,
        ):
            return None
        mapped = self.youtube_seconds + (local_source_seconds - self.local_source_seconds)
        return max(0.0, float(mapped))

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_source_seconds": round(float(self.local_source_seconds), 4),
            "youtube_seconds": round(float(self.youtube_seconds), 4),
            "confirmed_same_edit": bool(self.confirmed_same_edit),
            "source_sha256": self.source_sha256,
            "youtube_video_id": self.youtube_video_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> YouTubeClockAlignment | None:
        if not isinstance(payload, dict):
            return None
        try:
            local_sec = float(payload["local_source_seconds"])
            yt_sec = float(payload["youtube_seconds"])
            confirmed = bool(payload.get("confirmed_same_edit", False))
            sha = str(payload.get("source_sha256") or "")
            vid = str(payload.get("youtube_video_id") or "")
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(local_sec) and math.isfinite(yt_sec)):
            return None
        return cls(
            local_source_seconds=local_sec,
            youtube_seconds=yt_sec,
            confirmed_same_edit=confirmed,
            source_sha256=sha,
            youtube_video_id=vid,
        )


def _parse_time_anchor(raw_value: str) -> float:
    text = raw_value.strip()
    if not text:
        raise ValueError("Empty YouTube time anchor")
    if text.startswith("-"):
        raise ValueError("YouTube time anchor must be nonnegative")
    with_numeric: float | None = None
    try:
        with_numeric = float(text)
    except ValueError:
        with_numeric = None
    if with_numeric is not None:
        if not math.isfinite(with_numeric) or with_numeric < 0.0:
            raise ValueError("YouTube time anchor must be finite and nonnegative")
        return float(with_numeric)

    match = _TIME_TOKEN_RE.match(text)
    if not match or not any(match.group(k) for k in ("hours", "minutes", "seconds")):
        raise ValueError(f"Unrecognised YouTube time anchor: {raw_value!r}")
    hours = float(match.group("hours") or 0.0)
    minutes = float(match.group("minutes") or 0.0)
    seconds = float(match.group("seconds") or 0.0)
    total = hours * 3600.0 + minutes * 60.0 + seconds
    if not math.isfinite(total) or total < 0.0:
        raise ValueError("YouTube time anchor must be finite and nonnegative")
    return total


def parse_youtube_url(raw_url: str | None) -> YouTubeSourceLink | None:
    """Validate and canonicalise a user-entered YouTube URL without any network access.

    Accepts recognised HTTPS YouTube URLs (`youtube.com/watch?v=...`, `youtu.be/...`,
    `youtube.com/shorts/...`, including `www.` and `m.` hosts). Rejects lookalike domains,
    embedded credentials, and non-HTTPS schemes. Strips tracking and playlist parameters.
    """
    if raw_url is None:
        return None
    text = raw_url.strip()
    if not text:
        return None

    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError("YouTube link must use https://")
    if parts.username is not None or parts.password is not None:
        raise ValueError("YouTube link must not contain embedded credentials")
    host = (parts.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise ValueError(
            "Unrecognised YouTube domain; use https://www.youtube.com/watch?v=... or https://youtu.be/..."
        )
    if parts.port not in (None, 443):
        raise ValueError("YouTube link must not specify a custom port")

    query = parse_qs(parts.query, keep_blank_values=True)
    path_segments = [seg for seg in parts.path.split("/") if seg]
    video_id = ""
    if host == "youtu.be":
        if len(path_segments) != 1:
            raise ValueError("Short YouTube link must have the form https://youtu.be/<video_id>")
        video_id = path_segments[0]
    else:
        if path_segments == ["watch"]:
            v_vals = query.get("v", [])
            if len(v_vals) != 1:
                raise ValueError("YouTube watch link must include a single v=<video_id> parameter")
            video_id = v_vals[0]
        elif len(path_segments) == 2 and path_segments[0] == "shorts":
            video_id = path_segments[1]
        else:
            raise ValueError(
                "Unsupported YouTube URL path; use /watch?v=<id>, /shorts/<id>, or youtu.be/<id>"
            )

    if not _YOUTUBE_ID_RE.match(video_id):
        raise ValueError("YouTube video ID must be an 11-character identifier")

    anchor_seconds: float | None = None
    for key in ("t", "start"):
        if query.get(key):
            raw_anchor = query[key][0].strip()
            if raw_anchor:
                anchor_seconds = _parse_time_anchor(raw_anchor)
                break

    return YouTubeSourceLink(
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
        anchor_seconds=anchor_seconds,
    )


def parse_facebook_reel_url(raw_url: str | None) -> FacebookReelSourceLink | None:
    """Validate and canonicalize a Facebook Reel URL without network access."""
    if raw_url is None or not raw_url.strip():
        return None
    text = raw_url.strip()
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError("Facebook Reel link must use https://")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Facebook Reel link must not contain embedded credentials")
    host = (parts.hostname or "").lower()
    if host not in _FACEBOOK_HOSTS:
        raise ValueError("Use a Facebook Reel URL from facebook.com")
    if parts.port not in (None, 443):
        raise ValueError("Facebook Reel link must not specify a custom port")

    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) == 2 and segments[0].lower() in ("reel", "reels"):
        reel_id = segments[1]
        if not reel_id.isdigit() or not (5 <= len(reel_id) <= 32):
            raise ValueError("Facebook Reel ID must be numeric")
        canonical_path = f"/reel/{reel_id}/"
    elif len(segments) == 3 and segments[:2] == ["share", "r"]:
        reel_id = segments[2]
        canonical_path = f"/share/r/{reel_id}/"
    else:
        raise ValueError("Facebook link must use /reel/<id> or /share/r/<token>")
    if not _FACEBOOK_REEL_ID_RE.fullmatch(reel_id):
        raise ValueError("Facebook Reel ID or share token is malformed")
    return FacebookReelSourceLink(
        canonical_url=f"https://www.facebook.com{canonical_path}",
        reel_id=reel_id,
    )


def build_youtube_open_url(
    link: YouTubeSourceLink,
    *,
    timestamp_seconds: float | None = None,
) -> str:
    """Return the canonical YouTube watch URL with an optional integer timestamp parameter."""
    target = timestamp_seconds if timestamp_seconds is not None else link.anchor_seconds
    if target is not None and math.isfinite(target) and target >= 0.0:
        sec_int = round(max(0.0, float(target)))
        if sec_int > 0:
            return f"{link.canonical_url}&t={sec_int}s"
    return link.canonical_url
