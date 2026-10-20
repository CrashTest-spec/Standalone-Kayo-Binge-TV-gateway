'use strict';
const $ = id => document.getElementById(id);
let channels = [], player, selected, busy = false;
// Foxtel guide sequence; channel numbers are used only for ordering, never labels.
const channelOrder = [
  'foxtel one','uktv','lifestyle channel','fox8','arena','showcase','crime','comedy',
  'seinfeld','classics','british','docplay','universal tv','real life','tlc',
  'lifestyle food','lifestyle home','real history','outdoor24','discovery channel',
  'real crime','investigation discovery','animal planet','discovery turbo',
  'csi channel','fashion tv','tvsn shopping','good.','movies premiere','movies family',
  'movies action','movies comedy','british cinema','aussie classics','movies pulp',
  'fox sports news','fox cricket','fox league','fox sports 503','fox footy',
  'fox sports 505','fox sports 506','fox sports 507','espn','espn2','racing.com',
  'news 24','news 24 weather','news 24 world','sky news uk','bbc news','cnn','ms now',
  'nbc news now','cnbc','bloomberg','al jazeera','cgtn','nhk world','gb news',
  'dreamworks','trending','kids','club','retro','cmc','max','australian played'
];
const channelRank = name => {
  const index = channelOrder.indexOf(name.toLowerCase().trim());
  return index < 0 ? channelOrder.length : index;
};
function status(message) { $('status').textContent = message; }
function failure(error) {
  const code = error?.code;
  const names = window.shaka ? Object.entries(shaka.util.Error.Code).filter(([,v]) => v === code).map(([k]) => k).join('') : '';
  status(`Playback failed${code ? ` (${names || code})` : ''}. ${error?.message || 'The browser or provider could not complete playback.'} Check Chrome’s protected-content setting and your active subscription. The provider may reject this browser or device.`);
}
function render() {
  const list = $('channels'); list.replaceChildren();
  const query = $('search').value.toLowerCase();
  const matches = channels.filter(c => c.name.toLowerCase().includes(query) && (!$('provider').value || c.provider === $('provider').value));
  for (const c of matches) {
    const button = document.createElement('button'); button.className = 'channel'; button.type = 'button';
    button.setAttribute('aria-pressed', String(selected === c)); button.disabled = busy;
    if (c.logo) { const img = document.createElement('img'); img.src = c.logo.includes('/freeview/abc-logo') ? c.logo + '?v=' + Math.floor(Date.now()/30000) : c.logo; img.alt = ''; img.loading = 'lazy'; img.onerror = () => img.remove(); button.append(img); }
    const label = document.createElement('span'); label.textContent = c.name;
    button.append(label);
    button.onclick = () => play(c); list.append(button);
  }
  if (!matches.length) list.textContent = 'No channels found.';
}
function schedule(c) {
  $('schedule').replaceChildren();
  const rows = c.schedule.filter(r => new Date(r.end) > new Date()).slice(0, 10);
  for (const row of rows) {
    const item = document.createElement('div'); item.className = 'schedule';
    const time = document.createElement('time'); time.textContent = new Date(row.start).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
    const title = document.createElement('span'); title.textContent = row.title;
    if (row.description) { const description = document.createElement('p'); description.textContent = row.description; description.style.cssText = 'color:#aaa;font-size:14px;line-height:1.5;margin:7px 0 0'; title.append(description); }
    item.append(time, title); $('schedule').append(item);
  }
  if (!rows.length) $('schedule').textContent = 'No upcoming programmes available.';
}
async function play(c) {
  if (busy || !player) return;
  busy = true; selected = c; render(); schedule(c); $('title').textContent = c.name; status('Connecting…');
  try {
    await player.unload();
    player.configure({drm:{servers:c.license ? {'com.widevine.alpha': new URL(c.license, location.href).href} : {}}});
    await player.load(c.stream, null, c.provider === 'freeview' ? 'application/x-mpegURL' : 'application/dash+xml');
    await $('video').play(); status(`Watching ${c.name}`);
  } catch (error) { if (error.name === 'NotAllowedError') status('Ready. Press Play in the video controls.'); else failure(error); }
  finally { busy = false; render(); }
}
async function init() {
  $('search').oninput = render; $('provider').onchange = render;
  $('stop').onclick = async () => { if (busy) return; if (player) await player.unload(); status('Playback stopped.'); };
  if (!window.isSecureContext) {
    $('notice').hidden = false;
    $('notice').textContent = 'Widevine requires HTTPS or localhost. On the gateway Mac, open http://localhost:8787 in Chrome. Other devices need a trusted HTTPS connection.';
  }
  try {
    const response = await fetch('/api/channels'); if (!response.ok) throw new Error(`Channel request failed (${response.status})`);
    const seen = new Set();
    channels = (await response.json()).filter(c => {
      const name = c.name.toLowerCase().replace(/[^a-z0-9]/g, '');
      if (name !== 'foxsportsnews') return true;
      if (seen.has(name)) return false;
      seen.add(name); return true;
    }).sort((a, b) => (a.provider === 'freeview' ? a.number - 1000 : channelRank(a.name)) - (b.provider === 'freeview' ? b.number - 1000 : channelRank(b.name)) || a.name.localeCompare(b.name));
    render();
  } catch (error) { $('channels').textContent = error.message; }
  if (!window.shaka) return status('The video player library could not load. Check your internet connection and reload.');
  shaka.polyfill.installAll();
  if (!isSecureContext || !navigator.requestMediaKeySystemAccess || !shaka.Player.isBrowserSupported()) return status('Open this page in Chrome on the gateway Mac at http://localhost:8787. Widevine is unavailable in this browser context.');
  try {
    await navigator.requestMediaKeySystemAccess('com.widevine.alpha', [{initDataTypes:['cenc'], videoCapabilities:[{contentType:'video/mp4; codecs="avc1.42E01E"'}]}]);
    player = new shaka.Player(); await player.attach($('video')); player.addEventListener('error', event => failure(event.detail));
  } catch (error) { status('Widevine is unavailable. Use Chrome with protected content enabled. ' + error.message); }
}
document.addEventListener('DOMContentLoaded', init);
// Refresh the shared channel's guide and time-dependent logo without interrupting video.
setInterval(async () => {
  try {
    const response = await fetch('/api/channels');
    if (!response.ok) return;
    const updated = await response.json();
    for (const channel of channels) {
      const fresh = updated.find(item => item.stream === channel.stream);
      if (fresh) { channel.schedule = fresh.schedule; channel.logo = fresh.logo; }
    }
    if (selected) schedule(selected);
    render();
  } catch (_) { /* Keep the current guide during temporary network failures. */ }
}, 30000);
