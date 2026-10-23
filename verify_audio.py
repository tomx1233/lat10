"""Verify whether a Roblox audio asset is public, free, user-owned, and usable.

Used by `/verify` and `grant_audio` to validate Roblox audio asset IDs and URLs
before granting universe permissions or playing in-game.
"""

import json
import re
import sys
import urllib.error
import urllib.request
from typing import Optional, Tuple

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RobloxAudioVerifier/1.0"
DETAILS_URL = "https://www.roblox.com/assetmarketplace/getassetdetails?id={id}"
ECONOMY_URL = "https://economy.roblox.com/v2/assets/{id}/details"
DELIVERY_URL = "https://assetdelivery.roblox.com/v1/asset?id={id}"

AUDIO_TYPE_ID = 3
TYPE_NAMES = {
    3: "Audio",
    2: "Decal",
    12: "Audio",
    11: "Model",
    13: "Decal",
    10: "Model",
}


class AssetError(Exception):
    """Raised when an asset ID cannot be parsed or fails Roblox verification."""


def _get(url: str, timeout: int = 8) -> Tuple[int, bytes, dict]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(262144), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read(262144) if e.fp else b""
        hdrs = dict(e.headers) if e.headers else {}
        return e.code, body, hdrs
    except Exception:
        return 0, b"", {}


def extract_asset_id(raw: str) -> int:
    """Extract a positive integer Roblox asset ID from an ID string or Roblox URL."""
    if raw is None:
        raise AssetError("Empty input.")
    s = str(raw).strip().strip("<>@`").strip()
    if not s:
        raise AssetError("Empty input.")

    m = re.search(r"rbxasset(?:id)?://(\d+)", s, re.IGNORECASE)
    if not m:
        m = re.search(r"roblox\.com/(?:[a-z0-9_-]+/)+(\d+)", s, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        if val <= 0:
            raise AssetError("Asset ID must be positive.")
        return val

    if re.fullmatch(r"\d+", s):
        val = int(s)
        if val <= 0:
            raise AssetError("Asset ID must be positive.")
        return val

    raise AssetError(f"Could not pull a numeric asset ID out of `{raw}`.")


def _economy(asset_id: int) -> Optional[dict]:
    status, body, _ = _get(ECONOMY_URL.format(id=asset_id))
    if status == 200 and body:
        try:
            data = json.loads(body.decode("utf-8", "replace"))
            if isinstance(data, dict) and data.get("AssetId"):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _marketplace(asset_id: int) -> Optional[dict]:
    status, body, _ = _get(DETAILS_URL.format(id=asset_id))
    if status == 200 and body:
        try:
            data = json.loads(body.decode("utf-8", "replace"))
            if isinstance(data, dict) and data.get("Success") and data.get("Data"):
                return data["Data"]
        except json.JSONDecodeError:
            pass
    return None


def _delivery_verdict(asset_id: int) -> str:
    status, body, _ = _get(DELIVERY_URL.format(id=asset_id))
    text = body.decode("utf-8", "replace").lower() if body else ""
    if status == 200:
        return "deliverable"
    if "banned" in text or "moderated" in text or "copyright" in text:
        return "banned"
    if status == 409 or "archived" in text or "not approved for the requester" in text:
        return "private"
    if status in (401, 403):
        return "private"
    if status == 404:
        return "missing"
    return f"http_{status}"


def _type_name(tid) -> str:
    if tid is None:
        return "Unknown"
    return TYPE_NAMES.get(tid, f"Type {tid}")


def verify_asset(asset_id: int) -> dict:
    """Verify that `asset_id` is a public, free, user-created Roblox Audio asset."""
    eco = _economy(asset_id)
    mkt = _marketplace(asset_id)
    verdict = _delivery_verdict(asset_id)

    if not eco and not mkt:
        if verdict == "missing":
            raise AssetError(f"Asset `{asset_id}` does not exist on Roblox.")
        if verdict == "banned":
            raise AssetError(
                f"Asset `{asset_id}` was moderated or taken down (DMCA / content ban)."
            )
        raise AssetError(
            f"Could not reach Roblox metadata for `{asset_id}` (verdict: {verdict}). "
            "Try again in a moment."
        )

    asset_type_id = (eco or {}).get("AssetTypeId") or (mkt or {}).get("AssetTypeId")
    name = (eco or {}).get("Name") or (mkt or {}).get("Name") or "Unnamed"
    creator_obj = (eco or {}).get("Creator") or {}
    creator_name = (
        creator_obj.get("Name")
        or ((mkt or {}).get("Creator") or {}).get("Name")
        or "Unknown"
    )
    creator_type = (creator_obj.get("CreatorType") or "").lower()
    creator_id = creator_obj.get("CreatorTargetId") or creator_obj.get("Id")
    is_public = (eco or {}).get("IsPublicDomain")
    for_sale = (eco or {}).get("IsForSale")
    price = (eco or {}).get("PriceInRobux") or 0
    is_banned = (mkt or {}).get("IsBanned") or False

    if is_banned or verdict == "banned":
        raise AssetError(
            f"`{name}` (`{asset_id}`) is **moderated / taken down** on Roblox and won't play."
        )

    if asset_type_id != AUDIO_TYPE_ID:
        raise AssetError(
            f"`{name}` (`{asset_id}`) is a **{_type_name(asset_type_id)}**, not an Audio asset."
        )

    if creator_type == "group":
        raise AssetError(
            f"`{name}` (`{asset_id}`) was uploaded by a **group** (**{creator_name}**), "
            "not a user — only user-owned audio is supported."
        )

    if for_sale or (price and price > 0):
        raise AssetError(
            f"`{name}` (`{asset_id}`) is a **paid** asset ({price} Robux), not free."
        )

    if is_public is False and verdict != "deliverable":
        raise AssetError(
            f"`{name}` (`{asset_id}`) by **{creator_name}** is **private** — since Roblox's "
            "2022 audio privacy update, non-public audio only plays inside experiences its "
            "creator explicitly granted permission to."
        )

    if verdict == "private" and not is_public:
        raise AssetError(
            f"`{name}` (`{asset_id}`) by **{creator_name}** is **restricted / archived** and "
            "not playable in arbitrary games."
        )

    note = ""
    if verdict == "private" and is_public:
        note = (
            " *(Marked public in catalog; Roblox requires universe-level audio "
            "permissions for some >6s tracks.)*"
        )

    return {
        "ok": True,
        "message": f"**{name}** (`{asset_id}`) by **{creator_name}** is a valid public Audio.{note}",
        "name": name,
        "creator": creator_name,
        "creator_id": creator_id,
        "asset_type": "Audio",
        "price": 0,
    }


def main(argv: list) -> int:
    if len(argv) < 2:
        print("Usage: python verify_audio.py <asset_id_or_url> [...]", file=sys.stderr)
        return 2
    rc = 0
    for arg in argv[1:]:
        try:
            aid = extract_asset_id(arg)
            res = verify_asset(aid)
            print(f"[OK]   {res['message']}")
        except AssetError as e:
            print(f"[FAIL] {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
