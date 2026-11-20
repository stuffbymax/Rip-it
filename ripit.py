#!/usr/bin/env python3
"""
ripit.py — Terminal UI for ripping PS1/PS2 games, music CDs, and
movie/TV DVDs/Blu-rays on Linux, with optional metadata lookup and
video format conversion (MKV / MP4 / ISO).

Backends used (must be installed depending on what you rip):
  - ddrescue   : robust sector-by-sector reads -> .iso  (PS2, DVD/Blu-ray images, retries bad sectors)
  - cdrdao     : raw CD reads preserving audio tracks -> .bin/.cue (PS1 w/ CD audio)
  - cdparanoia : accurate digital audio extraction -> .wav (music CDs)
  - flac       : optional, compresses ripped audio tracks to FLAC
  - makemkvcon : MakeMKV CLI, rips DVD/Blu-ray titles to .mkv (handles encrypted discs)
  - ffmpeg     : converts to MP4/MKV; also rips the main title of UNPROTECTED discs
  - cd-info    : (libcdio-utils) optional, auto-detects track count / audio tracks
  - udevadm    : (usually preinstalled) detects disc labels / media presence / media type (CD/DVD/BD)
  - udisksctl  : optional, mounts the disc for the ffmpeg (unprotected) video route
  - eject      : (usually preinstalled) ejects the tray when done

Metadata lookups (need internet, no extra Python packages — uses urllib):
  - MusicBrainz (https://musicbrainz.org) for music CD track listings — no API key needed.
  - TMDb (https://www.themoviedb.org) for movie/TV titles — free API key required,
    set it from the Settings menu in the app.

Install:
  Debian/Ubuntu : sudo apt install gddrescue cdrdao cdparanoia flac libcdio-utils eject ffmpeg udisks2
  Fedora        : sudo dnf install ddrescue cdrdao cdparanoia flac libcdio-utils eject ffmpeg udisks2
  Arch          : sudo pacman -S ddrescue cdrdao cdparanoia flac libcdio eject ffmpeg udisks2
  MakeMKV       : not in most distro repos — get it from https://www.makemkv.com
                  (needs a license key after the trial period)

Run:
  python3 ripit.py           # normal TUI
  python3 ripit.py --check   # just check dependencies and exit

This tool only reads discs you insert — use it for backing up media you own.
Video output options:
  - ISO : raw sector image via ddrescue. Does NOT decrypt anything.
  - MKV / MP4 via MakeMKV : MakeMKV handles the disc's copy protection itself.
  - MKV / MP4 via ffmpeg  : unprotected/homemade discs only; ffmpeg cannot read
    encrypted commercial discs.
Circumvention rules vary by region, so check your local laws for video discs.
"""

import curses
import glob
import json
import os
import re
import shutil
import stat
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

CONFIG_DIR = os.path.expanduser("~/.config/ripit")
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
    "ddrescue": "ISO reads for PS2 / DVD / Blu-ray discs (retries bad sectors)",
    "cdrdao": "Raw CD reads preserving audio tracks (PS1 discs with music/FMV)",
    "cdparanoia": "Accurate digital audio extraction from music CDs",
    "flac": "Compresses ripped audio tracks to FLAC (optional)",
    "makemkvcon": "Rips DVD/Blu-ray titles to MKV (get MakeMKV from makemkv.com)",
    "ffmpeg": "Converts video to MP4/MKV; rips unprotected discs",
    "cd-info": "Auto-detects track count / audio tracks (optional)",
    "udevadm": "Detects disc label, media presence, and media type (optional)",
    "udisksctl": "Mounts the disc for the ffmpeg video route (optional)",
    "eject": "Ejects the tray automatically once ripping finishes (optional)",
    "toc2cue": "Converts cdrdao's .toc file into a standard .cue file (optional)",
}

INSTALL_HINTS = {
    "apt": "sudo apt install gddrescue cdrdao cdparanoia flac libcdio-utils eject ffmpeg udisks2",
    "dnf": "sudo dnf install ddrescue cdrdao cdparanoia flac libcdio-utils eject ffmpeg udisks2",
    "pacman": "sudo pacman -S ddrescue cdrdao cdparanoia flac libcdio eject ffmpeg udisks2",
    "makemkv": "download from https://www.makemkv.com (not in most repos)",
}


def which(tool):
    return shutil.which(tool) is not None


