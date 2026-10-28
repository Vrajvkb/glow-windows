"""
Glow v7 - ambient screen-edge light for Windows.

Same libraries as before (nothing new to install).
Run:  py glow_v7.py

Errors are saved to:   %APPDATA%\\GlowApp\\error.log
Settings are saved to: %APPDATA%\\GlowApp\\settings.json
"""
import sys
import os
import re
import json
import math
import time
import random
import bisect
import ctypes
import threading
import traceback
import collections
import urllib.request
import urllib.parse
import urllib.error
from ctypes import wintypes
from datetime import datetime, timezone

import numpy as np
import pyaudiowpatch as pyaudio
from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, QObject, QCoreApplication, pyqtSignal
from PyQt6.QtGui import (QColor, QPainter, QLinearGradient, QRadialGradient,
                         QConicalGradient, QBrush, QPen, QGuiApplication, QIcon,
                         QPixmap, QImage, QAction, QFont, QFontMetrics,
                         QPainterPath, QKeySequence, QSurfaceFormat)
from PyQt6.QtWidgets import (QApplication, QWidget, QSystemTrayIcon, QMenu,
                             QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton,
                             QSlider, QLabel, QComboBox, QSpinBox, QCheckBox,
                             QTabWidget, QColorDialog, QKeySequenceEdit,
                             QMessageBox)

try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    HAVE_GL = True
except Exception:
    QOpenGLWidget = None
    HAVE_GL = False

# ======================================================================
#  Files, settings, error log
# ======================================================================
APP_DIR = os.path.join(os.environ.get("APPDATA", "."), "GlowApp")
os.makedirs(APP_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
LOG_FILE = os.path.join(APP_DIR, "error.log")
_log_count = 0
try:
    if os.path.getsize(LOG_FILE) > 1_000_000:   # keep the log small
        os.remove(LOG_FILE)
except OSError:
    pass


def log_exception(exc_type, exc, tb):
    """Save errors to a file instead of crashing the whole app."""
    global _log_count
    _log_count += 1
    if _log_count > 200:
        return
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass
    print(text, file=sys.stderr)


sys.excepthook = log_exception

STYLES = ["Aurora", "Northern lights", "Ambient glow", "Neon line", "Comets",
          "Corner bloom", "Ripples", "Equalizer", "Spectrum edge",
          "Stage beams", "Embers"]
COLOR_MODES = ["Album art", "Rainbow", "Custom"]
TAKEOVER_MODES = ["Album + lyrics", "Album cover", "Lyrics"]
SHOW_MODES = ["Off", "When idle", "Right away"]
FPS_MODES = ["Auto", "30", "60", "90", "120", "144"]
CHOICES = {"style": STYLES, "color_mode": COLOR_MODES, "takeover_mode": TAKEOVER_MODES,
           "show_mode": SHOW_MODES, "fps": FPS_MODES}

DEFAULTS = {
    "style": "Aurora",
    "color_mode": "Album art",
    "color1": "#8b5cff",
    "color2": "#46e5f5",
    "bright": 80,          # how strong the light is (10-100)
    "size": 50,            # how big the effect is (10-100)
    "react": 60,           # how much it pulses with the beat (0-100)
    "sens": 50,            # how easily it picks up sound (1-100)
    "speed": 50,           # how fast things move (0-100)
    "sync_delay": 0,       # ms to delay the visuals (Bluetooth headphones)
    "lyric_offset": 0,     # tenths of a second
    "show_mode": "When idle",
    "takeover_mode": "Album + lyrics",
    "idle_secs": 30,
    "idle_only_playing": True,
    "show_clock": True,
    "hd_art": True,
    "gpu": True,
    "fps": "Auto",
    "hotkey": "Ctrl+Alt+G",
    "hotkey_np": "Ctrl+Alt+N",
}
settings = dict(DEFAULTS)


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k not in DEFAULTS or type(v) != type(DEFAULTS[k]):
                continue
            if k in CHOICES and v not in CHOICES[k]:
                continue
            if k in ("color1", "color2") and not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
                continue
            settings[k] = v
    except Exception:
        pass


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass


# ======================================================================
#  Windows helpers (idle time, global hotkeys, GPU preference)
# ======================================================================
user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def last_input_tick():
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    user32.GetLastInputInfo(ctypes.byref(lii))
    return lii.dwTime


def idle_seconds():
    return ((kernel32.GetTickCount() - last_input_tick()) & 0xFFFFFFFF) / 1000.0


def parse_hotkey(text):
    """'Ctrl+Alt+G' -> (modifiers, virtual key) or None."""
    mods, vk = 0, None
    for part in text.split("+"):
        p = part.strip().lower()
        if p in ("ctrl", "control"):
            mods |= 2
        elif p == "alt":
            mods |= 1
        elif p == "shift":
            mods |= 4
        elif p in ("meta", "win"):
            mods |= 8
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())
        elif p.startswith("f") and p[1:].isdigit() and 1 <= int(p[1:]) <= 12:
            vk = 0x6F + int(p[1:])
        else:
            return None
    if vk is None or mods == 0:
        return None
    return mods, vk


class Hotkey(QObject):
    pressed = pyqtSignal()
    failed = pyqtSignal()

    def __init__(self, hid):
        super().__init__()
        self.hid = hid
        self.tid = None

    def register(self, text):
        self.stop()
        parsed = parse_hotkey(text)
        if parsed:
            threading.Thread(target=self._run, args=parsed, daemon=True).start()

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)  # WM_QUIT
            self.tid = None

    def _run(self, mods, vk):
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # creates the message queue
        self.tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, self.hid, mods | 0x4000, vk):  # 0x4000 = no repeat
            self.tid = None
            self.failed.emit()
            return
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                self.pressed.emit()
        user32.UnregisterHotKey(None, self.hid)


def prefer_nvidia():
    """Tell Windows to run this Python with the high-performance (NVIDIA) GPU."""
    try:
        import winreg
        exes = {sys.executable}
        if sys.executable.lower().endswith("python.exe"):
            exes.add(sys.executable[:-10] + "pythonw.exe")
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\DirectX\UserGpuPreferences")
        for exe in exes:
            if os.path.exists(exe):
                winreg.SetValueEx(key, exe, 0, winreg.REG_SZ, "GpuPreference=2;")
        winreg.CloseKey(key)
        return True
    except Exception:
        log_exception(*sys.exc_info())
        return False


gpu_info = {"renderer": ""}


def detect_gpu():
    """Read the name of the GPU that OpenGL is using (call while GL is active)."""
    try:
        gl = ctypes.WinDLL("opengl32")
        gl.glGetString.restype = ctypes.c_char_p
        gl.glGetString.argtypes = [ctypes.c_uint]
        name = gl.glGetString(0x1F01)  # GL_RENDERER
        gpu_info["renderer"] = name.decode("utf-8", "replace") if name else "unknown"
    except Exception:
        gpu_info["renderer"] = "unknown"


# ======================================================================
#  Shared state
# ======================================================================
NB = 48                     # spectrum bands
chan = [2]                  # audio channel count
WHITE = QColor(255, 255, 255)

aud = {                     # written by the audio thread, read by the effects
    "level": 0.0, "bass": 0.0, "mid": 0.0, "treb": 0.0,
    "bands": [0.0] * NB, "beat_seq": 0, "beat_str": 0.0,
}
media = {                   # filled by the media thread
    "active": False, "playing": False, "key": "", "title": "", "artist": "",
    "raw_title": "", "raw_artist": "", "album": "", "source": "",
    "duration": 0.0, "pos_base": 0.0, "pos_t0": 0.0,
    "thumb_bytes": None, "thumb_id": 0, "thumb_info": "", "error": "",
}
lyr = {"key": "", "status": "none", "lines": [], "times": []}
art = {"pixmap": None, "hd": None, "c1": None, "c2": None, "c3": None}
hd = {"key": "", "image": None, "pal": None, "id": 0}


# ======================================================================
#  Audio: loudness, bass/mid/treble, spectrum and beat (onset) detection
# ======================================================================
agc = {"peak": 0.02, "bpeak": 2000.0, "mpeak": 1500.0, "tpeak": 800.0,
       "spk": 3000.0, "prev": None, "fm": 0.0, "last_on": 0.0}
_win_cache = {}
_edge_cache = {}


def _window(n):
    w = _win_cache.get(n)
    if w is None:
        w = _win_cache[n] = np.hanning(n).astype(np.float32)
    return w


