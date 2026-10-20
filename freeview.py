"""Melbourne OTA catalogue adapter, following AU Freeview 0.20.5's source."""
import gzip
import hashlib
import json
import re
import subprocess
import threading
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse


def fetch(url, headers=None):
    args = ['curl', '-fsSL', '--connect-timeout', '10', '--max-time', '30']
    for key, value in (headers or {}).items():
        args += ['-H', '{}: {}'.format(key, value)]
    result = subprocess.run(args + ['-w', '\n%{url_effective}', url], capture_output=True, timeout=35)
    if result.returncode:
        raise RuntimeError('Freeview upstream request failed ({})'.format(result.returncode))
    body, effective = result.stdout.rsplit(b'\n', 1)
    return body, effective.decode()


class FreeviewClient:
    def __init__(self):
        self.urls = {}
        self.lock = threading.Lock()
        self.abc_sources = {}
        self.abc_schedules = {}
        self.abc_logos = {}
        self.guide = {}
        self.guide_time = 0

    def full_guide(self):
        if time.time() - self.guide_time < 900:
            return self.guide
        try:
            body, _ = fetch("https://i.mjh.nz/au/Melbourne/epg.xml.gz")
            root = ET.fromstring(gzip.decompress(body))
            guide = {}
            for programme in root.findall("programme"):
                def stamp(value):
                    return datetime.strptime(value, "%Y%m%d%H%M%S %z").isoformat()
                row = {"Title": programme.findtext("title") or "",
                       "Start": stamp(programme.attrib["start"]), "End": stamp(programme.attrib["stop"]),
                       "Description": programme.findtext("desc") or "",
                       "EpisodeTitle": programme.findtext("sub-title") or ""}
                guide.setdefault(programme.attrib["channel"], []).append(row)
            self.guide = guide
            self.guide_time = time.time()
        except Exception:
            logging.getLogger("tv-gateway").warning("Freeview detailed guide unavailable; using available cached/compact guide")
            self.guide_time = time.time() - 840
        return self.guide

    @staticmethod
    def programme_is_real(row):
        title = (row.get("Title") or "").strip().lower()
        return bool(title) and not any(text in title for text in (
            "programs resume", "programmes resume", "off air", "off-air", "no programming"))

    def abc_active_service(self, now=None):
        # Feed schedules can contain each other's shows; never use them to tune video.
        return self.abc_service(now)

    def abc_logo(self):
        service = self.abc_active_service()
        with self.lock:
            url = self.abc_logos.get(service)
        if not url:
            raise KeyError("ABC logo unavailable")
        body, _ = fetch(url)
        mime = ("image/png" if body.startswith(b"\x89PNG") else
                "image/jpeg" if body.startswith(b"\xff\xd8") else
                "image/webp" if body[:4] == b"RIFF" else "image/svg+xml")
        return body, mime

    @staticmethod
    def abc_service(now=None):
        local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Australia/Melbourne"))
        minutes = local.hour * 60 + local.minute
        return "kids" if 240 <= minutes < 1170 else "family"

    def register(self, url, headers):
        key = hashlib.sha256((url + json.dumps(headers, sort_keys=True)).encode()).hexdigest()
        extension = urlparse(url).path.rsplit(".", 1)[-1].lower()
        if extension in ("m3u8", "ts", "mp4", "m4s", "aac", "vtt"):
            key += "." + extension
        with self.lock:
            self.urls[key] = (url, headers)
        return '/freeview/media/' + key

    def channels(self):
        body, _ = fetch('https://i.mjh.nz/au/Melbourne/tv.json.gz')
        data = json.loads(gzip.decompress(body))
        guide = self.full_guide()
        tiles = []
        for slug, channel in data.items():
            if slug == "mjh-racing-fast" or channel.get("name", "").lower().replace(" ", "") == "racing.com":
                continue
            number = channel.get('chno')
            # Numbered TV services only; 300-series entries are DVB radio.
            if not number or float(number) >= 300 or not channel.get('mjh_master'):
                continue
            rows = channel.get('programs') or data.get(channel.get('epg_id'), {}).get('programs') or []
            schedule = []
            for index, row in enumerate(rows):
                end = rows[index + 1][0] if index + 1 < len(rows) else row[0] + 3600
                schedule.append({'Title': row[1], 'Start': datetime.fromtimestamp(row[0], timezone.utc).isoformat(),
                                 'End': datetime.fromtimestamp(end, timezone.utc).isoformat()})
            schedule = guide.get(channel.get("epg_id") or slug) or schedule
            tiles.append({'AssetId': slug, 'Title': channel['name'], 'Number': number,
                          'Logo': channel.get('logo', ''), 'LinearSchedule': schedule,
                          'Stream': self.register(channel['mjh_master'], channel.get('headers') or {})})
        shared = [tile for tile in tiles if tile["AssetId"] in ("mjh-abc-kids", "mjh-abc-tv-plus")]
        if len(shared) == 2:
            for tile in shared:
                service = "kids" if tile["AssetId"] == "mjh-abc-kids" else "family"
                with self.lock:
                    self.abc_sources[service] = self.urls[tile["Stream"].rsplit("/", 1)[-1]]
                    self.abc_schedules[service] = tile["LinearSchedule"]
                    self.abc_logos[service] = tile["Logo"]
            combined = dict(next(tile for tile in shared if tile["AssetId"] == "mjh-abc-kids"))
            combined.update(AssetId="mjh-abc-kids-family", Title="ABC Kids/Family",
                            Stream="/freeview/media/abc-shared.m3u8",
                            Logo="/freeview/abc-logo")
            schedule = {}
            for tile in shared:
                service = "kids" if tile["AssetId"] == "mjh-abc-kids" else "family"
                for row in tile["LinearSchedule"]:
                    start = datetime.fromisoformat(row["Start"])
                    if self.programme_is_real(row):
                        schedule[(row["Start"], row["Title"])] = row
            combined["LinearSchedule"] = sorted(schedule.values(), key=lambda row: row["Start"])
            tiles = [tile for tile in tiles if tile not in shared] + [combined]
        return sorted(tiles, key=lambda tile: (float(tile['Number']), tile['Title']))

    def proxy(self, key):
        shared_master = key == "abc-shared.m3u8"
        shared_variant = re.fullmatch(r"abc-shared-(\d+)\.m3u8", key)
        if shared_master or shared_variant:
            service = self.abc_active_service()
            with self.lock:
                source = self.abc_sources.get(service)
            if not source:
                self.channels()
                source = self.abc_sources[service]
            url, headers = source
            body, effective = fetch(url, headers)
            variants = [line.strip() for line in body.decode('utf-8-sig').splitlines()
                        if line.strip() and not line.startswith("#")]
            if shared_variant:
                index = min(int(shared_variant.group(1)), len(variants) - 1)
                url = urljoin(effective, variants[index])
                body, effective = fetch(url, headers)
        else:
            with self.lock:
                entry = self.urls.get(key)
            if not entry:
                raise KeyError('Freeview stream expired; refresh the playlist')
            url, headers = entry
            body, effective = fetch(url, headers)
        if body.lstrip().startswith(b'#EXTM3U'):
            def rewrite(match):
                return 'URI="{}"'.format(self.register(urljoin(effective, match.group(1)), headers))
            lines = []
            variant_index = 0
            for line in body.decode('utf-8-sig').splitlines():
                if line and not line.startswith('#'):
                    if shared_master:
                        line = "/freeview/media/abc-shared-{}.m3u8".format(variant_index)
                        variant_index += 1
                    else:
                        line = self.register(urljoin(effective, line.strip()), headers)
                else:
                    line = re.sub(r'URI="([^"]+)"', rewrite, line)
                lines.append(line)
            return ('\n'.join(lines) + '\n').encode(), 'application/vnd.apple.mpegurl'
        mime = {'ts': 'video/mp2t', 'mp4': 'video/mp4', 'm4s': 'video/mp4',
                'aac': 'audio/aac', 'vtt': 'text/vtt'}.get(urlparse(effective).path.rsplit('.', 1)[-1], 'application/octet-stream')
        return body, mime
