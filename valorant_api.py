import urllib.request
import urllib.error
import json
import time
import base64

BASE = "https://pd.{region}.a.pvp.net"
AUTH_BASE = "https://auth.riotgames.com"
CONTENT_BASE = "https://shared.{region}.a.pvp.net"

REGION_SHARDS = {
    "na": "na",
    "eu": "eu",
    "ap": "ap",
    "kr": "kr",
}

_CONTENT_CACHE = {}

def _request(url, headers=None, data=None, method="GET", retries=2):
    req = urllib.request.Request(url, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if data and isinstance(data, dict):
        data = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            body = resp.read()
            if body:
                return json.loads(body.decode("utf-8"))
            return {}
    except urllib.error.HTTPError as e:
        if e.code == 429 and retries > 0:
            time.sleep(1)
            return _request(url, headers=headers, data=data, method=method, retries=retries - 1)
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code}: {body[:200]}")

def refresh_auth(account):
    """Refresh tokens if expired. Returns new tokens dict or None."""
    if not account.get("access_token"):
        return None
    expires = account.get("expires_at") or 0
    if expires and time.time() < expires - 60:
        return None
    ssws = account.get("ssws")
    if not ssws:
        return None
    url = f"{AUTH_BASE}/api/v1/authorization"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "RiotGamesApi/21.0.0 (Windows; 10)",
        "Cookie": f"ssws={ssws}",
    }
    payload = {
        "client_id": "play-valorant-web-prod",
        "response_type": "token id_token",
        "redirect_uri": "https://playvalorant.com/opt_in",
        "scope": "account openid",
        "acr_values": "urn:riot:bronze",
    }
    try:
        resp = _request(url, headers=headers, data=payload, method="POST")
    except RuntimeError:
        return None
    if isinstance(resp, dict) and "response" in resp:
        params = resp["response"].get("parameters", {})
        uri = params.get("uri", "")
        if "access_token=" in uri:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(uri)
            fragment = parsed.fragment
            qs = parse_qs(fragment)
            access_token = qs.get("access_token", [None])[0]
            if access_token:
                return {
                    "access_token": access_token,
                    "expires_at": int(time.time()) + 3600,
                }
    return None

def fetch_storefront(account):
    region = account.get("region", "na")
    access_token = account.get("access_token")
    entitlements_token = account.get("entitlements_token")
    if not access_token or not entitlements_token:
        raise RuntimeError("missing tokens")
    puuid = _extract_puuid(access_token)
    url = f"{BASE}/store/v3/storefront/{puuid}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Riot-Entitlements-JWT": entitlements_token,
        "X-Riot-ClientPlatform": "ew0KICAgICJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KICAicGxhdGZvcm1PUyI6ICJXaW5kb3dzIg0KfQ==",
        "X-Riot-ClientVersion": "release-09.02-shipping-9-1234567",
    }
    data = _request(url, headers=headers)
    skins = _parse_storefront(data, region)
    return skins

def _extract_puuid(access_token):
    parts = access_token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid jwt")
    payload = parts[1]
    padding = 4 - len(payload) % 4
    if padding != 4:
        payload += "=" * padding
    decoded = base64.urlsafe_b64decode(payload)
    obj = json.loads(decoded)
    return obj["sub"]

def _fetch_content(region):
    if region in _CONTENT_CACHE:
        return _CONTENT_CACHE[region]
    url = f"{CONTENT_BASE}/content-service/v3/content"
    try:
        data = _request(url)
    except RuntimeError:
        return {}
    _CONTENT_CACHE[region] = data
    return data

def _parse_storefront(data, region):
    skins = []
    offers = data.get("SkinsPanelLayout", {}).get("SingleItemOffers", [])
    content = _fetch_content(region)
    item_lookup = {}
    for item in content.get("items", []):
        item_lookup[item.get("id")] = item.get("name", "Unknown")
    for offer in offers:
        item_id = offer.get("Offer", {}).get("Reward", {}).get("Item", {}).get("ID")
        name = item_lookup.get(item_id, "Unknown")
        cost = offer.get("Offer", {}).get("Cost", {}).get("85ca954a-41d9-512e-b074-1f4dfac73e63", 0)
        skins.append({"name": name, "cost": cost})
    return skins