def _band_edges(nspec):
    e = _edge_cache.get(nspec)
    if e is None:
        hi = max(12, nspec // 2)
        edges = np.geomspace(2, hi, NB + 1).astype(int)
        starts = edges[:-1]
        ends = np.maximum(edges[1:], starts + 1)
        e = _edge_cache[nspec] = (starts, ends, (ends - starts).astype(np.float64))
    return e


def audio_callback(in_data, frame_count, time_info, status):
    try:
        data = np.frombuffer(in_data, dtype=np.int16).astype(np.float32)
        ch = chan[0]
        if ch > 1 and len(data) % ch == 0:
            data = data.reshape(-1, ch).mean(axis=1)
        if len(data) < 128:
            return (None, pyaudio.paContinue)
        gain = 0.5 + settings["sens"] / 40.0              # sensitivity -> 0.5 .. 3.0
        rms = float(np.sqrt(np.mean(data ** 2))) / 32768.0
        agc["peak"] = max(rms, agc["peak"] * 0.9985, 0.01)  # follows your volume
        spec = np.abs(np.fft.rfft(data * _window(len(data))))

        b = float(spec[1:9].mean())                       # bass
        m = float(spec[9:48].mean())                      # mids
        t = float(spec[48:220].mean())                    # treble
        agc["bpeak"] = max(b, agc["bpeak"] * 0.998, 2000.0)
        agc["mpeak"] = max(m, agc["mpeak"] * 0.998, 1500.0)
        agc["tpeak"] = max(t, agc["tpeak"] * 0.998, 800.0)

        # beat = sudden rise of low/mid energy (spectral flux)
        cur = spec[1:40].copy()
        if agc["prev"] is None or len(agc["prev"]) != len(cur):
            agc["prev"] = cur
        flux = float(np.maximum(cur - agc["prev"], 0).sum())
        agc["prev"] = cur
        thr = agc["fm"] * 1.5 + 15000.0
        agc["fm"] = agc["fm"] * 0.95 + flux * 0.05
        now = time.monotonic()
        if flux > thr and now - agc["last_on"] > 0.11:
            agc["last_on"] = now
            aud["beat_str"] = float(min(1.0, 0.45 + 0.55 * (flux - thr) / max(thr, 1.0)))
            aud["beat_seq"] += 1

        starts, ends, widths = _band_edges(len(spec))
        cs = np.concatenate(([0.0], np.cumsum(spec)))
        raw = (cs[ends] - cs[starts]) / widths
        agc["spk"] = max(float(raw.max()), agc["spk"] * 0.998, 3000.0)

        aud["level"] = float(min(1.0, (rms / agc["peak"]) ** 1.5 * gain))
        aud["bass"] = float(min(1.0, (b / agc["bpeak"]) ** 1.5 * gain))
        aud["mid"] = float(min(1.0, (m / agc["mpeak"]) ** 1.5 * gain))
        aud["treb"] = float(min(1.0, (t / agc["tpeak"]) ** 1.5 * gain))
        aud["bands"] = np.minimum(1.0, (raw / agc["spk"]) ** 0.75 * gain * 0.7).tolist()
    except Exception:
        pass
    return (None, pyaudio.paContinue)


class Audio:
    def __init__(self):
        self.p = None
        self.stream = None

    def start(self):
        if self.stream:
            return
        self.p = pyaudio.PyAudio()
        wasapi = self.p.get_host_api_info_by_type(pyaudio.paWASAPI)
        dev = self.p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not dev["isLoopbackDevice"]:
            for lb in self.p.get_loopback_device_info_generator():
                if dev["name"] in lb["name"]:
                    dev = lb
                    break
        chan[0] = dev["maxInputChannels"]
        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=dev["maxInputChannels"],
            rate=int(dev["defaultSampleRate"]),
            frames_per_buffer=1024,
            input=True,
            input_device_index=dev["index"],
            stream_callback=audio_callback,
        )
        self.stream.start_stream()

    def stop(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
        if self.p:
            self.p.terminate()
            self.p = None
        aud.update(level=0.0, bass=0.0, mid=0.0, treb=0.0, bands=[0.0] * NB)


# ======================================================================
#  Web helpers
# ======================================================================
ALLOWED_HOSTS = ("lrclib.net", "itunes.apple.com", "mzstatic.com", "api.deezer.com", "dzcdn.net")
MAX_IMG = 12_000_000
MAX_JSON = 2_000_000


def safe_url(url):
    """Only talk to the few services we use, and only over https."""
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_url(newurl):
            raise urllib.error.URLError("blocked redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect())


def http_get(url, params=None, timeout=10, limit=MAX_IMG):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    if not safe_url(url):
        raise ValueError("blocked url")
    req = urllib.request.Request(url, headers={"User-Agent": "GlowApp/7.0"})
    with _opener.open(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("response too large")
    return data


def http_json(url, params=None, timeout=10):
    return json.loads(http_get(url, params, timeout, MAX_JSON).decode("utf-8"))


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# ======================================================================
#  Now playing (Windows media controls). Works with anything that shows
#  up in the Windows volume/media popup: Spotify, YouTube, SoundCloud,
#  YouTube Music, Apple Music, VLC ... No login needed.
# ======================================================================
BROWSERS = ("chrome", "msedge", "edge", "firefox", "brave", "opera", "vivaldi")
NOISE = re.compile(r"\s*[\(\[][^\)\]]*(official|video|audio|lyric|visuali[sz]er|remaster|"
                   r"\bhd\b|\bhq\b|4k|\bmv\b)[^\)\]]*[\)\]]", re.I)


def clean_names(artist, title, source):
    """Tidy messy browser titles like 'Artist - Song (Official Video)'."""
    artist = re.sub(r"\s*-\s*Topic$", "", artist, flags=re.I)
    artist = re.sub(r"VEVO$", "", artist, flags=re.I).strip()
    title = NOISE.sub("", title).strip()
    if any(b in source.lower() for b in BROWSERS) and " - " in title:
        a, t = title.split(" - ", 1)
        if a.strip() and t.strip():
            return a.strip(), t.strip()
    return artist, title


def set_inactive():
    if media["active"] or media["key"]:
        media.update(active=False, playing=False, key="", title="", artist="",
                     raw_title="", raw_artist="", album="", source="")
        if media["thumb_bytes"] is not None:
            media["thumb_bytes"] = None
            media["thumb_id"] += 1


def media_worker():
    try:
        import asyncio
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager)
        from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions
    except Exception as e:
        media["error"] = f"winrt not installed: {e}"
        return

    async def read_thumb(ref):
        stream = await ref.open_read_async()
        size = int(stream.size)
        buf = Buffer(size)
        await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
        try:
            return bytes(memoryview(buf))
        except Exception:
            reader = DataReader.from_buffer(buf)
            arr = bytearray(buf.length)
            reader.read_bytes(arr)
            return bytes(arr)

    async def loop():
        mgr = await Manager.request_async()
        since = 99   # polls since the song changed
        while True:
            try:
                s = mgr.get_current_session()
                if s is None:
                    set_inactive()
                else:
                    props = await s.try_get_media_properties_async()
                    pb = s.get_playback_info()
                    tl = s.get_timeline_properties()
                    playing = int(pb.playback_status) == 4
                    raw_artist = props.artist or ""
                    raw_title = props.title or ""
                    album = props.album_title or ""
                    try:
                        src = s.source_app_user_model_id or ""
                    except Exception:
                        src = ""
                    artist, title = clean_names(raw_artist, raw_title, src)
                    key = f"{raw_artist}|{raw_title}|{album}"
                    dur = max(0.0, tl.end_time.total_seconds() - tl.start_time.total_seconds())
                    pos = tl.position.total_seconds()
                    try:
                        el = (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds()
                        if playing and 0 <= el < 600:
                            pos += el
                    except Exception:
                        pass
                    if dur > 0:
                        pos = min(pos, dur)

                    new_key = key != media["key"]
                    since = 0 if new_key else since + 1
                    thumb, info = None, media["thumb_info"] or "this app sent no cover"
                    if props.thumbnail is None:
                        info = "this app sent no cover"
                    elif since <= 5 or since % 15 == 0:   # re-check a few times after a song change
                        try:
                            thumb = await read_thumb(props.thumbnail)
                            info = "ok"
                        except Exception as e:
                            thumb = None
                            info = f"read error {type(e).__name__}: {e}"[:140]
                    media["thumb_info"] = info

                    if new_key:
                        media.update(key=key, title=title, artist=artist, raw_title=raw_title,
                                     raw_artist=raw_artist, album=album, source=src)
                    if thumb is not None and thumb != media["thumb_bytes"]:
                        media["thumb_bytes"] = thumb
                        media["thumb_id"] += 1
                    elif thumb is None and new_key and media["thumb_bytes"] is not None:
                        media["thumb_bytes"] = None
                        media["thumb_id"] += 1
                    media.update(active=True, playing=playing, duration=dur,
                                 pos_base=pos, pos_t0=time.monotonic())
            except Exception as e:
                media["thumb_info"] = f"media error {type(e).__name__}: {e}"[:140]
            await asyncio.sleep(1.0)

    try:
        asyncio.run(loop())
    except Exception as e:
        media["error"] = str(e)


def current_position():
    p = media["pos_base"]
    if media["playing"]:
        p += time.monotonic() - media["pos_t0"]
    return p


# ======================================================================
#  HD album art (looks up the real high-resolution cover online)
# ======================================================================
def pick_itunes(results, artist, title, album):
    na, nt, nal = norm(artist), norm(title), norm(album)
    best, best_score = None, 0
    for r in results:
        rt, ra, rc = norm(r.get("trackName")), norm(r.get("artistName")), norm(r.get("collectionName"))
        score = 0
        if nt and rt == nt:
            score += 3
        elif nt and rt and (nt in rt or rt in nt):
            score += 2
        if na and ra and (na in ra or ra in na):
            score += 2
        if nal and rc and (nal == rc or nal in rc or rc in nal):
            score += 1
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= 4 else None


def start_hd_fetch(key, artist, title, album, raw_artist, raw_title):
    hd.update(key=key, image=None, pal=None)

    def work():
        data = None
        try:
            for a, t in ((artist, title), (raw_artist, raw_title)):
                if data or not t:
                    continue
                res = http_json("https://itunes.apple.com/search",
                                {"term": f"{a} {t}".strip(), "entity": "song", "limit": 10}).get("results", [])
                hit = pick_itunes(res, a, t, album)
                if hit and hit.get("artworkUrl100"):
                    for size in (3000, 1500, 1000):
                        try:
                            data = http_get(hit["artworkUrl100"].replace("100x100bb", f"{size}x{size}bb"))
                            if len(data) > 5000:
                                break
                            data = None
                        except Exception:
                            data = None
            if data is None and artist and title:  # fallback: Deezer
                res = http_json("https://api.deezer.com/search",
                                {"q": f'artist:"{artist}" track:"{title}"', "limit": 5}).get("data", [])
                for r in res:
                    cover = (r.get("album") or {}).get("cover_xl")
                    if cover and norm(title) in norm(r.get("title")):
                        data = http_get(cover)
                        break
        except Exception:
            data = None
        img, pal = None, None
        if data and len(data) <= MAX_IMG:   # decode here, not in the window thread (no stutter)
            try:
                im = QImage.fromData(data)
                if not im.isNull():
                    if max(im.width(), im.height()) > 2048:
                        im = im.scaled(2048, 2048, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                    img = im
                    pal = extract_palette(im)
            except Exception:
                img, pal = None, None
        if hd["key"] == key:
            hd["image"] = img
            hd["pal"] = pal
            hd["id"] += 1

    threading.Thread(target=work, daemon=True).start()


# ======================================================================
#  Lyrics (free synced lyrics from lrclib.net)
# ======================================================================
TS = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def parse_lrc(text):
    out = []
    for line in text.splitlines():
        stamps = TS.findall(line)
        if not stamps:
            continue
        words = TS.sub("", line).strip()
        for m, s in stamps:
            out.append((int(m) * 60 + float(s.replace(":", ".")), words))
    out.sort(key=lambda x: x[0])
    return out


def lyric_candidates(artist, title, raw_artist, raw_title):
    out = []

    def add(a, t):
        a, t = a.strip(), t.strip()
        if t and (a, t) not in out:
            out.append((a, t))

    add(artist, title)
    add(raw_artist, raw_title)
    first = artist.split(",")[0].split(";")[0].split(" & ")[0]
    add(first, title)
    add(first, re.sub(r"\s+-\s.*$|\s*[(\[].*$", "", title))
    return out[:4]


def find_lyrics(cands, album, duration):
    for a, t in cands[:2]:
        params = {"artist_name": a, "track_name": t}
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = int(duration)
        try:
            d = http_json("https://lrclib.net/api/get", params, 8)
            if d.get("syncedLyrics"):
                return d
        except Exception:
            pass
    for a, t in cands:
        try:
            params = {"track_name": t, "artist_name": a} if a else {"q": t}
            res = http_json("https://lrclib.net/api/search", params, 8)
            best = None
            for r in res:
                if r.get("syncedLyrics"):
                    if best is None or abs((r.get("duration") or 0) - duration) < abs((best.get("duration") or 0) - duration):
                        best = r
            if best:
                return best
        except Exception:
            pass
    return None


def start_lyrics_fetch(key, cands, album, duration):
    lyr.update(key=key, status="loading", lines=[], times=[])

    def work():
        status, lines = "missing", []
        try:
            d = find_lyrics(cands, album, duration)
            if d:
                lines = parse_lrc(d["syncedLyrics"])
                if lines:
                    status = "synced"
        except Exception:
            pass
        if lyr["key"] == key:
            lyr["lines"] = lines
            lyr["times"] = [t for t, _ in lines]
            lyr["status"] = status

    threading.Thread(target=work, daemon=True).start()


def lyric_status_text():
    return {"loading": "Looking for lyrics\u2026",
            "missing": "No synced lyrics found"}.get(lyr["status"], "")


# ======================================================================
#  Accurate colors from album art (k-means on the real pixels)
# ======================================================================
def lift(c):
    """Keep the real hue/saturation, only brighten very dark colors."""
    h, s, v, _ = c.getHsv()
    return QColor.fromHsv(max(h, 0), min(255, int(s * 1.08)), max(v, 175))


def mix(a, b, t):
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def extract_palette(img):
    small = img.scaled(40, 40, Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation
                       ).convertToFormat(QImage.Format.Format_RGB32)
    pts = []
    for y in range(40):
        for x in range(40):
            c = QColor(small.pixel(x, y))
            pts.append((c.red(), c.green(), c.blue()))
    pts = np.array(pts, dtype=np.float32)
    k = 6
    rng = np.random.default_rng(7)
    centers = pts[rng.choice(len(pts), k, replace=False)].copy()
    lab = np.zeros(len(pts), dtype=int)
    for _ in range(10):
        d = ((pts[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        lab = d.argmin(axis=1)
        for c in range(k):
            m = lab == c
            if m.any():
                centers[c] = pts[m].mean(axis=0)
    counts = np.bincount(lab, minlength=k)

    cands = []
    for c, n in zip(centers, counts):
        if n == 0:
            continue
        col = QColor(int(c[0]), int(c[1]), int(c[2]))
        h, s, v, _ = col.getHsv()
        sat, val = s / 255.0, v / 255.0
        score = (n / len(pts)) * (0.15 + sat) ** 1.4 * (0.2 + val)
        cands.append((score, col, max(h, 0), sat))
    cands.sort(key=lambda x: -x[0])

    if max(c[3] for c in cands) < 0.12:  # black-and-white cover
        shades = sorted((c[1] for c in cands), key=lambda c: -c.value())
        light = lift(shades[0])
        return light, mix(light, QColor(120, 120, 140), 0.5), mix(light, WHITE, 0.4)

    chosen = [cands[0]]
    for cand in cands[1:]:
        if len(chosen) >= 3:
            break
        if cand[3] > 0.1 and all(min(abs(cand[2] - c[2]), 360 - abs(cand[2] - c[2])) >= 28 for c in chosen):
            chosen.append(cand)
    pal = [lift(c[1]) for c in chosen]
    base = pal[0]
    while len(pal) < 3:  # not enough different colors: shift the hue a little
        shift = 35 if len(pal) == 1 else -35
        pal.append(QColor.fromHsv((base.hue() + shift) % 360, base.saturation(), base.value()))
    return pal[0], pal[1], pal[2]


def target_colors(hue):
    mode = settings["color_mode"]
    if mode == "Rainbow":
        return [QColor.fromHsv(int(hue + off) % 360, 230, 255) for off in (0, 60, 120)]
    if mode == "Album art" and art["c1"] is not None:
        return [art["c1"], art["c2"], art["c3"]]
    a, b = QColor(settings["color1"]), QColor(settings["color2"])
    return [a, b, mix(a, b, 0.5)]


# ======================================================================
#  Text helpers
# ======================================================================
WRAP = Qt.TextFlag.TextWordWrap.value
A_CENTER = Qt.AlignmentFlag.AlignCenter.value | WRAP
A_LEFT = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter).value | WRAP
A_TOPC = (Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop).value | WRAP
A_TOPL = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop).value | WRAP
A_TOPR = (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop).value | WRAP


def ui_font(px, bold=False, spacing=0.0):
    f = QFont()
    f.setFamilies(["Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI"])
    f.setPixelSize(max(8, int(px)))
    f.setWeight(QFont.Weight.Bold if bold else QFont.Weight.DemiBold)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def draw_text(p, rect, flags, text, px, bold=False, color=None, one_line=False, spacing=0.0):
    f = ui_font(px, bold, spacing)
    p.setFont(f)
    p.setPen(color or WHITE)
    if one_line:
        flags &= ~WRAP
        text = QFontMetrics(f).elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width()))
    p.drawText(rect, flags, text)


def fmt_time(t):
    t = max(0, int(t))
    return f"{t // 60}:{t % 60:02d}"


def draw_progress(p, rect, px):
    dur, pos = media["duration"], current_position()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 50))
    p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
    if dur > 0:
        frac = max(0.0, min(1.0, pos / dur))
        fw = max(rect.height(), rect.width() * frac)
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawRoundedRect(QRectF(rect.x(), rect.y(), fw, rect.height()), rect.height() / 2, rect.height() / 2)
        p.drawEllipse(QPointF(rect.x() + fw, rect.center().y()), rect.height() * 1.4, rect.height() * 1.4)
        dim = QColor(255, 255, 255, 140)
        draw_text(p, QRectF(rect.x(), rect.bottom() + 12, rect.width() / 2, px * 1.8),
                  A_TOPL, fmt_time(pos), px, False, dim, True)
        draw_text(p, QRectF(rect.center().x(), rect.bottom() + 12, rect.width() / 2, px * 1.8),
                  A_TOPR, "-" + fmt_time(dur - pos), px, False, dim, True)


def rounded_pixmap(pix, size, radius, dpr=1.0):
    """Square, rounded-corner copy of a pixmap, rendered at real device pixels (sharp)."""
    px = max(2, int(size * dpr))
    src = pix.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                     Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(px, px)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, px, px), radius * dpr, radius * dpr)
        p.setClipPath(path)
        p.drawPixmap((px - src.width()) // 2, (px - src.height()) // 2, src)
    finally:
        p.end()
    out.setDevicePixelRatio(dpr)
    return out


# ======================================================================
#  Lyrics view: big, smooth-scrolling lines with blur (Apple Music style)
# ======================================================================
class LyricView:
    def __init__(self):
        self.view = 0.0
        self.key = ""
        self.hcache = {}

    def paint(self, p, rect, left, px, accent):
        if not media["active"] or lyr["status"] != "synced" or not lyr["lines"]:
            return False
        times, lines = lyr["times"], lyr["lines"]
        pos = current_position() + settings["lyric_offset"] / 10.0
        i = bisect.bisect_right(times, pos) - 1
        target = max(i, 0)
        if self.key != lyr["key"] or abs(self.view - target) > 4:
            self.key = lyr["key"]
            self.view = float(target)
            self.hcache = {}
        self.view += (target - self.view) * 0.14

        align = A_LEFT if left else A_CENTER
        width, height = rect.width(), rect.height()
        font = ui_font(px)
        layers = []
        p.save()
        try:
            p.setClipRect(rect)
            p.setFont(font)
            f = int(self.view)
            frac = self.view - f
            lo = max(0, f - 5)
            hi = min(len(lines) - 1, f + 6)
            hts = {}
            for j in range(lo, hi + 1):
                text = lines[j][1] or "\u266a"
                k = (text, int(width), int(px))
                if k not in self.hcache:
                    br = p.boundingRect(QRectF(0, 0, width, 10000), align, text)
                    self.hcache[k] = br.height() + px * 0.7
                hts[j] = self.hcache[k]
            cen, prev = {}, None
            for j in range(lo, hi + 1):
                cen[j] = hts[j] / 2 if prev is None else cen[prev] + hts[prev] / 2 + hts[j] / 2
                prev = j
            f2 = min(f + 1, hi)
            off = cen[f] * (1 - frac) + cen[f2] * frac
            cy = rect.center().y()

            # two small layers; drawing text small and scaling it up = soft blur
            for sc in (0.5, 0.25):
                img = QImage(max(2, int(width * sc)), max(2, int(height * sc)),
                             QImage.Format.Format_ARGB32_Premultiplied)
                img.fill(Qt.GlobalColor.transparent)
                qp = QPainter(img)
                qp.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
                qp.scale(img.width() / width, img.height() / height)
                qp.setFont(font)
                layers.append((img, qp))

            for j in range(lo, hi + 1):
                d = abs(j - self.view)
                a = max(0.14, 1 - 0.40 * d)
                if i < 0:
                    a *= 0.55
                wc = max(0.0, 1 - d / 0.7)
                wh = max(0.0, 1 - abs(d - 1.0) / 0.8)
                wq = max(0.0, min(1.0, d - 0.9))
                tot = wc + wh + wq
                if tot <= 0:
                    continue
                wc, wh, wq = wc / tot, wh / tot, wq / tot
                text = lines[j][1] or "\u266a"
                yc = cy + cen[j] - off
                r = QRectF(rect.x(), yc - hts[j] / 2, width, hts[j])
                if wc > 0.01:
                    if d < 0.6:  # soft colored glow behind the current line
                        ga = int(70 * (1 - d / 0.6))
                        p.setPen(QColor(accent.red(), accent.green(), accent.blue(), ga))
                        for ox, oy in ((-4, 0), (4, 0), (0, -4), (0, 4), (-3, -3), (3, 3), (-3, 3), (3, -3)):
                            p.drawText(r.translated(ox, oy), align, text)
                    p.setPen(QColor(255, 255, 255, int(255 * min(1.0, a * wc))))
                    p.drawText(r, align, text)
                local = r.translated(-rect.x(), -rect.y())
                for (img, qp), wgt in zip(layers, (wh, wq)):
                    if wgt > 0.01:
                        qp.setPen(QColor(255, 255, 255, int(255 * min(1.0, a * 1.15 * wgt))))
                        qp.drawText(local, align, text)
        finally:
            for _, qp in layers:
                if qp.isActive():
                    qp.end()
        try:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            for img, _ in layers:
                p.drawImage(rect, img)
        finally:
            p.restore()
        return True


lyric_view = LyricView()


# ======================================================================
#  Effects engine. Everything is drawn small and scaled up smoothly,
#  which gives soft, blurry, premium-looking light (no banding).
# ======================================================================
SCALE = 4
FALL = [(0.0, 1.0), (0.06, 0.82), (0.16, 0.52), (0.32, 0.24), (0.55, 0.08), (1.0, 0.0)]
BLOB = [(0.0, 1.0), (0.3, 0.55), (0.6, 0.18), (1.0, 0.0)]
PLUS = QPainter.CompositionMode.CompositionMode_Plus
OVER = QPainter.CompositionMode.CompositionMode_SourceOver


def perim_point(s, w, h, m=0.0):
    ww, hh = w - 2 * m, h - 2 * m
    s %= 2 * (ww + hh)
    if s < ww:
        return QPointF(m + s, m)
    s -= ww
    if s < hh:
        return QPointF(m + ww, m + s)
    s -= hh
    if s < ww:
        return QPointF(m + ww - s, m + hh)
    s -= ww
    return QPointF(m, m + hh - s)


def pw(x, e):
    """Safe power (never gives complex numbers)."""
    return max(0.0, x) ** e


class Fx:
    def __init__(self):
        self.cv = None
        self.smooth = 0.0        # music energy 0..1 (fast up, slow down)
        self.kick = 0.0          # beat impulse, jumps to ~1 on a beat then fades
        self.mid = 0.0
        self.treb = 0.0
        self.k = 1.0             # geometry multiplier (Size + Reaction)
        self.br = 0.8            # brightness multiplier (Brightness + Reaction)
        self.ts = 1.0            # speed multiplier
        self.angle = 0.0
        self.drift = 0.0
        self.hue = 0.0
        self.t = 0.0
        self.beat = False
        self.beat_count = 0
        self.seen_seq = 0
        self.flash = [0.0] * 4
        self.spawn_acc = 0.0
        self.hist = collections.deque()
        self.ripples = []
        self.parts = []
        self.heads = []
        self.rects = []
        self.bars = [0.0] * NB
        self.pal = [QColor("#8b5cff"), QColor("#46e5f5"), QColor("#ff55c8")]
        self.paint_ms = 0.0
        self.mask_cache = collections.OrderedDict()
        self.scratch = None
        self.style_fns = {"Aurora": self.aurora, "Northern lights": self.northern,
                          "Ambient glow": self.ambient_style, "Neon line": self.neon,
                          "Comets": self.comets, "Corner bloom": self.corners,
                          "Ripples": self.ripple_style, "Equalizer": self.equalizer,
                          "Spectrum edge": self.spectrum, "Stage beams": self.beams,
                          "Embers": self.embers}

    # ---- animation state ----
    def step(self, dt, w, h):
        now = time.monotonic()
        self.hist.append((now, aud["level"], aud["bass"], aud["mid"], aud["treb"],
                          aud["bands"], aud["beat_seq"], aud["beat_str"]))
        target = now - settings["sync_delay"] / 1000.0
        while len(self.hist) > 1 and self.hist[1][0] <= target:
            self.hist.popleft()
        _, lv, bs, md, tb, bnds, seq, strg = self.hist[0]

        self.beat = False
        if seq != self.seen_seq:
            self.seen_seq = seq
            self.beat = True
            self.kick = max(self.kick, strg)
            self.beat_count += 1
            self.ripples.append([0.0, strg])
            self.flash[self.beat_count % 4] = 1.0
        self.kick *= math.exp(-dt * 6.5)
        self.flash = [f * math.exp(-dt * 4.0) for f in self.flash]

        drive = min(1.0, 0.30 * lv + 0.30 * bs + 0.60 * self.kick)
        up, dn = 1 - math.exp(-dt * 45), 1 - math.exp(-dt * 7)
        self.smooth += (drive - self.smooth) * (up if drive > self.smooth else dn)
        sm = 1 - math.exp(-dt * 14)
        self.mid += (md - self.mid) * sm
        self.treb += (tb - self.treb) * sm
        decay = math.exp(-dt * 10.4)
        for i in range(NB):
            self.bars[i] = max(bnds[i], self.bars[i] * decay)

        s = self.smooth
        react = settings["react"] / 100.0
        self.ts = settings["speed"] / 50.0
        self.k = (settings["size"] / 50.0) * (1 + 1.8 * react * s)
        self.br = (settings["bright"] / 100.0) * (1 - 0.55 * react * (1 - s))
        ts = self.ts
        self.t += dt * ts
        self.angle = (self.angle + dt * 60 * ts * (1 + react * s)) % 360
        self.hue = (self.hue + dt * 22 * ts) % 360
        self.drift = (self.drift + dt * 360 * ts * (1 + react * (1.2 * s + 0.8 * self.mid))) % (2 * (w + h))
        self.ripples = [[a + dt * 0.55, st] for a, st in self.ripples if a + dt * 0.55 < 1.0]

        tgt = target_colors(self.hue)
        self.pal = [mix(self.pal[i], tgt[i], 0.1) for i in range(3)]
        if settings["style"] == "Embers":
            self.step_particles(dt, w, h)

    def step_particles(self, dt, w, h):
        s = self.smooth
        react = settings["react"] / 100.0
        self.spawn_acc += (25 + 230 * s * (0.4 + react) + 160 * self.treb) * (0.5 + 0.5 * self.ts) * dt
        n = int(self.spawn_acc)
        self.spawn_acc -= n
        if self.beat:
            n += 14
        for _ in range(n):
            if len(self.parts) >= 260:
                break
            pt = perim_point(random.random() * 2 * (w + h), w, h)
            if pt.y() <= 0.5:
                nx, ny = 0.0, 1.0
            elif pt.x() >= w - 0.5:
                nx, ny = -1.0, 0.0
            elif pt.y() >= h - 0.5:
                nx, ny = 0.0, -1.0
            else:
                nx, ny = 1.0, 0.0
            sp = random.uniform(40, 170) * (0.5 + 0.8 * self.ts) * (0.7 + 0.8 * s)
            tang = random.uniform(-40, 40)
            self.parts.append([pt.x(), pt.y(), nx * sp - ny * tang, ny * sp + nx * tang,
                               0.0, random.uniform(1.3, 2.8), random.uniform(0.6, 1.4),
                               random.randrange(3)])
        alive = []
        drag = max(0.0, 1 - 0.6 * dt)
        for pc in self.parts:
            pc[4] += dt
            if pc[4] < pc[5]:
                pc[0] += pc[2] * dt
                pc[1] += pc[3] * dt
                pc[2] *= drag
                pc[3] *= drag
                alive.append(pc)
        self.parts = alive

    # ---- small helpers ----
    def A(self, x):
        """Alpha 0..255 after the Brightness setting."""
        return int(max(0.0, min(255.0, x * self.br)))

    def canvas(self, w, h):
        sc = max(SCALE, -(-int(w) // 640))   # keep the small canvas <= ~640 px wide (fast at 4K)
        cw, ch = max(32, int(w) // sc), max(32, int(h) // sc)
        if self.cv is None or self.cv.width() != cw or self.cv.height() != ch:
            self.cv = QImage(cw, ch, QImage.Format.Format_ARGB32_Premultiplied)
        self.cv.fill(Qt.GlobalColor.transparent)
        q = QPainter(self.cv)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        q.scale(cw / w, ch / h)
        return q

    def new_layer(self):
        img = QImage(self.cv.size(), QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        return img

    def conical(self, w, h, a=255):
        g = QConicalGradient(QPointF(w / 2, h / 2), self.angle)
        cols = []
        for c in self.pal + [self.pal[0]]:
            cc = QColor(c)
            cc.setAlpha(max(0, min(255, int(a))))
            cols.append(cc)
        for i, cc in enumerate(cols):
            g.setColorAt(i / 3.0, cc)
        return g

    def frame_mask(self, w, h, thick):
        """White mask: strong at the screen edges, fading smoothly inward (cached)."""
        bucket = int(max(8.0, min(thick, min(w, h) * 0.6)) // 6)
        key = (self.cv.width(), self.cv.height(), bucket)
        m = self.mask_cache.get(key)
        if m is not None:
            self.mask_cache.move_to_end(key)
            return m
        t = bucket * 6 + 3.0
        m = self.new_layer()
        q = QPainter(m)
        try:
            q.scale(m.width() / w, m.height() / h)
            q.setCompositionMode(PLUS)
            for rect, p1, p2 in (
                (QRectF(0, 0, w, t), QPointF(0, 0), QPointF(0, t)),
                (QRectF(0, h - t, w, t), QPointF(0, h), QPointF(0, h - t)),
                (QRectF(0, 0, t, h), QPointF(0, 0), QPointF(t, 0)),
                (QRectF(w - t, 0, t, h), QPointF(w, 0), QPointF(w - t, 0)),
            ):
                g = QLinearGradient(p1, p2)
                for pos, a in FALL:
                    g.setColorAt(pos, QColor(255, 255, 255, int(255 * a)))
                q.fillRect(rect, QBrush(g))
        finally:
            q.end()
        self.mask_cache[key] = m
        while len(self.mask_cache) > 16:
            self.mask_cache.popitem(last=False)
        return m

    def tint(self, mask, w, h):
        """Fill the rotating color gradient, keep it only where the mask is."""
        out = self.scratch
        if out is None or out.size() != self.cv.size():
            out = self.scratch = self.new_layer()
        else:
            out.fill(Qt.GlobalColor.transparent)
        o = QPainter(out)
        try:
            o.scale(out.width() / w, out.height() / h)
            o.fillRect(QRectF(0, 0, w, h), QBrush(self.conical(w, h)))
            o.resetTransform()
            o.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            o.drawImage(0, 0, mask)
        finally:
            o.end()
        return out

    def blit(self, q, img, alpha=1.0):
        q.save()
        try:
            q.resetTransform()
            q.setCompositionMode(OVER)
            q.setOpacity(max(0.0, min(1.0, alpha)))
            q.drawImage(0, 0, img)
        finally:
            q.restore()

    def ambient(self, q, w, h, thick, alpha):
        self.blit(q, self.tint(self.frame_mask(w, h, thick), w, h), alpha * self.br)

    def blob(self, q, center, r, color, a):
        """Soft round light. a = alpha 0..255 (before Brightness)."""
        a = max(0.0, min(255.0, a * self.br))
        if a < 2 or r < 1:
            return
        g = QRadialGradient(center, r)
        c = QColor(color)
        for stop, f in BLOB:
            c.setAlpha(int(a * f))
            g.setColorAt(stop, c)
        q.setPen(Qt.PenStyle.NoPen)
        q.setBrush(QBrush(g))
        q.drawEllipse(center, r, r)

    # ---- styles ----
    def aurora(self, q, w, h):
        s, k = self.smooth, self.k
        self.ambient(q, w, h, 80 * k, 0.40 + 0.3 * s)
        q.setCompositionMode(PLUS)
        perim = 2 * (w + h)
        n = 8
        for i in range(n):
            spd = 1.0 + 0.3 * math.sin(i * 1.7)
            pos = i * perim / n + self.drift * spd * (1 if i % 2 == 0 else -0.7)
            col = mix(self.pal[i % 3], self.pal[(i + 1) % 3], 0.5 + 0.5 * math.sin(self.t * 0.6 + i))
            r = 150 * k * (0.75 + 0.3 * math.sin(self.t * 0.9 + i * 2.1)) * (1 + 0.4 * self.kick)
            self.blob(q, perim_point(pos, w, h), r, col, 150 + 100 * s + 50 * self.treb)

    def northern(self, q, w, h):
        s, k = self.smooth, self.k
        self.ambient(q, w, h, 50 * k, 0.25)
        q.setCompositionMode(PLUS)
        q.setPen(Qt.PenStyle.NoPen)
        n = 90
        step = w / n
        lift_ = 1 + 0.5 * self.mid
        for edge in (0, 1):
            ph = self.t * (1.0 if edge == 0 else -0.8)
            for i in range(n):
                x = i * step
                hgt = (110 + 70 * math.sin(x * 0.004 + ph * 1.3) + 50 * math.sin(x * 0.011 - ph * 0.9)
                       + 30 * math.sin(x * 0.027 + ph * 2.1)) * k * (0.55 if edge else 1.0) * lift_
                hgt = max(24.0, hgt)
                col = mix(self.pal[0], self.pal[1], 0.5 + 0.5 * math.sin(x * 0.003 + self.t * 0.7 + edge))
                y0, y1 = (0.0, hgt) if edge == 0 else (float(h), h - hgt)
                g = QLinearGradient(x, y0, x, y1)
                c = QColor(col)
                for stop, f in ((0.0, 0.9), (0.35, 0.45), (1.0, 0.0)):
                    c.setAlpha(self.A(235 * f * (0.7 + 0.5 * s + 0.3 * self.treb)))
                    g.setColorAt(stop, c)
                q.fillRect(QRectF(x, min(y0, y1), step + 1.5, hgt), QBrush(g))

    def ambient_style(self, q, w, h):
        s, k = self.smooth, self.k
        self.ambient(q, w, h, 110 * k * (1 + 0.3 * self.kick), 0.55 + 0.45 * s)
        self.ambient(q, w, h, 220 * k, 0.18)

    def neon(self, q, w, h):
        s, k = self.smooth, self.k
        bloom = 60 * k * (1 + 0.4 * self.kick)
        mask = self.new_layer()
        m = QPainter(mask)
        try:
            m.setRenderHint(QPainter.RenderHint.Antialiasing)
            m.scale(mask.width() / w, mask.height() / h)
            m.setCompositionMode(PLUS)
            m.setBrush(Qt.BrushStyle.NoBrush)
            for wd, a in ((bloom, 0.14), (bloom * 0.5, 0.22), (bloom * 0.2, 0.42)):
                a = min(1.0, a * (0.7 + 0.8 * s) * self.br * 1.4)
                pen = QPen(QColor(255, 255, 255, int(255 * a)), max(3.0, wd))
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                m.setPen(pen)
                m.drawRoundedRect(QRectF(2, 2, w - 4, h - 4), 28, 28)
        finally:
            m.end()
        self.blit(q, self.tint(mask, w, h))

    def comets(self, q, w, h):
        k = self.k
        self.heads = []
        self.ambient(q, w, h, 60 * k, 0.28)
        q.setCompositionMode(PLUS)
        perim = 2 * (w + h)
        N = 26
        L = 300 * k * (1 + 0.3 * self.kick)
        R0 = 20 * pw(k, 0.7)
        for c in range(4):
            col = self.pal[c % 3]
            d = 1 if c % 2 == 0 else -1
            head = d * self.drift * 1.5 + c * perim / 4
            for j in range(N):
                f = j / N
                pt = perim_point(head - d * f * L, w, h, 3)
                if j == 0:
                    self.heads.append(pt)
                cc = mix(col, WHITE, 0.55 * pw(1 - f, 2))
                self.blob(q, pt, R0 * (1 - 0.75 * f), cc, 240 * pw(1 - f, 1.6))

    def corners(self, q, w, h):
        s, k = self.smooth, self.k
        self.ambient(q, w, h, 60 * k, 0.25)
        q.setCompositionMode(PLUS)
        R = 240 * k * (1 + 0.06 * math.sin(self.t * 2.0))
        for n, (cx, cy, pi) in enumerate(((0, 0, 0), (w, 0, 1), (w, h, 2), (0, h, 1))):
            fl = self.flash[n]
            self.blob(q, QPointF(cx, cy), R * (1 + 0.15 * fl), self.pal[pi], 150 + 80 * s + 110 * fl)

    def ripple_style(self, q, w, h):
        s, k = self.smooth, self.k
        self.ambient(q, w, h, 90 * k, 0.45 + 0.3 * s)
        q.setCompositionMode(PLUS)
        q.setBrush(Qt.BrushStyle.NoBrush)
        maxin = min(w, h) * 0.24
        for age, st in self.ripples:
            age = max(0.0, min(1.0, age))
            off = 6 + age * maxin
            wd = (30 + age * 50) * (0.6 + 0.5 * k)
            a = self.A(230 * st * pw(1 - age, 1.6))
            pen = QPen(QBrush(self.conical(w, h, a)), wd)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            q.setPen(pen)
            q.drawRoundedRect(QRectF(off, off, w - 2 * off, h - 2 * off), 30, 30)

    def equalizer(self, q, w, h):
        k = self.k
        self.ambient(q, w, h, 50 * k, 0.2)
        q.setCompositionMode(PLUS)
        bw = w / NB
        maxh = min(h * 0.6, 280 * k)
        for i in range(NB):
            v = self.bars[i]
            bh = 8 + v * maxh
            col = mix(self.pal[0], self.pal[1], i / (NB - 1))
            col.setAlpha(self.A(120 + 100 * v))
            q.fillRect(QRectF(i * bw + bw * 0.1, h - bh - 8, bw * 0.8, bh + 8), col)
            q.fillRect(QRectF(i * bw + bw * 0.1, 0, bw * 0.8, bh * 0.5 + 8), col)

    def spectrum(self, q, w, h):
        k = self.k
        self.ambient(q, w, h, 40 * k, 0.2)
        q.setCompositionMode(PLUS)
        rects = []
        maxlen = min(min(w, h) * 0.3, 200 * k)
        nh, nv, gap = 64, 28, 0.22
        bw = w / nh
        for i in range(nh):
            idx = min(NB - 1, int(abs(i - (nh - 1) / 2) / ((nh - 1) / 2) * (NB * 0.85)))
            ln = 6 + self.bars[idx] * maxlen
            x = i * bw + bw * gap / 2
            col = mix(self.pal[0], self.pal[1], i / (nh - 1))
            rects.append((QRectF(x, 0, bw * (1 - gap), ln), col))
            rects.append((QRectF(x, h - ln, bw * (1 - gap), ln), col))
        bh = h / nv
        for j in range(nv):
            idx = min(NB - 1, int(j / (nv - 1) * NB * 0.85))
            ln = 6 + self.bars[idx] * maxlen
            y = h - (j + 1) * bh + bh * gap / 2
            col = mix(self.pal[1], self.pal[2], j / (nv - 1))
            rects.append((QRectF(0, y, ln, bh * (1 - gap)), col))
            rects.append((QRectF(w - ln, y, ln, bh * (1 - gap)), col))
        self.rects = rects
        for r, col in rects:
            c = QColor(col)
            c.setAlpha(self.A(150))
            q.fillRect(r, c)

    def beams(self, q, w, h):
        s, k = self.smooth, self.k
        self.ambient(q, w, h, 50 * k, 0.2)
        q.setCompositionMode(PLUS)
        q.setPen(Qt.PenStyle.NoPen)
        L = math.hypot(w, h) * min(1.0, 0.45 + 0.35 * k)
        for c, (cx, cy, base) in enumerate(((0, 0, 45), (w, 0, 135), (w, h, 225), (0, h, 315))):
            swing = 30 * math.sin(self.t * 0.9 + c * 1.7)
            spread = 6 + 5 * k + 6 * s + 8 * self.kick
            a0 = math.radians(base + swing - spread)
            a1 = math.radians(base + swing + spread)
            origin = QPointF(cx, cy)
            path = QPainterPath()
            path.moveTo(origin)
            path.lineTo(QPointF(cx + L * math.cos(a0), cy + L * math.sin(a0)))
            path.lineTo(QPointF(cx + L * math.cos(a1), cy + L * math.sin(a1)))
            path.closeSubpath()
            g = QRadialGradient(origin, L)
            cc = QColor(self.pal[c % 3])
            for stop, f in ((0.0, 1.0), (0.35, 0.45), (1.0, 0.0)):
                cc.setAlpha(self.A(210 * f * (0.45 + 0.8 * s)))
                g.setColorAt(stop, cc)
            q.fillPath(path, QBrush(g))

    def embers(self, q, w, h):
        s, k = self.smooth, self.k
        self.ambient(q, w, h, 60 * k, 0.28 + 0.2 * s)
        q.setCompositionMode(PLUS)
        for x, y, _vx, _vy, age, life, size, ci in self.parts:
            f = min(1.0, age / life)
            r = (7 + 14 * size) * (0.6 + 0.6 * k) * (1 - 0.5 * f)
            col = mix(self.pal[ci], WHITE, 0.35 * (1 - f))
            self.blob(q, QPointF(x, y), r, col, 230 * pw(1 - f, 1.2))

    # ---- sharp details drawn at full size on top ----
    def crisp(self, p, w, h):
        style = settings["style"]
        s, k = self.smooth, self.k
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if style == "Neon line":
            pen = QPen(QBrush(self.conical(w, h, self.A(255))), 2.5 + 2.0 * s + 3.0 * self.kick)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(2, 2, w - 4, h - 4), 28, 28)
        elif style == "Comets":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, self.A(240)))
            for pt in self.heads:
                p.drawEllipse(pt, 3.5 + s * 3, 3.5 + s * 3)
        elif style == "Equalizer":
            bw = w / NB
            maxh = min(h * 0.6, 280 * k)
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(NB):
                bh = 6 + self.bars[i] * maxh
                col = mix(self.pal[0], self.pal[1], i / (NB - 1))
                x, ww = i * bw + bw * 0.14, bw * 0.72
                g = QLinearGradient(0, h - bh, 0, h)
                g.setColorAt(0, QColor(col.red(), col.green(), col.blue(), self.A(255)))
                g.setColorAt(1, QColor(col.red(), col.green(), col.blue(), self.A(70)))
                p.setBrush(QBrush(g))
                p.drawRoundedRect(QRectF(x, h - bh, ww, bh), ww * 0.3, ww * 0.3)
                th = bh * 0.45
                g2 = QLinearGradient(0, 0, 0, th)
                g2.setColorAt(0, QColor(col.red(), col.green(), col.blue(), self.A(70)))
                g2.setColorAt(1, QColor(col.red(), col.green(), col.blue(), self.A(255)))
                p.setBrush(QBrush(g2))
                p.drawRoundedRect(QRectF(x, 0, ww, th), ww * 0.3, ww * 0.3)
        elif style == "Spectrum edge":
            p.setPen(Qt.PenStyle.NoPen)
            for r, col in self.rects:
                c = QColor(col)
                c.setAlpha(self.A(235))
                p.setBrush(c)
                p.drawRoundedRect(r, 3, 3)

    def paint(self, p, w, h, opaque):
        q = self.canvas(w, h)
        try:
            fn = self.style_fns.get(settings["style"], self.aurora)
            fn(q, w, h)
        except Exception:
            log_exception(*sys.exc_info())
        finally:
            q.end()
        p.save()
        try:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.setCompositionMode(OVER if opaque else QPainter.CompositionMode.CompositionMode_Source)
            p.drawImage(QRectF(0, 0, w, h), self.cv)
            p.setCompositionMode(OVER)
            self.crisp(p, w, h)
        except Exception:
            log_exception(*sys.exc_info())
        finally:
            p.restore()


# ======================================================================
#  Glow overlay (click-through, covers the screen)
# ======================================================================
class Glow(QWidget):
    def __init__(self, fx):
        super().__init__()
        self.fx = fx
        self.takeover = None
        self.refresh_hz = 60.0
        self.cur_iv = 16
        self.period_ms = 16.0
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(QGuiApplication.primaryScreen().geometry())
        self.t_last = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)   # sleeps (4 checks/s) until the glow is shown

    def target_interval(self):
        mode = settings["fps"]
        fps = min(144.0, max(30.0, self.refresh_hz)) if mode == "Auto" else float(mode)
        return max(4, int(round(1000.0 / fps)))

    def showEvent(self, e):
        scr = QGuiApplication.primaryScreen()
        self.setGeometry(scr.geometry())
        self.refresh_hz = scr.refreshRate() or 60.0
        self.cur_iv = self.target_interval()
        self.period_ms = float(self.cur_iv)
        self.timer.setInterval(self.cur_iv)
        self.t_last = time.monotonic()
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.setInterval(250)
        super().hideEvent(e)

    def tick(self):
        tk = self.takeover is not None and self.takeover.isVisible()
        if not self.isVisible() and not tk:
            return
        now = time.monotonic()
        dt = min(0.1, now - self.t_last)
        self.period_ms += ((now - self.t_last) * 1000.0 - self.period_ms) * 0.1
        self.t_last = now

        # Auto = follow the monitor (up to 144 Hz), but back off if the PC can't keep up
        base = self.target_interval()
        if settings["fps"] == "Auto":
            busy = self.fx.paint_ms > self.cur_iv * 0.85 or self.period_ms > self.cur_iv * 1.3
            calm = self.fx.paint_ms < self.cur_iv * 0.5 and self.period_ms < self.cur_iv * 1.1
            if busy:
                self.cur_iv = min(33, self.cur_iv + 1)
            elif calm:
                self.cur_iv = max(base, self.cur_iv - 1)
            self.cur_iv = max(self.cur_iv, base)
        else:
            self.cur_iv = base
        if self.cur_iv != self.timer.interval():
            self.timer.setInterval(self.cur_iv)

        self.fx.step(dt, self.width(), self.height())
        if tk:
            self.takeover.update()   # the now-playing screen draws the glow itself
        else:
            self.update()

    def paintEvent(self, e):
        t0 = time.perf_counter()
        p = QPainter(self)
        try:
            self.fx.paint(p, self.width(), self.height(), False)
        finally:
            p.end()
        self.fx.paint_ms += ((time.perf_counter() - t0) * 1000.0 - self.fx.paint_ms) * 0.1


# ======================================================================
#  Now-playing screen: modern full-screen view with glow + cover + lyrics
#  (OpenGL version runs on the GPU; the plain version is the fallback)
# ======================================================================
class TakeoverMixin:
    def init_takeover(self, fx):
        self.fx = fx
        self.sticky = False
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.shown_at = 0.0
        self.tick_at_show = 0
        self.bg = None
        self.bg_key = None
        self.cover_cache = None
        self.cover_key = None
        self.mesh = None
        self.vig = None
        self.vig_key = None
        self.last_key = ""
        self.track_t0 = 0.0
        self.t0 = time.monotonic()

    def show_full(self, sticky=False):
        """sticky=False: idle mode (any mouse/key closes it).
        sticky=True: 'right away' mode (Esc, click, or the hotkey closes it)."""
        self.sticky = sticky
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not sticky)
        self.setCursor(Qt.CursorShape.ArrowCursor if sticky else Qt.CursorShape.BlankCursor)
        self.setGeometry(QGuiApplication.primaryScreen().geometry())
        self.tick_at_show = last_input_tick()
        self.shown_at = time.monotonic()
        self.track_t0 = self.shown_at
        self.showFullScreen()
        if sticky:
            self.raise_()
            self.activateWindow()
            self.setFocus()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if self.sticky:
            self.hide()

    # ---- cached pieces ----
    def cover_source(self):
        return art["hd"] or art["pixmap"]

    def background(self, w, h):
        src = self.cover_source()
        key = (id(src), w, h)
        if self.bg_key != key:
            bg = QPixmap(w, h)
            bg.fill(QColor(8, 8, 12))
            if src:
                q = QPainter(bg)
                try:
                    q.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                    small = src.scaled(32, 32, Qt.AspectRatioMode.IgnoreAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                    q.drawPixmap(bg.rect(), small)
                    q.fillRect(bg.rect(), QColor(0, 0, 0, 140))
                finally:
                    q.end()
            self.bg, self.bg_key = bg, key
        return self.bg

    def cover_pixmap(self, size):
        src = self.cover_source()
        if not src:
            return None
        dpr = self.devicePixelRatioF()
        key = (id(src), int(size), dpr)
        if self.cover_key != key:
            self.cover_cache = rounded_pixmap(src, int(size), size * 0.045, dpr)
            self.cover_key = key
        return self.cover_cache

    def draw_mesh(self, p, w, h):
        """Slowly drifting colored light behind everything (modern gradient mesh)."""
        cw, ch = 96, max(16, int(96 * h / w))
        if self.mesh is None or self.mesh.width() != cw or self.mesh.height() != ch:
            self.mesh = QImage(cw, ch, QImage.Format.Format_ARGB32_Premultiplied)
        self.mesh.fill(Qt.GlobalColor.transparent)
        q = QPainter(self.mesh)
        try:
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            q.scale(cw / w, ch / h)
            q.setCompositionMode(PLUS)
            q.setPen(Qt.PenStyle.NoPen)
            t = time.monotonic() - self.t0
            s = self.fx.smooth
            for i in range(4):
                col = self.fx.pal[i % 3]
                cx = w * (0.5 + 0.42 * math.sin(t * (0.07 + 0.03 * i) + i * 1.9))
                cy = h * (0.5 + 0.40 * math.cos(t * (0.06 + 0.025 * i) + i * 2.3))
                r = max(w, h) * (0.34 + 0.05 * i + 0.05 * s)
                g = QRadialGradient(QPointF(cx, cy), r)
                c = QColor(col)
                for stop, f in ((0.0, 0.5 + 0.2 * s), (0.5, 0.2), (1.0, 0.0)):
                    c.setAlpha(int(255 * f * 0.55))
                    g.setColorAt(stop, c)
                q.setBrush(QBrush(g))
                q.drawEllipse(QPointF(cx, cy), r, r)
        finally:
            q.end()
        p.save()
        try:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawImage(QRectF(0, 0, w, h), self.mesh)
        finally:
            p.restore()

    def draw_vignette(self, p, w, h):
        """Dark edges. Drawn once at low resolution, then scaled up (cheap every frame)."""
        key = (w, h)
        if self.vig_key != key:
            sw, sh = max(16, w // 8), max(16, h // 8)
            pm = QPixmap(sw, sh)
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            try:
                g = QRadialGradient(QPointF(sw / 2, sh / 2), math.hypot(sw, sh) * 0.55)
                g.setColorAt(0.0, QColor(0, 0, 0, 0))
                g.setColorAt(0.55, QColor(0, 0, 0, 0))
                g.setColorAt(1.0, QColor(0, 0, 0, 190))
                q.fillRect(QRectF(0, 0, sw, sh), QBrush(g))
            finally:
                q.end()
            self.vig, self.vig_key = pm, key
        p.save()
        try:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawPixmap(QRectF(0, 0, w, h), self.vig, QRectF(0, 0, self.vig.width(), self.vig.height()))
        finally:
            p.restore()

    # ---- content blocks ----
    def draw_cover_block(self, p, rect, accent, ease):
        s, kick = self.fx.smooth, self.fx.kick
        sc = 1.0 + 0.025 * s + 0.02 * kick - (1 - ease) * 0.05
        c = rect.center()
        p.save()
        try:
            p.translate(c)
            p.scale(sc, sc)
            p.translate(-c)
            p.setPen(Qt.PenStyle.NoPen)
            for e, a in ((70, 7), (46, 11), (28, 16), (12, 24)):  # soft colored shadow
                col = QColor(accent)
                col.setAlpha(int(a * (0.8 + 0.9 * s)))
                p.setBrush(col)
                p.drawRoundedRect(rect.adjusted(-e, -e + 22, e, e + 22), 30 + e, 30 + e)
            cov = self.cover_pixmap(rect.width())
            if cov:
                p.drawPixmap(rect.toRect(), cov)
            else:
                p.setBrush(QColor(40, 40, 60))
                p.drawRoundedRect(rect, rect.width() * 0.045, rect.width() * 0.045)
                draw_text(p, rect, A_CENTER, "\u266a", rect.height() * 0.4, False, QColor(255, 255, 255, 90))
            p.setPen(QPen(QColor(255, 255, 255, 30), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, rect.width() * 0.045, rect.width() * 0.045)
        finally:
            p.restore()

    def draw_meta(self, p, x, y, width, h, center, ease):
        title, artist, album = media["title"], media["artist"], media["album"]
        dim = QColor(255, 255, 255, 185)
        faint = QColor(255, 255, 255, 120)
        a_t = A_TOPC if center else A_TOPL
        p.save()
        try:
            p.setOpacity(ease)
            p.translate(0, (1 - ease) * h * 0.025)
            draw_text(p, QRectF(x, y, width, h * 0.06), a_t, title, h * 0.042, True, None, True)
            draw_text(p, QRectF(x, y + h * 0.062, width, h * 0.04), a_t, artist, h * 0.028, False, dim, True)
            if album:
                draw_text(p, QRectF(x, y + h * 0.098, width, h * 0.03), a_t, album, h * 0.02, False, faint, True)
            draw_progress(p, QRectF(x, y + h * 0.15, width, 5), h * 0.017)
        finally:
            p.restore()

    def draw_clock(self, p, w, h):
        now = datetime.now()
        hour = now.strftime("%I:%M").lstrip("0")
        text = f"{hour} {now.strftime('%p')}"
        draw_text(p, QRectF(w * 0.55, h * 0.04, w * 0.4, h * 0.06), A_TOPR, text,
                  h * 0.045, True, QColor(255, 255, 255, 215), True)
        draw_text(p, QRectF(w * 0.55, h * 0.04 + h * 0.058, w * 0.4, h * 0.03), A_TOPR,
                  now.strftime("%A, %d %B"), h * 0.02, False, QColor(255, 255, 255, 120), True)

    def lyrics_or_status(self, p, rect, left, px, accent):
        try:
            ok = lyric_view.paint(p, rect, left, px, accent)
        except Exception:
            log_exception(*sys.exc_info())
            ok = True
        if not ok:
            align = A_LEFT if left else A_CENTER
            draw_text(p, rect, align, lyric_status_text(), px * 0.7, False, QColor(255, 255, 255, 110))

    def render_frame(self, p):
        t0 = time.perf_counter()
        try:
            self.draw(p)
        except Exception:
            log_exception(*sys.exc_info())
        self.fx.paint_ms += ((time.perf_counter() - t0) * 1000.0 - self.fx.paint_ms) * 0.1

    def draw(self, p):
        p.setRenderHints(QPainter.RenderHint.Antialiasing
                         | QPainter.RenderHint.SmoothPixmapTransform
                         | QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()
        now = time.monotonic()
        p.drawPixmap(0, 0, self.background(w, h))
        self.draw_mesh(p, w, h)
        self.draw_vignette(p, w, h)
        self.fx.paint(p, w, h, True)

        if media["key"] != self.last_key:
            self.last_key = media["key"]
            self.track_t0 = now
        e = min(1.0, (now - self.track_t0) / 0.9)
        ease = 1 - (1 - e) ** 3

        if settings["show_clock"]:
            self.draw_clock(p, w, h)
        if self.sticky and now - self.shown_at < 5.0:
            a = int(150 * min(1.0, 5.0 - (now - self.shown_at)))
            draw_text(p, QRectF(0, h * 0.95, w, h * 0.04), A_TOPC,
                      "Press Esc or click to close", h * 0.018, False, QColor(255, 255, 255, a))

        if not media["active"]:
            draw_text(p, QRectF(0, 0, w, h), A_CENTER, "\u266a  Nothing playing",
                      h * 0.05, False, QColor(255, 255, 255, 120))
            return

        accent = self.fx.pal[0]
        label = QColor(255, 255, 255, 120)
        mode = settings["takeover_mode"]

        if mode == "Album cover":
            S = h * 0.5
            cx, y0 = (w - S) / 2, h * 0.12
            draw_text(p, QRectF(cx, y0 - h * 0.05, S, h * 0.03), A_TOPC, "NOW PLAYING",
                      h * 0.015, True, label, True, h * 0.004)
            p.save()
            try:
                p.setOpacity(ease)
                self.draw_cover_block(p, QRectF(cx, y0, S, S), accent, ease)
            finally:
                p.restore()
            self.draw_meta(p, w * 0.25, y0 + S + h * 0.05, w * 0.5, h, True, ease)

        elif mode == "Album + lyrics":
            S = h * 0.46
            x0 = w * 0.075
            y0 = (h - (S + h * 0.24)) / 2 + h * 0.02
            draw_text(p, QRectF(x0, y0 - h * 0.05, S, h * 0.03), A_TOPL, "NOW PLAYING",
                      h * 0.015, True, label, True, h * 0.004)
            p.save()
            try:
                p.setOpacity(ease)
                self.draw_cover_block(p, QRectF(x0, y0, S, S), accent, ease)
            finally:
                p.restore()
            self.draw_meta(p, x0, y0 + S + h * 0.045, S, h, False, ease)
            lx = x0 + S + w * 0.07
            self.lyrics_or_status(p, QRectF(lx, h * 0.05, w * 0.94 - lx, h * 0.9), True, h * 0.05, accent)

        else:  # Lyrics
            draw_text(p, QRectF(w * 0.06, h * 0.045, w * 0.4, h * 0.03), A_TOPL, "NOW PLAYING",
                      h * 0.015, True, label, True, h * 0.004)
            self.lyrics_or_status(p, QRectF(w * 0.12, h * 0.1, w * 0.76, h * 0.78), False, h * 0.06, accent)
            draw_text(p, QRectF(w * 0.2, h * 0.9, w * 0.6, h * 0.05), A_TOPC,
                      f"{media['title']} \u2014 {media['artist']}", h * 0.024, False,
                      QColor(255, 255, 255, 170), True)


if HAVE_GL:
    class TakeoverGL(TakeoverMixin, QOpenGLWidget):
        """Runs on the GPU (OpenGL) with vsync, so it follows your monitor's refresh rate."""

        def __init__(self, fx):
            QOpenGLWidget.__init__(self)
            self.init_takeover(fx)

        def paintGL(self):
            p = QPainter(self)
            try:
                if not gpu_info["renderer"]:
                    detect_gpu()
                self.render_frame(p)
            finally:
                p.end()


class TakeoverCPU(TakeoverMixin, QWidget):
    def __init__(self, fx):
        QWidget.__init__(self)
        self.init_takeover(fx)

    def paintEvent(self, e):
        p = QPainter(self)
        try:
            self.render_frame(p)
        finally:
            p.end()


# ======================================================================
#  Control window
# ======================================================================
STYLE = """
QWidget { background:#080911; color:#f6f7ff; font-family:'Segoe UI'; font-size:13px; }
QTabWidget::pane { border:1px solid #1b1e33; border-radius:8px; }
QTabBar::tab { background:#10121f; padding:8px 14px; border-top-left-radius:8px;
               border-top-right-radius:8px; margin-right:2px; }
QTabBar::tab:selected { background:#8b5cff; color:white; }
QPushButton { background:#14172a; border:1px solid #242842; border-radius:8px; padding:6px 12px; }
QPushButton:hover { background:#1d2140; }
QSlider::groove:horizontal { height:6px; background:#1b1e33; border-radius:3px; }
QSlider::sub-page:horizontal { background:#8b5cff; border-radius:3px; }
QSlider::handle:horizontal { background:#ffffff; width:16px; margin:-5px 0; border-radius:8px; }
QComboBox, QSpinBox, QKeySequenceEdit { background:#10121f; border:1px solid #242842;
               border-radius:6px; padding:4px 8px; }
QCheckBox::indicator { width:18px; height:18px; border-radius:5px;
               border:1px solid #343a5c; background:#10121f; }
QCheckBox::indicator:checked { background:#8b5cff; border-color:#8b5cff; }
QLabel#dim { color:#a6abc4; }
QLabel#title { font-size:20px; font-weight:600; }
QLabel#song { font-size:16px; font-weight:600; }
"""
HINT_CSS = "color:#80859e; font-size:11px;"
START_CSS = "QPushButton{background:#8b5cff;border:none;font-size:16px;font-weight:600;padding:12px;}QPushButton:hover{background:#9d78ff;}"
STOP_CSS = "QPushButton{background:#e5484d;border:none;font-size:16px;font-weight:600;padding:12px;}QPushButton:hover{background:#f05a5f;}"


def pct(v):
    return f"{v}%"


def make_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#080911"))
        p.drawRoundedRect(QRectF(0, 0, 64, 64), 16, 16)
        g = QLinearGradient(0, 0, 64, 64)
        g.setColorAt(0, QColor("#8b5cff"))
        g.setColorAt(1, QColor("#46e5f5"))
        p.setPen(QPen(QBrush(g), 6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(32, 32), 15, 15)
    finally:
        p.end()
    return QIcon(pix)


class Panel(QWidget):
    def __init__(self, glow, audio, takeover, hk1, hk2):
        super().__init__()
        self.glow, self.audio, self.takeover = glow, audio, takeover
        self.hk1, self.hk2 = hk1, hk2
        self.tray = None
        self.running = False
        self.really_quit = False
        self.refreshers = []
        self.seen_thumb = -1
        self.seen_hd = -1
        self.setStyleSheet(STYLE)
        self.setWindowTitle("Glow")
        self.setFixedWidth(430)

        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(save_settings)

        root = QVBoxLayout(self)

        title = QLabel("Glow")
        title.setObjectName("title")
        root.addWidget(title)

        head = QHBoxLayout()
        self.cover_lbl = QLabel()
        self.cover_lbl.setFixedSize(64, 64)
        self.cover_lbl.setStyleSheet("background:#10121f; border-radius:12px;")
        head.addWidget(self.cover_lbl)
        col = QVBoxLayout()
        col.addStretch()
        self.song_lbl = QLabel("Nothing playing")   # song name, beside the cover
        self.song_lbl.setObjectName("song")
        col.addWidget(self.song_lbl)
        self.now = QLabel("")                        # artist
        self.now.setObjectName("dim")
        col.addWidget(self.now)
        col.addStretch()
        head.addLayout(col, 1)
        root.addLayout(head)

        self.button = QPushButton("Start")
        self.button.setStyleSheet(START_CSS)
        self.button.clicked.connect(self.toggle)
        root.addWidget(self.button)

        tabs = QTabWidget()
        root.addWidget(tabs)

        # --- Glow tab ---
        t1 = QWidget()
        f1 = QFormLayout(t1)
        self.row_combo(f1, "Style", "style", STYLES)
        self.row_combo(f1, "Colors", "color_mode", COLOR_MODES)
        cbox = QHBoxLayout()
        cbox.addWidget(self.color_button("color1"))
        cbox.addWidget(self.color_button("color2"))
        cbox.addStretch()
        f1.addRow("Custom colors", cbox)
        self.row_slider(f1, "Brightness", "bright", 10, 100, pct, "How strong the light is.")
        self.row_slider(f1, "Size", "size", 10, 100, pct, "How big the effect is on screen.")
        self.row_slider(f1, "Reaction", "react", 0, 100, pct,
                        "How much it grows and pulses with the beat. 0 = ignores the music.")
        self.row_slider(f1, "Sensitivity", "sens", 1, 100, pct,
                        "How easily it picks up sound.")
        self.row_slider(f1, "Speed", "speed", 0, 100, pct,
                        "How fast things move and colors flow. 0 = frozen.")
        self.row_slider(f1, "Beat delay", "sync_delay", 0, 500, lambda v: f"{v} ms",
                        "Raise this if the glow hits before you hear the beat (Bluetooth headphones).")
        tabs.addTab(t1, "Glow")

        # --- Music tab ---
        t2 = QWidget()
        f2 = QFormLayout(t2)
        self.row_slider(f2, "Lyrics timing", "lyric_offset", -30, 30,
                        lambda v: f"{v / 10:+.1f}s",
                        "Use + if lyrics show too late, \u2212 if too early.")
        sw = QHBoxLayout()
        self.sw = [QLabel(), QLabel(), QLabel()]
        for s in self.sw:
            s.setFixedSize(26, 26)
            s.setStyleSheet("background:#10121f; border-radius:6px;")
            sw.addWidget(s)
        sw.addStretch()
        f2.addRow("Colors from cover", sw)
        self.cover_info = QLabel("")
        self.cover_info.setObjectName("dim")
        self.cover_info.setWordWrap(True)
        f2.addRow(self.cover_info)
        self.lyr_label = QLabel("")
        self.lyr_label.setObjectName("dim")
        self.lyr_label.setWordWrap(True)
        f2.addRow(self.lyr_label)
        tabs.addTab(t2, "Music")

        # --- Screen tab ---
        t3 = QWidget()
        f3 = QFormLayout(t3)
        self.row_combo(f3, "Now-playing screen", "show_mode", SHOW_MODES)
        self.hint_row(f3, "When idle = appears after you stop using the PC. "
                          "Right away = opens when you press Start.")
        self.row_combo(f3, "Screen shows", "takeover_mode", TAKEOVER_MODES)
        self.hint_row(f3, "Lyrics come from LRCLIB (it receives the song's title, artist, album and length). "
                          "Choose 'Album cover' to skip lyric lookups.")
        self.row_spin(f3, "Idle after", "idle_secs", 5, 3600, " s")
        self.row_check(f3, "Idle screen only while music plays", "idle_only_playing")
        self.row_check(f3, "Show clock", "show_clock")
        open_btn = QPushButton("Open / close now-playing screen")
        open_btn.clicked.connect(self.toggle_nowplaying)
        f3.addRow(open_btn)
        self.hotkey_row(f3, "Start/Stop hotkey", "hotkey", self.hk1)
        self.hotkey_row(f3, "Now-playing hotkey", "hotkey_np", self.hk2)
        tabs.addTab(t3, "Screen")

        # --- Display tab ---
        t4 = QWidget()
        f4 = QFormLayout(t4)
        self.row_combo(f4, "Frame rate", "fps", FPS_MODES)
        self.hint_row(f4, "Auto follows your monitor (up to 144 Hz). Higher = smoother but uses more CPU.")
        self.row_check(f4, "HD album art (online)", "hd_art")
        self.hint_row(f4, "Looks up the song's title and artist on Apple's iTunes Search and, if needed, Deezer.")
        self.row_check(f4, "GPU for now-playing screen (restart)", "gpu")
        nv = QPushButton("Prefer NVIDIA GPU for Glow")
        nv.clicked.connect(self.use_nvidia)
        f4.addRow(nv)
        self.gpu_label = QLabel("")
        self.gpu_label.setObjectName("dim")
        self.gpu_label.setWordWrap(True)
        f4.addRow(self.gpu_label)
        reset = QPushButton("Reset all settings to defaults")
        reset.clicked.connect(self.reset)
        f4.addRow(reset)
        tabs.addTab(t4, "Display")

        self.status = QLabel("")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.slow = QTimer(self)
        self.slow.timeout.connect(self.slow_tick)
        self.slow.start(300)

    # ---------- small UI builders ----------
    def changed(self):
        self.save_timer.start(500)

    def note(self, text):
        self.status.setText(text)

    def refresh_ui(self):
        for r in self.refreshers:
            r()

    def hint_row(self, form, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(HINT_CSS)
        lbl.setWordWrap(True)
        form.addRow(lbl)

    def row_slider(self, form, label, key, lo, hi, fmt=str, hint=None):
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        val = QLabel()
        val.setObjectName("dim")
        val.setFixedWidth(54)
        box = QHBoxLayout()
        box.addWidget(s)
        box.addWidget(val)

        def on(v):
            settings[key] = v
            val.setText(fmt(v))
            self.changed()

        def refresh():
            s.blockSignals(True)
            s.setValue(settings[key])
            s.blockSignals(False)
            val.setText(fmt(settings[key]))

        s.valueChanged.connect(on)
        self.refreshers.append(refresh)
        refresh()
        form.addRow(label, box)
        if hint:
            self.hint_row(form, hint)

    def row_combo(self, form, label, key, items):
        c = QComboBox()
        c.addItems(items)

        def on(t):
            settings[key] = t
            self.changed()

        def refresh():
            c.blockSignals(True)
            c.setCurrentText(settings[key])
            c.blockSignals(False)

        c.currentTextChanged.connect(on)
        self.refreshers.append(refresh)
        refresh()
        form.addRow(label, c)

    def row_check(self, form, label, key):
        c = QCheckBox(label)

        def on(v):
            settings[key] = bool(v)
            self.changed()

        def refresh():
            c.blockSignals(True)
            c.setChecked(settings[key])
            c.blockSignals(False)

        c.toggled.connect(on)
        self.refreshers.append(refresh)
        refresh()
        form.addRow(c)

    def row_spin(self, form, label, key, lo, hi, suffix):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setSuffix(suffix)

        def on(v):
            settings[key] = v
            self.changed()

        def refresh():
            s.blockSignals(True)
            s.setValue(settings[key])
            s.blockSignals(False)

        s.valueChanged.connect(on)
        self.refreshers.append(refresh)
        refresh()
        form.addRow(label, s)

    def color_button(self, key):
        b = QPushButton()
        b.setFixedSize(46, 26)

        def paint():
            b.setStyleSheet(f"background:{settings[key]}; border:1px solid #666; border-radius:6px;")

        def pick():
            c = QColorDialog.getColor(QColor(settings[key]), self)
            if c.isValid():
                settings[key] = c.name()
                settings["color_mode"] = "Custom"
                self.refresh_ui()
                self.changed()

        b.clicked.connect(pick)
        self.refreshers.append(paint)
        paint()
        return b

    def hotkey_row(self, form, label, key, hk):
        edit = QKeySequenceEdit()
        if hasattr(edit, "setMaximumSequenceLength"):
            edit.setMaximumSequenceLength(1)

        def on(seq):
            text = seq.toString()
            if not text:
                return
            if parse_hotkey(text) is None:
                self.note("Hotkey needs Ctrl, Alt or Shift plus a key.")
                return
            settings[key] = text
            hk.register(text)
            self.note(f"Hotkey set: {text}")
            self.changed()

        def refresh():
            edit.blockSignals(True)
            edit.setKeySequence(QKeySequence(settings[key]))
            edit.blockSignals(False)

        edit.keySequenceChanged.connect(on)
        self.refreshers.append(refresh)
        refresh()
        form.addRow(label, edit)

    # ---------- actions ----------
    def reset(self):
        ans = QMessageBox.question(self, "Reset", "Reset all settings to default?")
        if ans != QMessageBox.StandardButton.Yes:
            return
        settings.update(DEFAULTS)
        self.refresh_ui()
        self.hk1.register(settings["hotkey"])
        self.hk2.register(settings["hotkey_np"])
        save_settings()
        self.note("Settings reset.")

    def use_nvidia(self):
        if prefer_nvidia():
            self.note("Done. Quit Glow and start it again so Windows uses your NVIDIA GPU.")
        else:
            self.note("Couldn't set it. Windows Settings > System > Display > Graphics: "
                      "add python.exe and choose High performance.")

    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def toggle_nowplaying(self):
        if self.takeover.isVisible():
            self.takeover.hide()
            return
        if not self.running:
            self.start()
        if self.running and not self.takeover.isVisible():
            self.takeover.show_full(True)

    def start(self):
        try:
            self.audio.start()
        except Exception as e:
            self.note(f"Audio error: {e}")
            return
        self.glow.show()
        self.running = True
        self.button.setText("Stop")
        self.button.setStyleSheet(STOP_CSS)
        self.note("Running.")
        if settings["show_mode"] == "Right away":
            self.takeover.show_full(True)

    def stop(self):
        self.audio.stop()
        self.glow.hide()
        self.takeover.hide()
        self.running = False
        self.button.setText("Start")
        self.button.setStyleSheet(START_CSS)
        self.note("Stopped.")

    def update_cover_widgets(self):
        pix = art["pixmap"] or art["hd"]
        self.cover_lbl.setPixmap(rounded_pixmap(pix, 64, 12, self.devicePixelRatioF()) if pix else QPixmap())
        for lbl, key in zip(self.sw, ("c1", "c2", "c3")):
            c = art[key]
            col = c.name() if c is not None else "#10121f"
            lbl.setStyleSheet(f"background:{col}; border-radius:6px;")

    def set_palette_from(self, img):
        try:
            art["c1"], art["c2"], art["c3"] = extract_palette(img)
        except Exception:
            log_exception(*sys.exc_info())

    def slow_tick(self):
        # new album cover from the music app -> picture + colors
        if media["thumb_id"] != self.seen_thumb:
            self.seen_thumb = media["thumb_id"]
            tb = media["thumb_bytes"]
            art["pixmap"] = art["c1"] = art["c2"] = art["c3"] = None
            if tb:
                img = QImage.fromData(tb)
                if not img.isNull():
                    art["pixmap"] = QPixmap.fromImage(img)
                    self.set_palette_from(img)
            self.update_cover_widgets()

        # HD cover from the internet (decoded in a worker thread)
        if not media["key"] or not settings["hd_art"]:
            hd["key"] = ""
            art["hd"] = None
        elif hd["key"] != media["key"]:
            art["hd"] = None
            start_hd_fetch(media["key"], media["artist"], media["title"], media["album"],
                           media["raw_artist"], media["raw_title"])
        if hd["id"] != self.seen_hd:
            self.seen_hd = hd["id"]
            if hd["image"] is not None and settings["hd_art"]:
                art["hd"] = QPixmap.fromImage(hd["image"])
                if hd["pal"]:
                    art["c1"], art["c2"], art["c3"] = hd["pal"]
                hd["image"] = None   # free the extra copy
                self.update_cover_widgets()

        if media["active"]:
            if art["hd"]:
                self.cover_info.setText(f"HD cover loaded \u2713 ({art['hd'].width()} px)")
            elif art["pixmap"]:
                self.cover_info.setText(f"Cover loaded \u2713 ({art['pixmap'].width()} px, no HD found)")
            else:
                self.cover_info.setText(f"No cover: {media['thumb_info']}")
        else:
            self.cover_info.setText("")

        # song name + artist beside the cover (the big title above stays "Glow")
        f = QFont(self.song_lbl.font())
        f.setPixelSize(16)
        f.setBold(True)
        fm = QFontMetrics(f)
        if media["error"]:
            self.song_lbl.setText("Song info unavailable")
            self.now.setText("")
            self.lyr_label.setText("Install: py -m pip install winrt-runtime winrt-Windows.Media "
                                   "winrt-Windows.Media.Control winrt-Windows.Storage.Streams "
                                   "winrt-Windows.Foundation winrt-Windows.Foundation.Collections")
            tip = "Glow"
        elif media["active"] and media["title"]:
            icon = "\u25b6" if media["playing"] else "\u23f8"
            self.song_lbl.setText(fm.elidedText(media["title"], Qt.TextElideMode.ElideRight, 290))
            self.now.setText(f"{icon}  {media['artist'] or 'Unknown artist'}")
            self.lyr_label.setText(lyric_status_text() if lyr["status"] != "synced" else "Lyrics: synced \u2713")
            tip = f"{media['title']} \u2014 {media['artist']}"
        else:
            self.song_lbl.setText("Nothing playing")
            self.now.setText("")
            self.lyr_label.setText("")
            tip = "Glow"
        if self.tray is not None:
            self.tray.setToolTip(tip)

        # GPU info
        if isinstance(self.takeover, TakeoverCPU):
            self.gpu_label.setText("Now-playing screen is using the CPU (GPU option off or OpenGL missing).")
        elif gpu_info["renderer"]:
            self.gpu_label.setText(f"Now-playing screen GPU: {gpu_info['renderer']}")
        else:
            self.gpu_label.setText("Open the now-playing screen once to see which GPU it uses.")

        # fetch lyrics only when the now-playing screen needs them
        tk = self.takeover
        need = self.running and "yrics" in settings["takeover_mode"] and \
            (settings["show_mode"] != "Off" or tk.isVisible())
        if need and media["active"] and media["key"] and lyr["key"] != media["key"]:
            cands = lyric_candidates(media["artist"], media["title"],
                                     media["raw_artist"], media["raw_title"])
            start_lyrics_fetch(media["key"], cands, media["album"], media["duration"])

        # now-playing screen show / hide
        if tk.isVisible():
            if not self.running:
                tk.hide()
            elif not tk.sticky and time.monotonic() - tk.shown_at > 0.7 and \
                    last_input_tick() != tk.tick_at_show:
                tk.hide()   # idle mode: any mouse/key closes it
        elif self.running and settings["show_mode"] == "When idle":
            if idle_seconds() >= settings["idle_secs"] and \
                    (media["playing"] or not settings["idle_only_playing"]):
                tk.show_full(False)

    def closeEvent(self, event):
        if self.really_quit:
            event.accept()
        else:
            event.ignore()
            self.hide()


def main():
    load_settings()
    use_gl = HAVE_GL and settings["gpu"]
    if use_gl:
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_UseDesktopOpenGL)
        fmt = QSurfaceFormat()
        fmt.setSwapInterval(1)   # vsync: follows the monitor's refresh rate
        fmt.setSamples(4)        # smooth edges
        QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    icon = make_icon()
    fx = Fx()
    audio = Audio()
    glow = Glow(fx)
    takeover = TakeoverGL(fx) if use_gl else TakeoverCPU(fx)
    glow.takeover = takeover
    hk1, hk2 = Hotkey(1), Hotkey(2)
    panel = Panel(glow, audio, takeover, hk1, hk2)
    panel.setWindowIcon(icon)
    panel.show()

    threading.Thread(target=media_worker, daemon=True).start()

    hk1.pressed.connect(panel.toggle)
    hk2.pressed.connect(panel.toggle_nowplaying)
    for hk in (hk1, hk2):
        hk.failed.connect(lambda: panel.note("A hotkey is used by another app. Pick a different one."))
    hk1.register(settings["hotkey"])
    hk2.register(settings["hotkey_np"])

    tray = QSystemTrayIcon(icon)
    panel.tray = tray
    menu = QMenu()
    show_action = QAction("Open Glow")
    show_action.triggered.connect(lambda: (panel.show(), panel.activateWindow()))
    toggle_action = QAction("Start / Stop")
    toggle_action.triggered.connect(panel.toggle)
    np_action = QAction("Now-playing screen")
    np_action.triggered.connect(panel.toggle_nowplaying)
    quit_action = QAction("Quit")

    def quit_all():
        panel.stop()
        panel.really_quit = True
        save_settings()
        hk1.stop()
        hk2.stop()
        app.quit()

    quit_action.triggered.connect(quit_all)
    for a in (show_action, toggle_action, np_action, quit_action):
        menu.addAction(a)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: panel.show()
        if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

