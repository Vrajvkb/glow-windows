# Glow

Glow is a Windows desktop app that adds audio-reactive ambient lighting around the edges of the screen. It is a personal project in development; this repository does not claim public adoption, outside contributors, or download figures.

## Features

- Eleven visual styles, including Aurora, Northern lights, Ambient glow, Neon line, Comets, Corner bloom, Ripples, Equalizer, Spectrum edge, Stage beams, and Embers.
- Color modes based on album art, rainbow colors, or custom colors.
- Controls for brightness, effect size, beat response, sensitivity, animation speed, and synchronization delay.
- A now-playing view with album art and synced lyrics.
- Windows system-tray controls and saved settings.
- A companion `index.html` landing page with a browser-based visual preview.

## Requirements

- Windows (the desktop app uses Windows APIs and audio loopback capture).
- Python with `numpy`, `PyQt6`, and `pyaudiowpatch` installed.
- For online album art and synced lyrics, an internet connection. The app looks up song metadata with Apple's iTunes Search and Deezer, and lyrics with LRCLIB. Its settings screen describes which song details are sent for those lookups.

## Run the desktop app

From this folder, install the dependencies and start Glow:

```powershell
py -m pip install numpy PyQt6 pyaudiowpatch
py glow_v7.py
```

The app saves settings and an error log in `%APPDATA%\GlowApp\`.

## Files

- `glow_v7.py` — Windows desktop application.
- `index.html` — promotional page and in-browser visual preview.

## Project status

Glow is currently under development. No release, user count, download total, or external contribution statistics are claimed here. The project has not yet published a license; usage and redistribution rights are therefore not granted by this README.