def check_dependencies(print_report=False):
    status = {t: which(t) for t in TOOL_INFO}
    if print_report:
        print("Tools:")
        for t, desc in TOOL_INFO.items():
            print(f"  [{'OK' if status[t] else 'MISSING'}] {t:11s} - {desc}")
        if not all(status.values()):
            print("\nInstall missing tools with one of:")
            for mgr, cmd in INSTALL_HINTS.items():
                print(f"  {mgr:8s}: {cmd}")
            print("\nYou only need the tools for the kinds of discs you rip.")
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

    # ID_CDROM_MEDIA_BD / _DVD / _CD are set by udev's cdrom_id helper.
    if props.get("ID_CDROM_MEDIA_BD") == "1":
        media_type = "BD"
    elif props.get("ID_CDROM_MEDIA_DVD") == "1":
        media_type = "DVD"
    elif props.get("ID_CDROM_MEDIA_CD") == "1":
        media_type = "CD"
    else:
        media_type = "unknown"

    return {"dev": dev, "has_media": has_media, "label": label, "media_type": media_type}


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

USER_AGENT = "ripit-tui/1.2 ( personal-use disc backup tool )"


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

def rip_iso(dev, out_path, cluster_sectors=None):
    """ddrescue reads the whole data track to a .iso, retrying bad sectors.

    cluster_sectors optionally sets ddrescue's -c (cluster) size, which
    controls how many 2048-byte sectors are read per request. Bumping this
    up helps throughput on large, mostly-healthy Blu-ray discs; leaving it
    at the ddrescue default is usually fine and is more careful about
    isolating bad sectors on scratched discs.
    """
    mapfile = out_path + ".ddrescue.log"
    cmd = ["ddrescue", "-b", "2048"]
    if cluster_sectors:
        cmd += ["-c", str(cluster_sectors)]
    cmd += ["-r3", "-v", dev, out_path, mapfile]
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


# ---- Video backends: MakeMKV, ffmpeg ---------------------------------------

VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts",
              ".mts", ".vob", ".mpg", ".mpeg", ".webm", ".flv", ".ogv")


def rip_mkv(dev, work_dir, min_seconds):
    """makemkvcon remuxes every title longer than min_seconds to .mkv.
    MakeMKV handles the disc's copy protection itself."""
    os.makedirs(work_dir, exist_ok=True)
    if not glob.glob("/dev/sg*"):
        print("WARNING: no /dev/sg* devices found. MakeMKV needs the sg module:")
        print("         sudo modprobe sg\n")
    cmd = ["makemkvcon", "mkv", f"dev:{dev}", "all", work_dir,
           f"--minlength={min_seconds}", "--progress=-same"]
    print(f"\n$ {' '.join(cmd)}\n")
    return subprocess.call(cmd)


