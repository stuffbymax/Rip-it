#!/usr/bin/env python3
"""
ripit.py — Terminal UI for ripping PS1/PS2 games, music CDs, and
movie/TV DVDs on Linux, with optional metadata lookup.

Backends used (must be installed depending on what you rip):
  - ddrescue   : robust sector-by-sector reads -> .iso  (PS2, movies/TV, retries bad sectors)
  - cdrdao     : raw CD reads preserving audio tracks -> .bin/.cue (PS1 w/ CD audio)
  - cdparanoia : accurate digital audio extraction -> .wav (music CDs)
  - flac       : optional, compresses ripped audio tracks to FLAC
  - cd-info    : (libcdio-utils) optional, auto-detects track count / audio tracks
  - udevadm    : (usually preinstalled) detects disc labels / media presence
  - eject      : (usually preinstalled) ejects the tray when done

Metadata lookups (need internet, no extra Python packages — uses urllib):
  - MusicBrainz (https://musicbrainz.org) for music CD track listings — no API key needed.
  - TMDb (https://www.themoviedb.org) for movie/TV titles — free API key required,
    set it from the Settings menu in the app.

Install:
  Debian/Ubuntu : sudo apt install gddrescue cdrdao cdparanoia flac libcdio-utils eject
  Fedora        : sudo dnf install ddrescue cdrdao cdparanoia flac libcdio-utils eject
  Arch          : sudo pacman -S ddrescue cdrdao cdparanoia flac libcdio eject

Run:
  python3ripit.py           # normal TUI
  python3ripit.py --check   # just check dependencies and exit

This tool only reads discs you insert — use it for backing up media you own.
Some commercial DVDs use copy protection; this tool only images raw sectors
(it does not decrypt CSS), but circumvention rules vary by region, so check
your local laws for video discs.
"""

import curses
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------
# Config (stores the TMDb API key)
# --------------------------------------------------------------------------

CONFIG_DIR = os.path.expanduser("~/.config/gamerip-tui")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Dependency checking
# --------------------------------------------------------------------------

TOOL_INFO = {
    "ddrescue": "ISO reads for PS2 / movie / TV discs (retries bad sectors)",
    "cdrdao": "Raw CD reads preserving audio tracks (PS1 discs with music/FMV)",
    "cdparanoia": "Accurate digital audio extraction from music CDs",
    "flac": "Compresses ripped audio tracks to FLAC (optional)",
    "cd-info": "Auto-detects track count / audio tracks (optional)",
    "udevadm": "Detects disc label and whether media is inserted (optional)",
    "eject": "Ejects the tray automatically once ripping finishes (optional)",
    "toc2cue": "Converts cdrdao's .toc file into a standard .cue file (optional)",
}

INSTALL_HINTS = {
    "apt": "sudo apt install gddrescue cdrdao cdparanoia flac libcdio-utils eject",
    "dnf": "sudo dnf install ddrescue cdrdao cdparanoia flac libcdio-utils eject",
    "pacman": "sudo pacman -S ddrescue cdrdao cdparanoia flac libcdio eject",
}


def which(tool):
    return shutil.which(tool) is not None


def check_dependencies(print_report=False):
    status = {t: which(t) for t in TOOL_INFO}
    if print_report:
        print("Tools:")
        for t, desc in TOOL_INFO.items():
            print(f"  [{'OK' if status[t] else 'MISSING'}] {t:10s} - {desc}")
        if not all(status.values()):
            print("\nInstall missing tools with one of:")
            for mgr, cmd in INSTALL_HINTS.items():
                print(f"  {mgr:8s}: {cmd}")
        else:
            print("\nAll tools are present.")
    return status


