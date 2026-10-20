# Standalone Kayo + Binge TV gateway

## Browser player

Combined playlists and the browser also include Melbourne terrestrial TV services.
Freeview uses HLS without the Binge/Kayo Widevine properties, preserves upstream
request headers through a local proxy, and contributes its available programmes
to the combined XMLTV guide. These are online simulcasts, not reception from a
physical antenna. Availability depends on the source feed and broadcaster.

On the gateway machine, open http://localhost:8787 in Chrome and select a channel.
The page provides channel search, service filters, upcoming programmes and native
video controls. Shaka Player loads from a pinned jsDelivr URL, so internet access
is required. Playback uses the browser's Widevine CDM and the existing licence
proxy; the provider must accept the device and subscription.

Widevine requires a secure context. Plain HTTP at a LAN IP is not sufficient:
other devices require trusted HTTPS for the page, manifest, segments and licence
proxy. Safari does not support this Widevine configuration.

## What it provides

- `http://YOUR_IP:8787/playlist.m3u8` — combined Kayo and Binge playlist
- `http://YOUR_IP:8787/kayo.m3u8` — Kayo only
- `http://YOUR_IP:8787/binge.m3u8` — Binge only
- `http://YOUR_IP:8787/epg.xml` — combined XMLTV guide
- `http://YOUR_IP:8787/kayo.xml` and `/binge.xml` — separate guides
- `/health` — health check

The `.m3u8` files are channel playlists. Kayo and Binge themselves supply Widevine-protected MPEG-DASH streams, not native HLS. The receiving player must support DASH and Widevine metadata from an extended M3U playlist. Kodi, Sparkle TV and some Android IPTV clients are suitable candidates; basic HLS-only players are not.

## How to run

```sh
python3 configure.py
./run.sh
```

The launcher creates a private Python environment, installs the small HTTP dependency, and starts the gateway on port 8787. Leave that terminal running, or configure your operating system to run `run.sh` at login.

Add these URLs to the IPTV application on each Tailscale device:

```text
http://YOUR_IP:8787/playlist.m3u8
http://YOUR_IP:8787/epg.xml
```

## Credentials and playback

Credentials remain in the local `.env` file and are never inserted into playlists. The gateway signs in lazily when a channel is opened, refreshes account tokens, forwards Widevine licence challenges, attaches CDN tokens to media requests, and maintains/relinquishes playback locks.

Only use this with your own active Kayo and Binge subscriptions and on devices you control. Concurrent-stream limits still apply.
