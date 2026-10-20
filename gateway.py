#!/usr/bin/env python3
"""Standalone Kayo/Binge IPTV gateway for a trusted Tailscale network."""

import argparse
import base64
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse
from xml.sax.saxutils import escape, quoteattr

import requests
from freeview import FreeviewClient


LOG = logging.getLogger("tv-gateway")
ROOT = Path(__file__).resolve().parent
USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 12; SHIELD Android TV Build/SR1A.211012.001)"
SIGN_IN_URL = "https://authentication-prod.ar.indazn.com/v5/SignIn"
REFRESH_URL = "https://ott-authz-bff-prod.ar.indazn.com/v5/RefreshAccessToken"
PLAYBACK_URL = "https://api.playback.indazn.com/v5/Playback"
LOCK_URL = "https://concurrency-v2.playback.indazn.com/v3/concurrency/lock/"
RAIL_URL = "https://rail-router.discovery.indazn.com/jp/v10/Rail"
IMAGE_URL = "https://image.discovery.indazn.com/jp/v3/{folder}/none/{id}/fill/none/top/none/80/{width}/{height}/{ext}/image?brand={brand}"

PROVIDERS = {
    "binge": {
        "brand": "BINGE",
        "entitlement": "base_binge_content",
        "rail": "Livetvschedule",
        "fallback_rail": "LinearChannels",
    },
    "kayo": {
        "brand": "KAYO",
        "entitlement": "tier_standard_kayo",
        "rail": "Livetvschedule",
        "fallback_rail": None,
    },
}

PLAYER_PARAMS = {
    "AppVersion": "0.150.3",
    "DrmType": "WIDEVINE",
    "Format": "MPEG-DASH",
    "PlayerId": "DAZN-androidtv",
    "Platform": "androidtv",
    "Model": "SHIELD Android TV",
    "Secure": "true",
    "Manufacturer": "NVIDIA",
    "PlayReadyInitiator": "false",
    "LanguageCode": "en",
}


CHANNEL_ORDER = (
    "foxtel one",
    "uktv",
    "lifestyle channel",
    "fox8",
    "arena",
    "showcase",
    "crime",
    "comedy",
    "seinfeld",
    "classics",
    "british",
    "docplay",
    "universal tv",
    "real life",
    "tlc",
    "lifestyle food",
    "lifestyle home",
    "real history",
    "outdoor24",
    "discovery channel",
    "real crime",
    "investigation discovery",
    "animal planet",
    "discovery turbo",
    "csi channel",
    "fashion tv",
    "tvsn shopping",
    "good.",
    "movies premiere",
    "movies family",
    "movies action",
    "movies comedy",
    "british cinema",
    "aussie classics",
    "movies pulp",
    "fox sports news",
    "fox cricket",
    "fox league",
    "fox sports 503",
    "fox footy",
    "fox sports 505",
    "fox sports 506",
    "fox sports 507",
    "espn",
    "espn2",
    "racing.com",
    "news 24",
    "news 24 weather",
    "news 24 world",
    "sky news uk",
    "bbc news",
    "cnn",
    "ms now",
    "nbc news now",
    "cnbc",
    "bloomberg",
    "al jazeera",
    "cgtn",
    "nhk world",
    "gb news",
    "dreamworks",
    "trending",
    "kids",
    "club",
    "retro",
    "cmc",
    "max",
    "australian played",
)
CHANNEL_RANK = {name: index for index, name in enumerate(CHANNEL_ORDER)}
# Verified against https://www.foxtel.com.au/tv-guide/grid (October 2026).
CHANNEL_NUMBERS = {"foxtel one":101,"uktv":105,"lifestyle channel":106,"fox8":108,"arena":111,"showcase":112,"crime":113,"comedy":114,"seinfeld":115,"classics":117,"british":118,"universal tv":120,"real life":121,"tlc":123,"lifestyle food":125,"lifestyle home":126,"real history":130,"outdoor24":131,"discovery channel":133,"real crime":135,"investigation discovery":136,"animal planet":141,"discovery turbo":143,"fashion tv":151,"tvsn shopping":176,"good.":182,"movies premiere":401,"movies family":403,"movies action":405,"movies comedy":406,"british cinema":413,"aussie classics":414,"movies pulp":415,"fox sports news":500,"fox cricket":501,"fox league":502,"fox sports 503":503,"fox footy":504,"fox sports 505":505,"fox sports 506":506,"fox sports 507":507,"espn":509,"espn2":510,"racing.com":529,"news 24":600,"news 24 weather":601,"news 24 world":603,"sky news uk":604,"bbc news":606,"cnn":609,"ms now":612,"nbc news now":613,"cnbc":620,"bloomberg":621,"al jazeera":651,"cgtn":653,"nhk world":656,"gb news":658,"dreamworks":717,"trending":801,"kids":802,"club":803,"retro":804,"cmc":805,"max":806,"australian played":807}