def require_tool(stdscr, tool, purpose):
    """Show a blocking screen with install hints if `tool` is missing.
    Returns True if the tool is available."""
    if which(tool):
        return True
    lines = [f"'{tool}' is not installed — needed for: {purpose}", ""]
    lines.append("Install with one of:")
    for mgr, cmd in INSTALL_HINTS.items():
        lines.append(f"  {mgr}: {cmd}")
    info_screen(stdscr, lines)
    return False


# --------------------------------------------------------------------------
# Drive / disc detection
# --------------------------------------------------------------------------

def list_optical_drives():
    devs = sorted(set(glob.glob("/dev/sr*") + glob.glob("/dev/cdrom*")))
    resolved = []
    seen_real = set()
    for d in devs:
        real = os.path.realpath(d)
        if real not in seen_real and os.path.exists(real):
            seen_real.add(real)
            resolved.append(real)
    return resolved


def udev_properties(dev):
    if not which("udevadm"):
        return {}
    try:
        out = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={dev}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return {}
    props = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return props


def probe_drive(dev):
    props = udev_properties(dev)
    has_media = props.get("ID_CDROM_MEDIA") == "1"
    label = props.get("ID_FS_LABEL", "") or props.get("ID_FS_LABEL_ENC", "")
    return {"dev": dev, "has_media": has_media, "label": label}


def cdinfo_track_summary(dev):
    """Return (num_tracks, has_audio, raw_text) via cd-info, or (None, None, reason)."""
    if not which("cd-info"):
        return None, None, "cd-info not installed"
    try:
        out = subprocess.run(
            ["cd-info", "--no-header", dev],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception as e:
        return None, None, f"cd-info failed: {e}"

    tracks = []
    in_list = False
    for line in out.splitlines():
        if "Track List" in line:
            in_list = True
            continue
        if in_list:
            if not line.strip():
                break
            m = re.match(r"\s*(\d+):\s+\S+\s+\S+\s+(\w+)", line)
            if m:
                tracks.append(m.group(2).lower())
    if not tracks:
        return None, None, out[-500:] if out else "no track data returned"
    has_audio = any(t == "audio" for t in tracks)
    return len(tracks), has_audio, out


def cdparanoia_track_count(dev):
    """Fallback track-count detection using cdparanoia -Q if cd-info is unavailable."""
    if not which("cdparanoia"):
        return None
    try:
        proc = subprocess.run(
            ["cdparanoia", "-Q", "-d", dev],
            capture_output=True, text=True, timeout=20,
        )
        out = (proc.stderr or "") + (proc.stdout or "")
    except Exception:
        return None
    nums = re.findall(r"^\s*(\d+)\.\s", out, re.MULTILINE)
    if not nums:
        return None
    return max(int(n) for n in nums)


# --------------------------------------------------------------------------
# Metadata lookups
# --------------------------------------------------------------------------

USER_AGENT = "gamerip-tui/1.1 ( personal-use disc backup tool )"


def _http_get_json(url, headers=None, timeout=15):
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP error {e.code}"
    except urllib.error.URLError as e:
        return None, f"Network error: {e.reason}"
    except Exception as e:
        return None, f"Request failed: {e}"


def musicbrainz_search_release(query):
    url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": 10}
    )
    data, err = _http_get_json(url)
    if err:
        return [], err
    return data.get("releases", []), None


def musicbrainz_release_tracks(release_id):
    url = f"https://musicbrainz.org/ws/2/release/{release_id}?" + urllib.parse.urlencode(
        {"fmt": "json", "inc": "recordings"}
    )
    data, err = _http_get_json(url)
    if err:
        return None, err
    titles = []
    for medium in data.get("media", []):
        for t in medium.get("tracks", []):
            titles.append(t.get("title", "Unknown Track"))
    return titles, None


def tmdb_search(query, api_key, media_type="movie"):
    endpoint = "tv" if media_type == "tv" else "movie"
    url = f"https://api.themoviedb.org/3/search/{endpoint}?" + urllib.parse.urlencode(
        {"api_key": api_key, "query": query}
    )
    data, err = _http_get_json(url)
    if err:
        return [], err
    return data.get("results", []), None