def build_ffmpeg_cmd(src, dst, out_fmt, crf=18, preset="medium"):
    """MKV: lossless stream copy (video, all audio, all subtitles).
    MP4: H.264 + AAC transcode; first video stream + all audio, no subtitles
    (MP4 can't hold most disc subtitle formats)."""
    cmd = ["ffmpeg", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?"]
    if out_fmt == "mkv":
        cmd += ["-map", "0:s?", "-c", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
                "-movflags", "+faststart"]
    cmd += ["-max_muxing_queue_size", "1024", dst]
    return cmd


def convert_to_mp4(src, dst, crf=18):
    cmd = build_ffmpeg_cmd(src, dst, "mp4", crf=crf)
    print(f"\n$ {' '.join(cmd)}\n")
    return subprocess.call(cmd)


def find_mountpoint(dev):
    real = os.path.realpath(dev)
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and os.path.realpath(parts[0]) == real:
                    return parts[1].replace("\\040", " ")
    except OSError:
        pass
    return None


def ensure_mounted(dev):
    """Return the disc's mount point, mounting via udisksctl if needed."""
    mp = find_mountpoint(dev)
    if mp:
        return mp
    if which("udisksctl"):
        subprocess.run(["udisksctl", "mount", "-b", dev], capture_output=True, text=True)
        return find_mountpoint(dev)
    return None


def _find_child_ci(parent, name):
    """Case-insensitive lookup of a child entry (discs vary in case)."""
    try:
        for entry in os.listdir(parent):
            if entry.lower() == name.lower():
                return os.path.join(parent, entry)
    except OSError:
        pass
    return None


def find_main_video_source(mountpoint):
    """Locate the main title of a mounted, UNPROTECTED disc.
    Returns (description, ffmpeg_input) or (None, None).

    DVD: the VIDEO_TS title set with the most data, parts joined with the
         ffmpeg concat protocol.
    BD : the largest .m2ts in BDMV/STREAM (a decent guess at the main
         feature; discs with seamless-branching playlists may differ).
    """
    video_ts = _find_child_ci(mountpoint, "VIDEO_TS")
    if video_ts:
        sets = {}
        for f in os.listdir(video_ts):
            m = re.match(r"VTS_(\d\d)_(\d+)\.VOB$", f, re.IGNORECASE)
            if m and int(m.group(2)) >= 1:   # part 0 is the menu
                sets.setdefault(m.group(1), []).append(os.path.join(video_ts, f))
        if sets:
            best = max(sets.values(), key=lambda fl: sum(os.path.getsize(p) for p in fl))
            best.sort()
            return f"DVD title set ({len(best)} VOB file(s))", "concat:" + "|".join(best)

    bdmv = _find_child_ci(mountpoint, "BDMV")
    if bdmv:
        stream = _find_child_ci(bdmv, "STREAM")
        if stream:
            files = [os.path.join(stream, f) for f in os.listdir(stream) if f.lower().endswith(".m2ts")]
            if files:
                biggest = max(files, key=os.path.getsize)
                return f"Blu-ray stream {os.path.basename(biggest)}", biggest

    return None, None


def do_rip_video(stdscr, dev, out_fmt, engine, out_dir, name, is_tv,
                 min_seconds, crf, cluster_sectors=None):
    """Handles iso / mkv / mp4 for movie and TV discs.
    engine is 'makemkv' (handles encrypted discs, all titles) or
    'ffmpeg' (unprotected discs only, main title only)."""
    if out_fmt == "iso":
        return do_rip(stdscr, dev, "iso", out_dir, name, cluster_sectors=cluster_sectors)

    curses.endwin()
    print("=" * 70)
    print(f"Ripping {dev} -> {out_fmt.upper()} via {engine}")
    print("Do not remove the disc until this finishes.")
    print("=" * 70)

    start = time.time()
    lines = []
    rc = 1

    if engine == "makemkv":
        work_dir = os.path.join(out_dir, name)
        rc = rip_mkv(dev, work_dir, min_seconds)
        mkvs = sorted(glob.glob(os.path.join(work_dir, "*.mkv")))
        lines = [f"makemkvcon exit code: {rc}", f"{len(mkvs)} title(s) ripped"]
        if rc != 0 or not mkvs:
            lines += drive_failure_hints(dev)

        # A movie disc that yields exactly one title -> flatten to "Name.mkv"
        if not is_tv and len(mkvs) == 1:
            flat = os.path.join(out_dir, name + ".mkv")
            os.replace(mkvs[0], flat)
            try:
                os.rmdir(work_dir)
            except OSError:
                pass
            mkvs = [flat]

        if out_fmt == "mp4" and mkvs and rc == 0:
            for src in mkvs:
                dst = os.path.splitext(src)[0] + ".mp4"
                crc = convert_to_mp4(src, dst, crf=crf)
                if crc == 0 and os.path.exists(dst):
                    os.remove(src)          # keep only the MP4
                    lines.append(f"MP4: {dst}")
                else:
                    lines.append(f"ffmpeg failed on {src} (MKV kept)")
        else:
            lines += [f"MKV: {p}" for p in mkvs]
    else:
        print("Looking for the disc's mount point...")
        mp = ensure_mounted(dev)
        if not mp:
            lines = [f"Could not find or mount {dev}.",
                     "Mount the disc manually (or install udisks2) and try again."]
        else:
            desc, src = find_main_video_source(mp)
            if not src:
                lines = [f"No VIDEO_TS or BDMV folder found on {mp}.",
                         "The disc may be unreadable or not a video disc."]
            else:
                print(f"Using {desc} from {mp}")
                os.makedirs(out_dir, exist_ok=True)
                dst = os.path.join(out_dir, f"{name}.{out_fmt}")
                cmd = build_ffmpeg_cmd(src, dst, out_fmt, crf=crf)
                print(f"\n$ {' '.join(cmd)}\n")
                rc = subprocess.call(cmd)
                if rc == 0 and os.path.exists(dst):
                    lines = [f"ffmpeg finished ({desc}).", f"{out_fmt.upper()}: {dst}"]
                else:
                    if os.path.exists(dst):
                        os.remove(dst)
                    lines = [f"ffmpeg exit code: {rc}",
                             "If the disc is a commercial/encrypted one, ffmpeg can't read it —",
                             "use the MakeMKV engine instead."]

    print(f"\nDone in {time.time() - start:.0f}s.")
    print("\nPress Enter to return to the menu...")
    input()
    stdscr.refresh()
    return lines, rc


# --------------------------------------------------------------------------
# curses helpers
# --------------------------------------------------------------------------

# Theme colors use the standard 8-color curses palette, so they work in
# ordinary terminal emulators without requiring true-color support.
THEMES = {
    "Default":  {"fg": "white", "bg": "black", "accent": "cyan", "selected_fg": "black", "selected_bg": "cyan"},
    "Midnight": {"fg": "white", "bg": "blue",  "accent": "cyan", "selected_fg": "black", "selected_bg": "cyan"},
    "Forest":   {"fg": "white", "bg": "black", "accent": "green", "selected_fg": "black", "selected_bg": "green"},
    "Amber":    {"fg": "yellow", "bg": "black", "accent": "yellow", "selected_fg": "black", "selected_bg": "yellow"},
    "Light":    {"fg": "black", "bg": "white", "accent": "blue", "selected_fg": "black", "selected_bg": "cyan"},
    "Plum":     {"fg": "white", "bg": "black", "accent": "magenta", "selected_fg": "white", "selected_bg": "magenta"},
}

COLOR_NAMES = {
    "black": curses.COLOR_BLACK, "red": curses.COLOR_RED,
    "green": curses.COLOR_GREEN, "yellow": curses.COLOR_YELLOW,
    "blue": curses.COLOR_BLUE, "magenta": curses.COLOR_MAGENTA,
    "cyan": curses.COLOR_CYAN, "white": curses.COLOR_WHITE,
}

# Pair IDs shared by all screens.
PAIR_NORMAL, PAIR_TITLE, PAIR_MUTED, PAIR_SELECTED, PAIR_ACCENT = range(1, 6)


def init_theme(stdscr):
    """Load the configured palette and apply its background to the screen."""
    cfg = load_config()
    theme_name = cfg.get("theme", "Default")
    palette = THEMES.get(theme_name, THEMES["Default"])
    if not curses.has_colors():
        stdscr.bkgd(" ", curses.A_NORMAL)
        return
    try:
        curses.start_color()
        try:
            curses.use_default_colors()
            default_bg = -1
        except curses.error:
            default_bg = COLOR_NAMES[palette["bg"]]
        bg = default_bg if palette["bg"] == "black" and default_bg == -1 else COLOR_NAMES[palette["bg"]]
        curses.init_pair(PAIR_NORMAL, COLOR_NAMES[palette["fg"]], bg)
        curses.init_pair(PAIR_TITLE, COLOR_NAMES[palette["accent"]], bg)
        curses.init_pair(PAIR_MUTED, COLOR_NAMES[palette["fg"]], bg)
        curses.init_pair(PAIR_SELECTED, COLOR_NAMES[palette["selected_fg"]], COLOR_NAMES[palette["selected_bg"]])
        curses.init_pair(PAIR_ACCENT, COLOR_NAMES[palette["accent"]], bg)
        stdscr.bkgd(" ", curses.color_pair(PAIR_NORMAL))
    except curses.error:
        # Some terminals advertise colors but reject color-pair changes.
        pass


def color_attr(pair, fallback=curses.A_NORMAL):
    try:
        return curses.color_pair(pair)
    except curses.error:
        return fallback


def draw_banner(stdscr, subtitle=""):
    stdscr.erase()
    stdscr.bkgd(" ", color_attr(PAIR_NORMAL))
    h, w = stdscr.getmaxyx()
    title = "DISC RIPPER — Games / Music / Movies / TV"
    stdscr.addstr(0, max(0, (w - len(title)) // 2), title, color_attr(PAIR_TITLE, curses.A_BOLD) | curses.A_BOLD)
    if subtitle:
        stdscr.addstr(1, max(0, (w - len(subtitle)) // 2), subtitle, color_attr(PAIR_MUTED, curses.A_DIM) | curses.A_DIM)
    stdscr.addstr(2, 0, "-" * max(0, w - 1), color_attr(PAIR_ACCENT))


def select_from_list(stdscr, title, options, descriptions=None):
    idx = 0
    curses.curs_set(0)
    while True:
        draw_banner(stdscr, title)
        h, w = stdscr.getmaxyx()
        start_y = 4
        for i, opt in enumerate(options):
            marker = "> " if i == idx else "  "
            attr = color_attr(PAIR_SELECTED, curses.A_REVERSE) if i == idx else color_attr(PAIR_NORMAL)
            row = start_y + i * 2
            if row >= h - 3:
                break
            stdscr.addstr(row, 2, f"{marker}{opt}"[: w - 3], attr)
            if descriptions and i < len(descriptions) and descriptions[i] and row + 1 < h - 3:
                for j, wrapped in enumerate(textwrap.wrap(descriptions[i], max(w - 8, 10))):
                    stdscr.addstr(row + 1, 6, wrapped, color_attr(PAIR_MUTED, curses.A_DIM) | curses.A_DIM)
                    break
        stdscr.addstr(h - 2, 2, "Up/Down to move, Enter to select, q to cancel", color_attr(PAIR_MUTED, curses.A_DIM) | curses.A_DIM)
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
    stdscr.addstr(4, 2, prompt[: w - 3])
    stdscr.addstr(6, 2, f"(default: {default})"[: w - 3] if default else "", curses.A_DIM)
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

def _choose_drive_raw(stdscr):
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
        if info["has_media"]:
            kind = info["media_type"] if info["media_type"] != "unknown" else "disc"
            status = f"{kind} detected"
        else:
            status = "empty / unknown"
        label = f" \"{info['label']}\"" if info["label"] else ""
        options.append(f"{d}{label} — {status}")
    options.append("Enter device path manually")

    choice = select_from_list(stdscr, "Select optical drive", options)
    if choice is None:
        return None
    if choice == len(options) - 1:
        return text_input(stdscr, "Enter device path (e.g. /dev/sr0):") or None
    return drives[choice]


def validate_device(dev):
    """Normalise and sanity-check a device path.
    Returns (device, problem_lines). problem_lines is empty when OK."""
    dev = (dev or "").strip()
    if not dev:
        return None, ["No device given."]
    # Forgive common typos: 'sr0', '/dev/src0', '/dev/sr 0'
    dev = dev.replace(" ", "")
    if not dev.startswith("/"):
        dev = "/dev/" + dev
    dev = re.sub(r"^/dev/src(\d+)$", r"/dev/sr\1", dev)

    if not os.path.exists(dev):
        found = list_optical_drives()
        lines = [f"Unknown device: {dev}",
                 "Optical drives are named /dev/sr0, /dev/sr1, ... (sr, not src)."]
        lines.append("Detected drives: " + (", ".join(found) if found else "none"))
        if not found:
            lines += ["", "No drive visible to Linux. Check the cable / USB connection,",
                      "and run 'lsblk' or 'dmesg | tail' to see if it was detected."]
        return None, lines
    try:
        if not stat.S_ISBLK(os.stat(dev).st_mode):
            return None, [f"{dev} is not a block device."]
    except OSError as e:
        return None, [f"Cannot stat {dev}: {e}"]
    if not os.access(dev, os.R_OK):
        return None, [f"No read permission on {dev}.",
                      "Add yourself to the cdrom group, then log out and back in:",
                      "  sudo usermod -aG cdrom $USER",
                      "(or test by running this script with sudo)."]
    return dev, []


def choose_drive(stdscr):
    dev = _choose_drive_raw(stdscr)
    if not dev:
        return None
    dev, problems = validate_device(dev)
    if problems:
        info_screen(stdscr, problems)
        return None
    return dev


def drive_failure_hints(dev):
    """Explain the usual causes of MakeMKV's 'Failed to open disk'."""
    return [
        "",
        "'Failed to open disk' usually means one of:",
        "1. The sg kernel module isn't loaded (MakeMKV needs it):",
        "     sudo modprobe sg",
        "   Make it permanent: echo sg | sudo tee /etc/modules-load.d/sg.conf",
        "2. No permission on the drive: sudo usermod -aG cdrom $USER",
        "   (then log out/in). Quick test: run this script with sudo.",
        f"3. Another program has the disc open (file manager auto-mount, VLC):",
        f"     sudo umount {dev}   and close that program.",
        "4. MakeMKV is out of date or its key/beta key expired — update from",
        "   makemkv.com (new discs often need the latest version).",
        "5. The disc is dirty/scratched or the drive can't read this format.",
    ]


def choose_destination(stdscr, default_name, default_dir="~/Games"):
    out_dir = text_input(stdscr, "Save to directory:", os.path.expanduser(default_dir))
    out_dir = os.path.expanduser(out_dir)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        info_screen(stdscr, [f"Could not create directory: {e}"])
        return None, None
    name = text_input(stdscr, "Base filename (no extension):", default_name)
    name = sanitize_filename(name) or default_name
    return out_dir, name


def do_rip(stdscr, dev, fmt, out_dir, name, cluster_sectors=None):
    """fmt is 'iso' (ddrescue) or 'binclue' (cdrdao). Used by games and video."""
    out_base = os.path.join(out_dir, name)
    curses.endwin()

    print("=" * 70)
    print(f"Ripping {dev} -> {out_base}.{'iso' if fmt == 'iso' else 'bin/cue'}")
    print("Do not remove the disc until this finishes.")
    print("=" * 70)

    start = time.time()
    if fmt == "iso":
        rc, mapfile = rip_iso(dev, out_base + ".iso", cluster_sectors=cluster_sectors)
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
        releases, err = run_with_spinner(
            stdscr, "Searching MusicBrainz...", musicbrainz_search_release, query
        ) or ([], "Search failed")
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
                ) or (None, "Lookup failed")
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

    out_dir, name = choose_destination(stdscr, album_dir_name, default_dir="~/Music")
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
# Flow: Movie / TV disc (DVD or Blu-ray)
# --------------------------------------------------------------------------

def choose_disc_format(stdscr, dev):
    """Ask (or auto-detect via udev) whether the inserted disc is a DVD or
    Blu-ray. Returns 'DVD' or 'BD', or None if the user cancelled."""
    info = probe_drive(dev)
    detected = info.get("media_type", "unknown")

    if detected in ("DVD", "BD"):
        options = [f"Auto-detected: {detected}", "DVD", "Blu-ray"]
    else:
        options = ["DVD", "Blu-ray"]

    choice = select_from_list(stdscr, "Disc format", options)
    if choice is None:
        return None
    if detected in ("DVD", "BD"):
        if choice == 0:
            return detected
        return "DVD" if choice == 1 else "BD"
    return "DVD" if choice == 0 else "BD"


def choose_mp4_quality(stdscr):
    """Returns an x264 CRF value (lower = better quality / bigger file), or None."""
    choice = select_from_list(
        stdscr, "MP4 quality",
        ["High (CRF 18) — near-transparent, large files",
         "Balanced (CRF 21) — good quality, moderate size",
         "Small (CRF 24) — noticeably smaller, some quality loss"],
    )
    if choice is None:
        return None
    return [18, 21, 24][choice]


def flow_video(stdscr):
    dev = choose_drive(stdscr)
    if not dev:
        return

    disc_format = choose_disc_format(stdscr, dev)
    if disc_format is None:
        return

    # ---- Output format -------------------------------------------------
    fmt_choice = select_from_list(
        stdscr, "Output format",
        ["MKV (lossless remux, recommended)",
         "MP4 (H.264/AAC transcode, widest device support)",
         "ISO (raw disc image)"],
        ["Keeps all audio/subtitle tracks",
         "Slow; drops subtitles; plays on almost anything",
         "Sector copy via ddrescue; no decryption"],
    )
    if fmt_choice is None:
        return
    out_fmt = ["mkv", "mp4", "iso"][fmt_choice]

    # ---- Engine (MKV/MP4 only) ----------------------------------------
    engine = None
    if out_fmt != "iso":
        eng_choice = select_from_list(
            stdscr, "Ripping engine",
            ["MakeMKV (handles encrypted discs, rips all titles)",
             "ffmpeg (unprotected discs only, main title only)"],
            ["Needs makemkvcon installed", "Needs ffmpeg; disc must be readable/mounted"],
        )
        if eng_choice is None:
            return
        engine = "makemkv" if eng_choice == 0 else "ffmpeg"

    # ---- Tool checks ---------------------------------------------------
    needs = []
    if out_fmt == "iso":
        needs = [("ddrescue", "ISO rips")]
    elif engine == "makemkv":
        needs = [("makemkvcon", "MKV rips")]
        if out_fmt == "mp4":
            needs.append(("ffmpeg", "MP4 conversion"))
    else:
        needs = [("ffmpeg", "ffmpeg disc rips")]
    for tool, why in needs:
        if not require_tool(stdscr, tool, why):
            return

    # ---- Notes ---------------------------------------------------------
    if out_fmt == "iso" and disc_format == "BD":
        info_screen(stdscr, [
            "Blu-ray note: this tool images raw sectors with ddrescue, the",
            "same as it does for DVDs — it does NOT decrypt AACS or BD+.",
            "An encrypted commercial Blu-ray's .iso will likely need",
            "separate decryption software (e.g. MakeMKV) before it's",
            "playable; unencrypted/homemade discs will just work.",
            "",
            "Blu-ray discs are large (25-100+ GB); make sure the",
            "destination has enough free space and the rip may take a while.",
        ])
    elif engine == "ffmpeg":
        info_screen(stdscr, [
            "ffmpeg route: works only on unprotected / homemade discs.",
            "It rips ONE title — the largest on the disc (usually the main",
            "feature). Encrypted commercial discs will fail; use MakeMKV",
            "for those. TV discs with several episodes should use MakeMKV",
            "so every episode is ripped.",
        ])

    kind = select_from_list(stdscr, "What kind of disc?", ["Movie", "TV Series"])
    if kind is None:
        return
    is_tv = kind == 1

    # ---- TMDb lookup ---------------------------------------------------
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
            ) or ([], "Search failed")
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

    default_name = title or ("bluray" if disc_format == "BD" else "video")
    if title and year:
        default_name = f"{title} ({year})"
    if is_tv and season:
        default_name += f" - Season {season.zfill(2)}"

    out_dir, name = choose_destination(
        stdscr, sanitize_filename(default_name),
        default_dir="~/Videos",
    )
    if not out_dir:
        return

    # ---- Extra options -------------------------------------------------
    min_seconds = 600
    if engine == "makemkv":
        mins = text_input(stdscr, "Ignore titles shorter than (minutes):", "10" if is_tv else "20")
        min_seconds = int(mins) * 60 if mins.isdigit() else 600

    crf = 18
    if out_fmt == "mp4":
        crf = choose_mp4_quality(stdscr)
        if crf is None:
            return

    # Blu-ray ISOs are much larger than DVDs; a bigger ddrescue cluster
    # size speeds up the initial pass on healthy discs. DVDs keep the
    # ddrescue default (finer-grained, better for isolating bad sectors).
    cluster_sectors = 64 if disc_format == "BD" else None

    base = os.path.join(out_dir, name)
    if out_fmt == "iso":
        output_desc = f"{base}.iso"
    elif engine == "makemkv" and is_tv:
        output_desc = f"{base}/  (folder of episodes, .{out_fmt})"
    else:
        output_desc = f"{base}.{out_fmt}"

    engine_desc = {None: "ddrescue", "makemkv": "MakeMKV", "ffmpeg": "ffmpeg"}[engine]
    summary = [
        f"Drive:    {dev}",
        f"Disc:     {'Blu-ray' if disc_format == 'BD' else 'DVD'}",
        f"Type:     {'TV Series' if is_tv else 'Movie'}",
        f"Title:    {title or '(not looked up)'}",
        f"Format:   {out_fmt.upper()} via {engine_desc}" + (f" (CRF {crf})" if out_fmt == "mp4" else ""),
        f"Output:   {output_desc}",
        "",
        "Press any key to start ripping, or q to cancel.",
    ]
    draw_banner(stdscr, "Confirm")
    for i, line in enumerate(summary):
        stdscr.addstr(4 + i, 2, line[: curses.COLS - 3] if hasattr(curses, "COLS") else line)
    stdscr.refresh()
    if stdscr.getch() == ord("q"):
        return

    result_lines, rc = do_rip_video(
        stdscr, dev, out_fmt, engine, out_dir, name, is_tv,
        min_seconds, crf, cluster_sectors=cluster_sectors,
    )

    if title:
        try:
            meta_out = {
                "title": title,
                "year": year,
                "season": season or None,
                "disc_format": disc_format,
                "output_format": out_fmt,
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
# Flow: Convert an existing video file (or folder of them) to MKV / MP4
# --------------------------------------------------------------------------

def collect_video_files(path):
    """A file -> [file]; a folder -> every video file directly inside it."""
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        files = [os.path.join(path, f) for f in sorted(os.listdir(path))
                 if f.lower().endswith(VIDEO_EXTS)]
        return [f for f in files if os.path.isfile(f)]
    return []


def flow_convert(stdscr):
    if not require_tool(stdscr, "ffmpeg", "video conversion"):
        return

    path = text_input(stdscr, "Path to a video file or a folder of videos:")
    if not path:
        return
    files = collect_video_files(path)
    if not files:
        info_screen(stdscr, [f"No video files found at: {path}"])
        return

    target_choice = select_from_list(
        stdscr, "Convert to",
        ["MKV (lossless remux — fast, no quality loss)",
         "MP4 (H.264/AAC transcode — slow, plays everywhere)"],
        ["Copies streams as-is into a Matroska container; keeps subtitles",
         "Re-encodes video and audio; drops subtitles"],
    )
    if target_choice is None:
        return
    target = "mkv" if target_choice == 0 else "mp4"

    crf = 18
    if target == "mp4":
        crf = choose_mp4_quality(stdscr)
        if crf is None:
            return

    out_dir_in = text_input(stdscr, "Output directory (blank = same folder as each source):")
    out_dir = os.path.expanduser(out_dir_in) if out_dir_in else None
    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            info_screen(stdscr, [f"Could not create directory: {e}"])
            return

    keep = select_from_list(stdscr, "Original files", ["Keep originals", "Delete originals after a successful conversion"])
    if keep is None:
        return
    delete_originals = keep == 1

    # Work out the job list, skipping anything that would overwrite its own source.
    jobs = []
    skipped = []
    for src in files:
        dst_dir = out_dir or os.path.dirname(os.path.abspath(src))
        dst = os.path.join(dst_dir, os.path.splitext(os.path.basename(src))[0] + "." + target)
        if os.path.abspath(dst) == os.path.abspath(src):
            skipped.append(os.path.basename(src))
        else:
            jobs.append((src, dst))

    if not jobs:
        info_screen(stdscr, ["Nothing to do — every file is already ." + target + " in that location."])
        return

    summary = [
        f"Files:    {len(jobs)} to convert" + (f" ({len(skipped)} skipped, already .{target})" if skipped else ""),
        f"Target:   {target.upper()}" + (f" (CRF {crf})" if target == "mp4" else ""),
        f"Output:   {out_dir or '(next to each source)'}",
        f"Originals: {'DELETED after success' if delete_originals else 'kept'}",
        "",
        "Press any key to start, or q to cancel.",
    ]
    draw_banner(stdscr, "Confirm")
    for i, line in enumerate(summary):
        stdscr.addstr(4 + i, 2, line)
    stdscr.refresh()
    if stdscr.getch() == ord("q"):
        return

    curses.endwin()
    print("=" * 70)
    print(f"Converting {len(jobs)} file(s) to {target.upper()}")
    print("=" * 70)

    start = time.time()
    ok, failed = [], []
    for n, (src, dst) in enumerate(jobs, 1):
        print(f"\n=== [{n}/{len(jobs)}] {os.path.basename(src)} ===")
        if os.path.exists(dst):
            print(f"Skipping: {dst} already exists")
            failed.append(f"{os.path.basename(src)} (output already exists)")
            continue
        cmd = build_ffmpeg_cmd(src, dst, target, crf=crf)
        print(f"$ {' '.join(cmd)}\n")
        rc = subprocess.call(cmd)
        if rc == 0 and os.path.exists(dst):
            ok.append(dst)
            if delete_originals:
                try:
                    os.remove(src)
                except OSError:
                    pass
        else:
            failed.append(os.path.basename(src))
            if os.path.exists(dst):
                os.remove(dst)   # don't leave half-written files behind

    print(f"\nDone in {time.time() - start:.0f}s. {len(ok)} converted, {len(failed)} failed.")
    print("\nPress Enter to return to the menu...")
    input()
    stdscr.refresh()

    lines = [f"Converted {len(ok)}/{len(jobs)} file(s) to {target.upper()}."]
    if failed:
        lines.append("Failed: " + ", ".join(failed))
        if target == "mkv":
            lines.append("Tip: some stream types can't be copied into MKV — try MP4 for those.")
    info_screen(stdscr, lines)


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
            [f"Set TMDb API key (current: {masked})", "Clear TMDb API key", f"Color theme (current: {cfg.get('theme', 'Default')})", "Back"],
            ["Free key from https://www.themoviedb.org/settings/api", "", "Choose background and accent colors", ""],
        )
        if choice is None or choice == 3:
            return
        if choice == 0:
            key = text_input(stdscr, "Enter TMDb API key (v3 auth):", current)
            cfg["tmdb_api_key"] = key.strip()
            save_config(cfg)
        elif choice == 1:
            cfg["tmdb_api_key"] = ""
            save_config(cfg)
        elif choice == 2:
            names = list(THEMES)
            selected = select_from_list(
                stdscr, "Color theme", names,
                ["Classic black background with cyan accents", "Deep blue background", "Green accents on black", "Amber/yellow accents on black", "Light background with blue accents", "Purple accents on black"],
            )
            if selected is not None:
                cfg["theme"] = names[selected]
                save_config(cfg)
                init_theme(stdscr)


# --------------------------------------------------------------------------
# Main menu / entry point
# --------------------------------------------------------------------------

def main_tui(stdscr):
    curses.curs_set(0)
    init_theme(stdscr)
    while True:
        choice = select_from_list(stdscr, "Main Menu", [
            "Rip PS1 / PS2 game disc",
            "Rip Music CD",
            "Rip Movie / TV disc (DVD/Blu-ray -> MKV / MP4 / ISO)",
            "Convert existing video file(s) to MKV / MP4",
            "Settings (TMDb API key, theme)",
            "Check dependencies",
            "Quit",
        ])
        if choice is None or choice == 6:
            return
        if choice == 0:
            flow_game(stdscr)
        elif choice == 1:
            flow_music(stdscr)
        elif choice == 2:
            flow_video(stdscr)
        elif choice == 3:
            flow_convert(stdscr)
        elif choice == 4:
            settings_menu(stdscr)
        elif choice == 5:
            status = check_dependencies()
            lines = [f"{'OK ' if ok else 'MISSING'}  {t:11s} - {desc}" for (t, desc), ok in
                     zip(TOOL_INFO.items(), status.values())]
            lines += ["", "You only need the tools for the kinds of discs you rip."]
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