def load_env_file(path):
    """Load simple KEY=VALUE settings without executing them as shell code."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file(ROOT / ".env")


def env(name, default=""):
    return os.environ.get(name, default).strip()


def jwt_payload(token):
    value = token.split(".")[1]
    value += "=" * ((4 - len(value) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(value))


def add_query(url, **params):
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        query[key] = [value]
    return parsed._replace(query=urlencode(query, doseq=True)).geturl()


def safe_slug(value):
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")


def xmltv_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    return dt.strftime("%Y%m%d%H%M%S %z")


@dataclass
class PlaybackSession:
    provider: str
    manifest_url: str
    license_url: str
    token_name: str = ""
    token_value: str = ""
    lock_id: str = ""
    lock_interval: int = 0
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    last_refresh: float = field(default_factory=time.time)


class ProviderClient:
    def __init__(self, key):
        self.key = key
        self.config = PROVIDERS[key]
        self.brand = self.config["brand"]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "X-BRAND": self.brand})
        self.device_id = env("{}_DEVICE_ID".format(self.brand), secrets.token_hex(5))
        self.token = ""
        self.expires = 0
        self.dazn_id = ""
        self.entitlements = []
        self.auth_lock = threading.Lock()

    def credentials(self):
        prefix = self.brand
        username = env("{}_EMAIL".format(prefix)) or env("TV_EMAIL")
        password = env("{}_PASSWORD".format(prefix)) or env("TV_PASSWORD")
        if not username or not password:
            raise RuntimeError("Set {}_EMAIL and {}_PASSWORD (or TV_EMAIL/TV_PASSWORD)".format(prefix, prefix))
        return username, password

    def login(self):
        with self.auth_lock:
            if self.token and self.expires > time.time() + 300:
                return
            if self.token:
                data = self._curl_json("POST", REFRESH_URL, {"DeviceId": self.device_id})
                if data.get("AuthToken", {}).get("Token"):
                    self._save_token(data)
                    return
            username, password = self.credentials()
            data = self._curl_json(
                "POST",
                SIGN_IN_URL,
                {
                    "Email": username,
                    "Password": password,
                    "Platform": "web",
                    "DeviceId": self.device_id,
                    "Brand": self.brand,
                },
                extra_headers={"brand": self.brand},
            )
            self._save_token(data)

    def _curl_json(self, method, url, body=None, params=None, extra_headers=None):
        """Use macOS curl for DAZN control APIs; its TLS profile passes their WAF."""
        if params:
            url = add_query(url, **params)
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, "X-BRAND": self.brand}
        if self.token:
            headers.update({"Authorization": "Bearer {}".format(self.token), "X-DAZNID": self.dazn_id})
        headers.update(extra_headers or {})
        args = ["curl", "-sS", "-X", method, url]
        for name, value in headers.items():
            args.extend(["-H", "{}: {}".format(name, value)])
        payload = json.dumps(body if body is not None else {}) if method in ("POST", "PUT") else None
        result = subprocess.run(args + (["--data-binary", "@-"] if payload is not None else []), input=payload, text=True, capture_output=True, timeout=35)
        if result.returncode:
            raise RuntimeError("{} request failed: {}".format(self.brand, result.stderr.strip()))
        if not result.stdout.strip() and method == "DELETE":
            return {}
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("{} request returned an invalid response".format(self.brand)) from None
        if "odata.error" in data:
            raise RuntimeError("{} request failed: {}".format(self.brand, data["odata.error"]))
        return data

    def _save_token(self, data):
        token = (data.get("AuthToken") or {}).get("Token")
        if not token:
            raise RuntimeError("{} sign-in failed: {}".format(self.brand, data.get("Result") or data))
        payload = jwt_payload(token)
        status = (payload.get("productStatus") or {}).get(self.brand, "")
        if not status.startswith(("Active", "Partial", "FreeTrial")):
            raise RuntimeError("{} subscription is not active: {}".format(self.brand, status or data.get("Result")))
        self.token = token
        self.expires = int(payload["exp"])
        self.dazn_id = payload.get("user", "")
        self.entitlements = [x["id"] for x in payload.get("entitlements", {}).get("entitlementSets", [])]
        self.session.headers.update({"Authorization": "Bearer {}".format(token), "X-DAZNID": self.dazn_id})

    def channels(self):
        params = {
            "platform": "web",
            "id": self.config["rail"],
            "country": "au",
            "brand": self.key,
            "languageCode": "en",
        }
        data = self._curl_json("GET", RAIL_URL, params=params)
        tiles = data.get("Tiles") or []
        if not tiles and self.config.get("fallback_rail"):
            params["id"] = self.config["fallback_rail"]
            tiles = self._curl_json("GET", RAIL_URL, params=params).get("Tiles") or []
        return [tile for tile in tiles if tile.get("AssetId")]

    def playback(self, asset_id):
        self.login()
        params = dict(PLAYER_PARAMS)
        params["AssetId"] = asset_id
        headers = {"x-dazn-device": self.device_id, "x-correlation-id": str(uuid.uuid4())}
        data = self._curl_json("POST", PLAYBACK_URL, {}, params=params, extra_headers=headers)
        if not data.get("PlaybackDetails"):
            raise RuntimeError("{} playback failed: {}".format(self.brand, data))
        details = data["PlaybackDetails"][0]
        token = details.get("CdnToken") or {}
        manifest = details["ManifestUrl"]
        if token.get("Name"):
            manifest = add_query(manifest, **{token["Name"]: token["Value"]})
        lock = data.get("PlaybackLock") or {}
        return PlaybackSession(
            provider=self.key,
            manifest_url=manifest,
            license_url=details["LaUrl"],
            token_name=token.get("Name") or "",
            token_value=token.get("Value") or "",
            lock_id=lock.get("LockId") or "",
            lock_interval=int(lock.get("UpdateLockIntervalSeconds") or 0),
        )

    def refresh_lock(self, playback):
        if not playback.lock_id:
            return
        self.login()
        data = self._curl_json("PUT", LOCK_URL + "refresh/" + playback.lock_id, {})
        if data.get("LockId"):
            playback.last_refresh = time.time()

    def release_lock(self, playback):
        if playback.lock_id:
            try:
                self._curl_json("DELETE", LOCK_URL + playback.lock_id)
            except Exception:
                LOG.exception("Could not release %s playback lock", self.brand)


class Gateway:
    def __init__(self):
        self.clients = {key: ProviderClient(key) for key in PROVIDERS}
        self.clients["freeview"] = FreeviewClient()
        self.playbacks = {}
        self.channel_sessions = {}
        self.playback_lock = threading.Lock()
        self.channel_cache = {}
        self.cache_time = {}
        self.channel_cache_lock = threading.Lock()
        threading.Thread(target=self._maintain_locks, daemon=True).start()

    def channels(self, provider):
        with self.channel_cache_lock:
            now = time.time()
            if provider not in self.channel_cache or now - self.cache_time.get(provider, 0) > 120:
                try:
                    channels = self.clients[provider].channels()
                    if not channels:
                        raise RuntimeError("Provider returned no channels")
                    self.channel_cache[provider] = channels
                    self.cache_time[provider] = now
                except Exception:
                    if provider not in self.channel_cache:
                        raise
                    LOG.warning("%s catalogue refresh failed; serving cached channels", provider)
                    self.cache_time[provider] = now - 90
            return self.channel_cache[provider]

    def channel_playback(self, provider, asset_id):
        channel_key = (provider, asset_id)
        with self.playback_lock:
            session_id = self.channel_sessions.get(channel_key)
            playback = self.playbacks.get(session_id)
            if playback:
                playback.last_used = time.time()
                return session_id, playback
        playback = self.clients[provider].playback(asset_id)
        session_id = secrets.token_urlsafe(18)
        with self.playback_lock:
            old_id = self.channel_sessions.get(channel_key)
            old = self.playbacks.get(old_id)
            if old:
                self.clients[provider].release_lock(playback)
                old.last_used = time.time()
                return old_id, old
            self.playbacks[session_id] = playback
            self.channel_sessions[channel_key] = session_id
        return session_id, playback

    def get_playback(self, session_id):
        with self.playback_lock:
            playback = self.playbacks.get(session_id)
            if playback:
                playback.last_used = time.time()
            return playback

    def _maintain_locks(self):
        while True:
            time.sleep(15)
            now = time.time()
            with self.playback_lock:
                items = list(self.playbacks.items())
            for session_id, playback in items:
                client = self.clients[playback.provider]
                if now - playback.last_used > 180:
                    client.release_lock(playback)
                    with self.playback_lock:
                        self.playbacks.pop(session_id, None)
                        for channel_key, mapped_id in list(self.channel_sessions.items()):
                            if mapped_id == session_id:
                                self.channel_sessions.pop(channel_key, None)
                elif playback.lock_interval and now - playback.last_refresh >= max(playback.lock_interval - 15, 30):
                    try:
                        client.refresh_lock(playback)
                    except Exception:
                        LOG.exception("Playback lock refresh failed")


GATEWAY = Gateway()


class Handler(BaseHTTPRequestHandler):
    server_version = "KayoBingeGateway/1.0"

    def do_GET(self):
        try:
            self._get()
        except Exception as error:
            LOG.exception("GET %s failed", self.path)
            self.send_error(500, str(error))

    def do_POST(self):
        try:
            self._post()
        except Exception as error:
            LOG.exception("POST %s failed", self.path)
            self.send_error(500, str(error))

    def base_url(self):
        return env("PUBLIC_BASE_URL") or "http://{}".format(self.headers.get("Host", "127.0.0.1:8787"))

    def send_bytes(self, data, content_type, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _get(self):
        path = urlparse(self.path).path
        if path == "/foxtel-live.apk":
            apk = ROOT / "foxtel-live.apk"
            if not apk.is_file():
                return self.send_error(404, "Android TV APK is not installed in this gateway folder")
            return self.send_bytes(apk.read_bytes(), "application/vnd.android.package-archive",
                                   headers={"Content-Disposition": 'attachment; filename="foxtel-live-android-tv-1.3.1.apk"'})
        if path == "/freeview/abc-logo":
            GATEWAY.channels("freeview")
            data, mime = GATEWAY.clients["freeview"].abc_logo()
            return self.send_bytes(data, mime)
        if path.startswith("/freeview/channel/"):
            slug = path.rsplit("/", 1)[-1].removesuffix(".m3u8")
            tile = next((tile for tile in GATEWAY.channels("freeview") if tile["AssetId"] == slug), None)
            if not tile:
                return self.send_error(404)
            data, mime = GATEWAY.clients["freeview"].proxy(tile["Stream"].rsplit("/", 1)[-1])
            return self.send_bytes(data, mime)
        if path.startswith("/freeview/media/"):
            data, mime = GATEWAY.clients["freeview"].proxy(path.rsplit("/", 1)[-1])
            return self.send_bytes(data, mime)
        if path in ("/", "/player.js"):
            filename = "player.html" if path == "/" else "player.js"
            content_type = "text/html; charset=utf-8" if path == "/" else "application/javascript"
            return self.send_bytes((ROOT / filename).read_bytes(), content_type)
        if path == "/api/channels":
            channels = []
            for provider in ("freeview", "binge", "kayo"):
                for tile in GATEWAY.channels(provider):
                    asset_id = quote(tile["AssetId"], safe="")
                    channels.append({
                        "name": tile.get("Title") or tile["AssetId"],
                        "provider": provider,
                        "logo": self.logo(provider, tile, ""),
                        "stream": ("/freeview/channel/{}.m3u8".format(asset_id) if provider == "freeview" else "/stream/{}/{}.mpd".format(provider, asset_id)),
                        "license": None if provider == "freeview" else "/license/{}/{}".format(provider, asset_id),
                        "number": tile.get("Number"),
                        "schedule": [{"title": row.get("Title") or "", "description": row.get("Description") or "", "start": row["Start"], "end": row["End"]}
                                     for row in self.schedule_rows(tile.get("LinearSchedule") or {})],
                    })
            return self.send_bytes(json.dumps(channels).encode(), "application/json")
        if path == "/health":
            return self.send_bytes(b'{"status":"ok"}\n', "application/json")
        if path == "/":
            base = self.base_url()
            html = """<!doctype html><html><head><meta name=viewport content=\"width=device-width\"><title>Kayo + Binge Gateway</title><style>body{{font:18px system-ui;max-width:720px;margin:48px auto;padding:0 20px;background:#101116;color:#eee}}a{{display:block;color:#8cc8ff;padding:10px 0}}code{{color:#b5f5c8}}</style></head><body><h1>Kayo + Binge Gateway</h1><p>The gateway is running.</p><a href=\"{base}/playlist.m3u8\">Kodi playlist</a><a href=\"{base}/tivimate.m3u8\">TiviMate playlist</a><a href=\"{base}/binge.m3u8\">Binge playlist</a><a href=\"{base}/kayo.m3u8\">Kayo playlist</a><a href=\"{base}/epg.xml\">Combined EPG</a><p>Add the matching playlist and EPG URLs to a DASH/Widevine-compatible IPTV app. A web browser or VLC cannot play the protected channels.</p></body></html>""".format(base=base)
            return self.send_bytes(html.encode(), "text/html; charset=utf-8")
        if path in ("/playlist.m3u8", "/all.m3u8"):
            return self.send_bytes(self.playlist(("freeview", "binge", "kayo")).encode(), "text/plain; charset=utf-8")
        if path == "/tivimate.m3u8":
            return self.send_bytes(self.playlist(("freeview", "binge", "kayo"), client="tivimate").encode(), "text/plain; charset=utf-8")
        if path in ("/binge.m3u8", "/kayo.m3u8"):
            return self.send_bytes(self.playlist((path[1:-5],)).encode(), "text/plain; charset=utf-8")
        if path == "/epg.xml":
            return self.send_bytes(self.epg(("freeview", "binge", "kayo")).encode(), "application/xml")
        if path in ("/binge.xml", "/kayo.xml"):
            return self.send_bytes(self.epg((path[1:-4],)).encode(), "application/xml")
        if path.startswith("/logos/binge/"):
            filename = Path(path).name
            logo = ROOT / "logos" / "binge" / filename
            if not logo.is_file() or logo.suffix.lower() != ".png":
                return self.send_error(404)
            return self.send_bytes(logo.read_bytes(), "image/png", headers={"Cache-Control": "public, max-age=86400"})
        match = re.fullmatch(r"/stream/(binge|kayo)/([^/]+)\.mpd", path)
        if match:
            session_id, playback = GATEWAY.channel_playback(match.group(1), match.group(2))
            return self.proxy_manifest(session_id, playback)
        match = re.fullmatch(r"/media/([^/]+)/([^/]+)/(.*)", path)
        if match:
            return self.proxy_media(*match.groups(), urlparse(self.path).query)
        self.send_error(404)

    def _post(self):
        path = urlparse(self.path).path
        match = re.fullmatch(r"/license/(binge|kayo)/([^/]+)", path)
        if match:
            _, playback = GATEWAY.channel_playback(match.group(1), match.group(2))
        else:
            legacy = re.fullmatch(r"/license/([^/]+)", path)
            playback = GATEWAY.get_playback(legacy.group(1)) if legacy else None
        if not playback:
            return self.send_error(404, "Playback session expired")
        size = int(self.headers.get("Content-Length", "0"))
        challenge = self.rfile.read(size)
        client = GATEWAY.clients[playback.provider]
        args = [
            "curl", "-sS", "-X", "POST", playback.license_url,
            "-H", "User-Agent: {}".format(USER_AGENT),
            "-H", "X-BRAND: {}".format(client.brand),
            "-H", "X-DAZNID: {}".format(client.dazn_id),
            "-H", "Content-Type: {}".format(self.headers.get("Content-Type", "application/octet-stream")),
            "--data-binary", "@-",
        ]
        response = subprocess.run(args, input=challenge, capture_output=True, timeout=35)
        if response.returncode:
            raise RuntimeError("Widevine licence request failed")
        return self.send_bytes(response.stdout, "application/octet-stream")

    def playlist(self, providers, client="kodi"):
        base = self.base_url()
        lines = ["#EXTM3U"]
        channels = [(provider, tile) for provider in providers for tile in GATEWAY.channels(provider)]
        channels.sort(key=lambda row: (
            float(row[1]["Number"]) - 1000 if row[0] == "freeview" else CHANNEL_RANK.get((row[1].get("Title") or "").lower().strip(), len(CHANNEL_ORDER)),
            (row[1].get("Title") or "").lower(),
        ))
        seen = set()
        for provider, tile in channels:
                channel_name = re.sub(r"[^a-z0-9]", "", (tile.get("Title") or "").lower())
                if channel_name == "foxsportsnews" and channel_name in seen:
                    continue
                seen.add(channel_name)
                asset_id = tile["AssetId"]
                name = tile.get("Title") or asset_id
                logo = self.logo(provider, tile, base)
                if provider == "freeview":
                    lines.extend([
                        '#EXTINF:-1 tvg-id="{}" tvg-name="{}" tvg-logo="{}" tvg-chno="{}" group-title="Foxtel Live",{}'.format(
                            escape(asset_id), escape(name), escape(logo), tile["Number"], escape(name)),
                        "{}/freeview/channel/{}.m3u8".format(base, quote(asset_id, safe="")),
                    ])
                    continue
                stream = "{}/stream/{}/{}.mpd".format(base, provider, quote(asset_id, safe=""))
                license_url = "{}/license/{}/{}".format(base, provider, quote(asset_id, safe=""))
                # Kodi, Sparkle and some Android IPTV clients understand these Widevine hints.
                properties = [
                    "#KODIPROP:inputstream=inputstream.adaptive",
                    "#KODIPROP:inputstream.adaptive.manifest_type=mpd",
                    "#KODIPROP:inputstream.adaptive.license_type=com.widevine.alpha",
                    "#KODIPROP:inputstream.adaptive.license_key={}".format(license_url),
                ]
                if client == "kodi":
                    properties[1:1] = [
                        "#KODIPROP:mimetype=application/dash+xml",
                        "#KODIPROP:inputstream.adaptive.drm_legacy=com.widevine.alpha|{}|".format(license_url),
                    ]
                lines.extend(
                    [
                        '#EXTINF:-1 tvg-id="{id}" tvg-name="{name}" tvg-logo="{logo}"{number} group-title="{group}",{name}'.format(
                            id=escape(asset_id), name=escape(name), logo=escape(logo), group="Foxtel Live",
                            number=(' tvg-chno="{}"'.format(CHANNEL_NUMBERS[name.lower().strip()])
                                    if name.lower().strip() in CHANNEL_NUMBERS else ""),
                        ),
                    ] + properties + [stream]
                )
        return "\n".join(lines) + "\n"

    def logo(self, provider, tile, base):
        if provider == "freeview":
            logo = tile.get("Logo") or ""
            return base + logo if logo.startswith("/") else logo
        if provider == "binge":
            local = ROOT / "logos" / "binge" / (safe_slug(tile.get("Title")) + ".png")
            if local.is_file():
                return "{}/logos/binge/{}".format(base, quote(local.name))
        image = tile.get("LogoImage") or tile.get("Image") or {}
        if not image.get("Id"):
            return ""
        ext = "png" if image.get("ImageMimeType") == "png" else "jpg"
        return IMAGE_URL.format(folder="linear-channel", id=image["Id"], width=512, height=512, ext=ext, brand=provider)

    def epg(self, providers):
        root = ET.Element("tv", {"generator-info-name": "Kayo Binge Standalone Gateway"})
        for provider in providers:
            for tile in GATEWAY.channels(provider):
                asset_id = tile["AssetId"]
                channel = ET.SubElement(root, "channel", {"id": asset_id})
                ET.SubElement(channel, "display-name").text = tile.get("Title") or asset_id
                logo = self.logo(provider, tile, self.base_url())
                if logo:
                    ET.SubElement(channel, "icon", {"src": logo})
                for row in self.schedule_rows(tile.get("LinearSchedule") or {}):
                    if not row.get("Start") or not row.get("End"):
                        continue
                    programme = ET.SubElement(
                        root,
                        "programme",
                        {"channel": asset_id, "start": xmltv_time(row["Start"]), "stop": xmltv_time(row["End"])},
                    )
                    ET.SubElement(programme, "title").text = row.get("Title") or ""
                    if row.get("EpisodeTitle"):
                        ET.SubElement(programme, "sub-title").text = row["EpisodeTitle"]
                    if row.get("Description"):
                        ET.SubElement(programme, "desc").text = row["Description"]
                    for genre in row.get("Genre") or []:
                        if genre.get("name"):
                            ET.SubElement(programme, "category").text = genre["name"]
        return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")

    @staticmethod
    def schedule_rows(value):
        rows = []
        if isinstance(value, dict):
            if value.get("Start") and value.get("End"):
                rows.append(value)
            else:
                for child in value.values():
                    rows.extend(Handler.schedule_rows(child))
        elif isinstance(value, list):
            for child in value:
                rows.extend(Handler.schedule_rows(child))
        unique = {(row["Start"], row["End"], row.get("Title") or ""): row for row in rows}
        return sorted(unique.values(), key=lambda row: row["Start"])

    def proxy_manifest(self, session_id, playback):
        response = subprocess.run(
            ["curl", "-sS", "-L", "-H", "User-Agent: {}".format(USER_AGENT), playback.manifest_url],
            capture_output=True,
            timeout=35,
        )
        if response.returncode or b"<MPD" not in response.stdout[:2000]:
            raise RuntimeError("Provider manifest request failed")
        root = ET.fromstring(response.stdout)
        # Shaka expects an unprefixed MPD root; ElementTree otherwise emits ns0:MPD.
        if root.tag.startswith("{"):
            ET.register_namespace("", root.tag[1:].split("}", 1)[0])
        ET.register_namespace("cenc", "urn:mpeg:cenc:2013")
        base_nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "BaseURL"]
        if not base_nodes:
            namespace = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
            base_nodes = [ET.Element(namespace + "BaseURL")]
            root.insert(0, base_nodes[0])
        manifest_dir = playback.manifest_url.rsplit("/", 1)[0] + "/"
        for node in base_nodes:
            upstream = urljoin(manifest_dir, (node.text or "").strip())
            encoded = base64.urlsafe_b64encode(upstream.encode()).decode().rstrip("=")
            # Keep browser playback on its secure localhost/HTTPS origin.
            node.text = "/media/{}/{}/".format(session_id, encoded)
        data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        return self.send_bytes(data, "application/dash+xml")

    def proxy_media(self, session_id, encoded_base, remainder, downstream_query=""):
        playback = GATEWAY.get_playback(session_id)
        if not playback:
            return self.send_error(404, "Playback session expired")
        padded = encoded_base + "=" * ((4 - len(encoded_base) % 4) % 4)
        upstream_base = base64.urlsafe_b64decode(padded).decode()
        upstream = urljoin(upstream_base, remainder)
        if downstream_query:
            parsed = urlparse(upstream)
            combined = "&".join(value for value in (parsed.query, downstream_query) if value)
            upstream = parsed._replace(query=combined).geturl()
        if playback.token_name:
            upstream = add_query(upstream, **{playback.token_name: playback.token_value})
        headers = {"User-Agent": USER_AGENT}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        args = ["curl", "-sS", "-L", "-H", "User-Agent: {}".format(USER_AGENT)]
        if headers.get("Range"):
            args.extend(["-H", "Range: {}".format(headers["Range"])])
        args.append(upstream)
        process = subprocess.Popen(args, stdout=subprocess.PIPE)
        self.send_response(206 if headers.get("Range") else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = process.stdout.read(128 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            process.stdout.close()
            process.wait(timeout=10)
        if process.returncode:
            LOG.warning("Provider media request failed with curl status %s", process.returncode)

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=env("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(env("PORT", "8787")))
    args = parser.parse_args()
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOG.info("Serving Kayo/Binge playlists on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