# --------------------------------------------------------------------------
# Ripping backends (run with curses suspended so native progress shows)
# --------------------------------------------------------------------------

def rip_iso(dev, out_path):
    """ddrescue reads the whole data track to a .iso, retrying bad sectors."""
    mapfile = out_path + ".ddrescue.log"
    cmd = ["ddrescue", "-b", "2048", "-r3", "-v", dev, out_path, mapfile]
    print(f"\n$ {' '.join(cmd)}\n")
    rc = subprocess.call(cmd)
    return rc, mapfile


def rip_bin_cue(dev, out_base):
    """cdrdao raw-reads all tracks (incl. audio) to .bin + generates a .cue."""
    toc_path = out_base + ".toc"
    bin_path = out_base + ".bin"
    cue_path = out_base + ".cue"
    cmd = [
        "cdrdao", "read-cd",
        "--device", dev,
        "--driver", "generic-mmc-raw",
        "--read-raw",
        "--datafile", bin_path,
        toc_path,
    ]
    print(f"\n$ {' '.join(cmd)}\n")
    rc = subprocess.call(cmd)
    if rc == 0 and which("toc2cue"):
        subprocess.call(["toc2cue", toc_path, cue_path])
    return rc, toc_path, cue_path


def rip_audio_tracks(dev, out_dir, n, track_titles, to_flac):
    """cdparanoia extracts each audio track to .wav (optionally re-encoded to .flac)."""
    results = []
    for i in range(1, n + 1):
        title = track_titles[i - 1] if track_titles and i - 1 < len(track_titles) else f"Track {i:02d}"
        base = sanitize_filename(f"{i:02d} - {title}")
        wav_path = os.path.join(out_dir, base + ".wav")
        print(f"\n=== Track {i}/{n}: {title} ===")
        cmd = ["cdparanoia", "-d", dev, str(i), wav_path]
        print(f"$ {' '.join(cmd)}")
        rc = subprocess.call(cmd)
        final_path = wav_path
        if rc == 0 and to_flac and which("flac") and os.path.exists(wav_path):
            flac_path = os.path.join(out_dir, base + ".flac")
            subprocess.call(["flac", "--best", "-f", "-o", flac_path, wav_path])
            if os.path.exists(flac_path):
                os.remove(wav_path)
                final_path = flac_path
        results.append((i, title, rc, final_path))
    return results


# --------------------------------------------------------------------------
# curses helpers
# --------------------------------------------------------------------------

def draw_banner(stdscr, subtitle=""):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    title = "DISC RIPPER — Games / Music / Movies / TV"
    stdscr.addstr(0, max(0, (w - len(title)) // 2), title, curses.A_BOLD)
    if subtitle:
        stdscr.addstr(1, max(0, (w - len(subtitle)) // 2), subtitle, curses.A_DIM)
    stdscr.addstr(2, 0, "-" * (w - 1))


def select_from_list(stdscr, title, options, descriptions=None):
    idx = 0
    curses.curs_set(0)
    while True:
        draw_banner(stdscr, title)
        h, w = stdscr.getmaxyx()
        start_y = 4
        for i, opt in enumerate(options):
            marker = "> " if i == idx else "  "
            attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
            row = start_y + i * 2
            if row >= h - 3:
                break
            stdscr.addstr(row, 2, f"{marker}{opt}"[: w - 3], attr)
            if descriptions and i < len(descriptions) and descriptions[i] and row + 1 < h - 3:
                for j, wrapped in enumerate(textwrap.wrap(descriptions[i], max(w - 8, 10))):
                    stdscr.addstr(row + 1, 6, wrapped, curses.A_DIM)
        stdscr.addstr(h - 2, 2, "Up/Down to move, Enter to select, q to cancel", curses.A_DIM)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(options)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(options)
        elif key in (curses.KEY_ENTER, 10, 13):
            return idx
        elif key in (27, ord("q")):
            return None


def text_input(stdscr, prompt, default=""):
    curses.curs_set(1)
    draw_banner(stdscr, "")
    h, w = stdscr.getmaxyx()
    stdscr.addstr(4, 2, prompt)
    stdscr.addstr(6, 2, f"(default: {default})" if default else "", curses.A_DIM)
    stdscr.addstr(8, 2, "> ")
    stdscr.refresh()
    curses.echo()
    win = curses.newwin(1, max(w - 6, 10), 8, 4)
    win.keypad(True)
    val = win.getstr().decode("utf-8", errors="ignore").strip()
    curses.noecho()
    curses.curs_set(0)
    return val if val else default


def info_screen(stdscr, lines, wait_key=True):
    draw_banner(stdscr, "")
    h, w = stdscr.getmaxyx()
    row = 4
    for line in lines:
        for wrapped in (textwrap.wrap(line, max(w - 4, 10)) or [""]):
            if row < h - 2:
                stdscr.addstr(row, 2, wrapped)
                row += 1
    if wait_key:
        if row + 1 < h:
            stdscr.addstr(row + 1, 2, "Press any key to continue...", curses.A_DIM)
        stdscr.refresh()
        stdscr.getch()
    else:
        stdscr.refresh()


def run_with_spinner(stdscr, message, func, *args, **kwargs):
    """Run func(...) in a background thread while showing a spinner, so slow
    disc reads / network lookups don't look like a frozen screen."""
    result = {}
    error = {}

    def worker():
        try:
            result["value"] = func(*args, **kwargs)
        except Exception as e:
            error["value"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    frames = ["|", "/", "-", "\\"]
    i = 0
    while t.is_alive():
        draw_banner(stdscr, "")
        stdscr.addstr(4, 2, f"{frames[i % len(frames)]} {message}")
        stdscr.addstr(6, 2, "Please wait...", curses.A_DIM)
        stdscr.refresh()
        time.sleep(0.12)
        i += 1
    t.join()

    if "value" in error:
        return None
    return result.get("value")


def sanitize_filename(s):
    s = re.sub(r"[^\w\-. ()]", "_", s)
    return s.strip() or "untitled"


# --------------------------------------------------------------------------
# Shared steps
# --------------------------------------------------------------------------

def choose_drive(stdscr):
    drives = list_optical_drives()
    if not drives:
        val = text_input(
            stdscr,
            "No optical drives auto-detected. Enter device path manually (e.g. /dev/sr0), or leave blank to cancel:",
        )
        return val or None

    infos = run_with_spinner(
        stdscr, f"Scanning {len(drives)} drive(s) for inserted discs...",
        lambda: [probe_drive(d) for d in drives],
    ) or [probe_drive(d) for d in drives]

    options = []
    for d, info in zip(drives, infos):
        status = "disc detected" if info["has_media"] else "empty / unknown"
        label = f" \"{info['label']}\"" if info["label"] else ""
        options.append(f"{d}{label} — {status}")
    options.append("Enter device path manually")

    choice = select_from_list(stdscr, "Select optical drive", options)
    if choice is None:
        return None
    if choice == len(options) - 1:
        return text_input(stdscr, "Enter device path (e.g. /dev/sr0):") or None
    return drives[choice]


def choose_destination(stdscr, default_name):
    out_dir = text_input(stdscr, "Save to directory:", os.path.expanduser("~/Games"))
    out_dir = os.path.expanduser(out_dir)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        info_screen(stdscr, [f"Could not create directory: {e}"])
        return None, None
    name = text_input(stdscr, "Base filename (no extension):", default_name)
    name = sanitize_filename(name) or default_name
    return out_dir, name


def do_rip(stdscr, dev, fmt, out_dir, name):
    """fmt is 'iso' (ddrescue) or 'binclue' (cdrdao). Used by games and video."""
    out_base = os.path.join(out_dir, name)
    curses.endwin()

    print("=" * 70)
    print(f"Ripping {dev} -> {out_base}.{'iso' if fmt == 'iso' else 'bin/cue'}")
    print("Do not remove the disc until this finishes.")
    print("=" * 70)

    start = time.time()
    if fmt == "iso":
        rc, mapfile = rip_iso(dev, out_base + ".iso")
        elapsed = time.time() - start
        print(f"\nddrescue exit code: {rc}  (elapsed {elapsed:.0f}s)")
        result_lines = [
            f"ISO rip finished. Exit code: {rc}",
            f"File: {out_base}.iso",
            f"Log:  {mapfile}",
        ]
    else:
        rc, toc_path, cue_path = rip_bin_cue(dev, out_base)
        elapsed = time.time() - start
        print(f"\ncdrdao exit code: {rc}  (elapsed {elapsed:.0f}s)")
        result_lines = [
            f"BIN/CUE rip finished. Exit code: {rc}",
            f"Data:  {out_base}.bin",
            f"TOC:   {toc_path}",
            f"Cue:   {cue_path if os.path.exists(cue_path) else '(toc2cue not installed — .cue not generated)'}",
        ]

    print("\nPress Enter to return to the menu...")
    input()
    stdscr.refresh()
    return result_lines, rc


def do_rip_music(stdscr, dev, out_dir, n, track_titles, to_flac):
    curses.endwin()
    print("=" * 70)
    print(f"Ripping {n} audio track(s) from {dev} -> {out_dir}")
    print("Do not remove the disc until this finishes.")
    print("=" * 70)
    start = time.time()
    results = rip_audio_tracks(dev, out_dir, n, track_titles, to_flac)
    elapsed = time.time() - start
    failed = [r for r in results if r[2] != 0]
    print(f"\nDone in {elapsed:.0f}s. {len(results) - len(failed)}/{len(results)} tracks OK.")
    print("\nPress Enter to return to the menu...")
    input()
    stdscr.refresh()
    result_lines = [f"Ripped {len(results) - len(failed)}/{len(results)} tracks to {out_dir}"]
    if failed:
        result_lines.append(f"Failed tracks: {', '.join(str(r[0]) for r in failed)}")
    return result_lines, results


def maybe_eject(stdscr, dev):
    if not which("eject"):
        return
    choice = select_from_list(stdscr, "Eject the disc?", ["Yes", "No"])
    if choice == 0:
        subprocess.call(["eject", dev])


# --------------------------------------------------------------------------
# Flow: PS1 / PS2 games
# --------------------------------------------------------------------------

def choose_console_and_format(stdscr, dev):
    console_choice = select_from_list(
        stdscr, "What are you ripping?",
        ["PlayStation 1 disc", "PlayStation 2 disc"],
        ["Often has CD-audio/FMV tracks -> defaults to BIN/CUE",
         "Data-only DVD -> defaults to ISO"],
    )
    if console_choice is None:
        return None, None
    console = "PS1" if console_choice == 0 else "PS2"
    default_format = "binclue" if console == "PS1" else "iso"

    detected_note = "cd-info not available for auto-check"
    if which("cd-info"):
        n, has_audio, raw = run_with_spinner(
            stdscr, "Reading disc table of contents (cd-info)...",
            cdinfo_track_summary, dev,
        ) or (None, None, "cd-info returned nothing")
        if n is not None:
            detected_note = f"cd-info sees {n} track(s), audio present: {has_audio}"
            if has_audio:
                default_format = "binclue"

    fmt_options = [
        f"Auto (recommended: {'BIN/CUE' if default_format == 'binclue' else 'ISO'})",
        "Force ISO (ddrescue) — data-only, smaller, loses any audio tracks",
        "Force BIN/CUE (cdrdao) — raw read, preserves CD-audio tracks",
    ]
    fmt_choice = select_from_list(stdscr, "Rip format", fmt_options, [detected_note, "", ""])
    if fmt_choice is None:
        return console, None
    if fmt_choice == 0:
        fmt = default_format
    elif fmt_choice == 1:
        fmt = "iso"
    else:
        fmt = "binclue"
    return console, fmt


def flow_game(stdscr):
    dev = choose_drive(stdscr)
    if not dev:
        return
    info = probe_drive(dev)
    if not info["has_media"]:
        proceed = select_from_list(
            stdscr, f"No media detected on {dev} (or detection unsupported). Continue anyway?",
            ["Yes, try anyway", "No, go back"],
        )
        if proceed != 0:
            return

    console, fmt = choose_console_and_format(stdscr, dev)
    if fmt is None:
        return
    if fmt == "iso" and not require_tool(stdscr, "ddrescue", "ISO rips"):
        return
    if fmt == "binclue" and not require_tool(stdscr, "cdrdao", "BIN/CUE rips"):
        return

    default_name = info["label"] or console.lower()
    out_dir, name = choose_destination(stdscr, default_name)
    if not out_dir:
        return

    summary = [
        f"Drive:    {dev}",
        f"Console:  {console}",
        f"Format:   {'ISO (ddrescue)' if fmt == 'iso' else 'BIN/CUE (cdrdao)'}",
        f"Output:   {os.path.join(out_dir, name)}.{'iso' if fmt == 'iso' else 'bin/cue'}",
        "",
        "Press any key to start ripping, or q to cancel.",
    ]
    draw_banner(stdscr, "Confirm")
    for i, line in enumerate(summary):
        stdscr.addstr(4 + i, 2, line)
    stdscr.refresh()
    if stdscr.getch() == ord("q"):
        return

    result_lines, rc = do_rip(stdscr, dev, fmt, out_dir, name)
    info_screen(stdscr, result_lines)
    maybe_eject(stdscr, dev)


# --------------------------------------------------------------------------
# Flow: Music CD
# --------------------------------------------------------------------------

def flow_music(stdscr):
    if not require_tool(stdscr, "cdparanoia", "Music CD ripping"):
        return
    dev = choose_drive(stdscr)
    if not dev:
        return

    n, has_audio, raw = run_with_spinner(
        stdscr, "Reading disc table of contents...", cdinfo_track_summary, dev
    ) or (None, None, "")
    if n is None:
        n = run_with_spinner(stdscr, "Trying cdparanoia TOC read...", cdparanoia_track_count, dev)
    if n is None:
        manual = text_input(stdscr, "Could not auto-detect track count. Enter number of tracks:", "10")
        try:
            n = int(manual)
        except ValueError:
            info_screen(stdscr, ["Invalid track count, cancelling."])
            return

    query = text_input(stdscr, "Search MusicBrainz (e.g. 'Artist - Album'), or leave blank to skip:")
    track_titles = None
    album_dir_name = "Unknown Album"
    chosen_release = None

    if query:
        releases, err = run_with_spinner(stdscr, "Searching MusicBrainz...", musicbrainz_search_release, query)
        if err:
            info_screen(stdscr, [f"MusicBrainz search failed: {err}", "Continuing without metadata."])
        elif not releases:
            info_screen(stdscr, ["No matches found on MusicBrainz.", "Continuing without metadata."])
        else:
            options = []
            for r in releases[:10]:
                artist = (r.get("artist-credit") or [{}])[0].get("name", "Unknown Artist")
                date = r.get("date", "")[:4] if r.get("date") else "????"
                track_count = ""
                media = r.get("media") or []
                if media:
                    track_count = f", {media[0].get('track-count', '?')} tracks"
                options.append(f"{artist} - {r.get('title', '?')} ({date}{track_count})")
            idx = select_from_list(stdscr, "Select matching release", options)
            if idx is not None:
                chosen_release = releases[idx]
                titles, err2 = run_with_spinner(
                    stdscr, "Fetching track list...", musicbrainz_release_tracks, chosen_release["id"]
                )
                if err2:
                    info_screen(stdscr, [f"Could not fetch track list: {err2}", "Continuing without titles."])
                elif titles and len(titles) == n:
                    track_titles = titles
                    artist = (chosen_release.get("artist-credit") or [{}])[0].get("name", "Unknown Artist")
                    album_dir_name = sanitize_filename(f"{artist} - {chosen_release.get('title', 'Album')}")
                elif titles:
                    info_screen(stdscr, [
                        f"Track count mismatch: disc has {n}, MusicBrainz release has {len(titles)}.",
                        "Continuing without auto-titling.",
                    ])

    to_flac = False
    if which("flac"):
        fmt_choice = select_from_list(stdscr, "Audio format", ["WAV (uncompressed)", "FLAC (compressed, lossless)"])
        to_flac = fmt_choice == 1

    out_dir, name = choose_destination(stdscr, album_dir_name)
    if not out_dir:
        return
    album_path = os.path.join(out_dir, name)
    os.makedirs(album_path, exist_ok=True)

    summary = [
        f"Drive:    {dev}",
        f"Tracks:   {n}",
        f"Titles:   {'from MusicBrainz' if track_titles else 'generic (Track 01, 02, ...)'}",
        f"Format:   {'FLAC' if to_flac else 'WAV'}",
        f"Output:   {album_path}/",
        "",
        "Press any key to start ripping, or q to cancel.",
    ]
    draw_banner(stdscr, "Confirm")
    for i, line in enumerate(summary):
        stdscr.addstr(4 + i, 2, line)
    stdscr.refresh()
    if stdscr.getch() == ord("q"):
        return

    result_lines, results = do_rip_music(stdscr, dev, album_path, n, track_titles, to_flac)

    if chosen_release is not None:
        try:
            artist = (chosen_release.get("artist-credit") or [{}])[0].get("name", "Unknown Artist")
            meta = {
                "artist": artist,
                "album": chosen_release.get("title"),
                "date": chosen_release.get("date"),
                "musicbrainz_release_id": chosen_release.get("id"),
                "tracks": [{"number": i, "title": t} for i, t, rc, path in results],
            }
            with open(os.path.join(album_path, "metadata.json"), "w") as f:
                json.dump(meta, f, indent=2)
        except Exception:
            pass

    info_screen(stdscr, result_lines)
    maybe_eject(stdscr, dev)


# --------------------------------------------------------------------------
# Flow: Movie / TV DVD
# --------------------------------------------------------------------------

def flow_video(stdscr):
    if not require_tool(stdscr, "ddrescue", "Movie/TV disc rips"):
        return
    dev = choose_drive(stdscr)
    if not dev:
        return

    kind = select_from_list(stdscr, "What kind of disc?", ["Movie", "TV Series"])
    if kind is None:
        return
    is_tv = kind == 1

    cfg = load_config()
    api_key = cfg.get("tmdb_api_key", "")
    title = None
    year = ""
    meta = None

    if not api_key:
        info_screen(stdscr, [
            "No TMDb API key set — metadata lookup is disabled.",
            "Add a free key from the Main Menu > Settings to enable title search.",
            "Continuing with manual filename entry.",
        ])
    else:
        query = text_input(stdscr, "Search title on TMDb (leave blank to skip):")
        if query:
            results, err = run_with_spinner(
                stdscr, "Searching TMDb...", tmdb_search, query, api_key, "tv" if is_tv else "movie"
            )
            if err:
                info_screen(stdscr, [f"TMDb search failed: {err}", "Continuing without metadata."])
            elif not results:
                info_screen(stdscr, ["No matches found.", "Continuing without metadata."])
            else:
                options = []
                for r in results[:8]:
                    name = r.get("name") if is_tv else r.get("title")
                    date = r.get("first_air_date") if is_tv else r.get("release_date")
                    yr = (date or "????")[:4]
                    options.append(f"{name} ({yr})")
                idx = select_from_list(stdscr, "Select match", options)
                if idx is not None:
                    meta = results[idx]
                    title = meta.get("name") if is_tv else meta.get("title")
                    date = meta.get("first_air_date") if is_tv else meta.get("release_date")
                    year = (date or "")[:4]

    season = ""
    if is_tv:
        season = text_input(stdscr, "Season number for this disc (optional):", "")

    default_name = title or "video"
    if title and year:
        default_name = f"{title} ({year})"
    if is_tv and season:
        default_name += f" - Season {season.zfill(2)}"

    out_dir, name = choose_destination(stdscr, sanitize_filename(default_name))
    if not out_dir:
        return

    summary = [
        f"Drive:    {dev}",
        f"Type:     {'TV Series' if is_tv else 'Movie'}",
        f"Title:    {title or '(not looked up)'}",
        f"Output:   {os.path.join(out_dir, name)}.iso",
        "",
        "Press any key to start ripping, or q to cancel.",
    ]
    draw_banner(stdscr, "Confirm")
    for i, line in enumerate(summary):
        stdscr.addstr(4 + i, 2, line)
    stdscr.refresh()
    if stdscr.getch() == ord("q"):
        return

    result_lines, rc = do_rip(stdscr, dev, "iso", out_dir, name)

    if title:
        try:
            meta_out = {
                "title": title,
                "year": year,
                "season": season or None,
                "tmdb_id": meta.get("id") if meta else None,
                "overview": meta.get("overview") if meta else None,
            }
            with open(os.path.join(out_dir, name + ".metadata.json"), "w") as f:
                json.dump(meta_out, f, indent=2)
        except Exception:
            pass

    info_screen(stdscr, result_lines)
    maybe_eject(stdscr, dev)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def settings_menu(stdscr):
    while True:
        cfg = load_config()
        current = cfg.get("tmdb_api_key", "")
        masked = (current[:4] + "...") if current else "(not set)"
        choice = select_from_list(
            stdscr, "Settings",
            [f"Set TMDb API key (current: {masked})", "Clear TMDb API key", "Back"],
            ["Free key from https://www.themoviedb.org/settings/api", "", ""],
        )
        if choice is None or choice == 2:
            return
        if choice == 0:
            key = text_input(stdscr, "Enter TMDb API key (v3 auth):", current)
            cfg["tmdb_api_key"] = key.strip()
            save_config(cfg)
        elif choice == 1:
            cfg["tmdb_api_key"] = ""
            save_config(cfg)


# --------------------------------------------------------------------------
# Main menu / entry point
# --------------------------------------------------------------------------

def main_tui(stdscr):
    curses.curs_set(0)
    while True:
        choice = select_from_list(stdscr, "Main Menu", [
            "Rip PS1 / PS2 game disc",
            "Rip Music CD",
            "Rip Movie / TV DVD",
            "Settings (TMDb API key)",
            "Check dependencies",
            "Quit",
        ])
        if choice is None or choice == 5:
            return
        if choice == 0:
            flow_game(stdscr)
        elif choice == 1:
            flow_music(stdscr)
        elif choice == 2:
            flow_video(stdscr)
        elif choice == 3:
            settings_menu(stdscr)
        elif choice == 4:
            status = check_dependencies()
            lines = [f"{'OK ' if ok else 'MISSING'}  {t:10s} - {desc}" for (t, desc), ok in
                     zip(TOOL_INFO.items(), status.values())]
            info_screen(stdscr, lines)


def main():
    if "--check" in sys.argv:
        check_dependencies(print_report=True)
        return
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__)
        return
    curses.wrapper(main_tui)


if __name__ == "__main__":
    main()
