![](https://media.tenor.com/njVGQRUXoVAAAAAC/idk.gif)


# its made by ai BTW
# GameRip TUI

A terminal-based disc ripping utility for Linux that can back up:

- 🎮 PlayStation 1 games
- 🎮 PlayStation 2 games
- 🎵 Music CDs
- 🎬 Movie DVDs
- 📺 TV series DVDs

GameRip provides a simple curses-based interface while using proven Linux command-line tools for the actual ripping process. :contentReference[oaicite:0]{index=0}

## Features

- Interactive terminal UI
- Automatic optical drive detection
- Disc label detection
- Dependency checker
- PS1 ripping to BIN/CUE (preserves CD audio)
- PS2 ripping to ISO
- Music CD ripping to WAV or FLAC
- MusicBrainz metadata lookup
- TMDb lookup for movies and TV shows
- Optional automatic disc eject
- Stores TMDb API key securely in `~/.config/gamerip-tui`

## Supported Formats

| Media | Output |
|-------|--------|
| PlayStation 1 | BIN/CUE |
| PlayStation 2 | ISO |
| Music CD | WAV or FLAC |
| Movie DVD | ISO |
| TV DVD | ISO |

## Dependencies

Required depending on what you want to rip:

| Tool | Purpose |
|------|---------|
| ddrescue | ISO disc imaging |
| cdrdao | Raw BIN/CUE ripping |
| cdparanoia | Audio CD extraction |
| flac | Optional FLAC encoding |
| cd-info | Disc detection |
| udevadm | Drive/media detection |
| eject | Automatic eject |
| toc2cue | Convert TOC to CUE |

### Arch Linux

```sh
sudo pacman -S ddrescue cdrdao cdparanoia flac libcdio eject
```

### Debian / Ubuntu

```bash
sudo apt install gddrescue cdrdao cdparanoia flac libcdio-utils eject
```

### Fedora

```bash
sudo dnf install ddrescue cdrdao cdparanoia flac libcdio-utils eject
```

## Installation

Clone the repository:

```bash
git clone https://github.com/yourusername/gamerip-tui.git
cd gamerip-tui
```

Make the script executable:

```bash
chmod +x ripit.py
```

Run:

```bash
./ripit.py
```

or

```bash
python3 ripit.py
```

## Checking Dependencies

```bash
python3 ripit.py --check
```

## Metadata

### Music CDs

Uses **MusicBrainz** for:

- Album titles
- Artist names
- Track names

No API key is required.

### Movies & TV

Uses **TMDb**.

You'll need a free TMDb API key, which can be entered from the **Settings** menu.

## Output

### PS1

```
Game Name.bin
Game Name.cue
```

### PS2

```
Game Name.iso
```

### Music

```
Artist - Album/
├── 01 - Track.flac
├── 02 - Track.flac
└── metadata.json
```

### Movies

```
Movie Title (2024).iso
Movie Title (2024).metadata.json
```

## Notes

- PS1 games containing CD audio should be ripped as **BIN/CUE**.
- PS2 DVDs are normally best stored as **ISO**.
- DVDs are imaged sector-for-sector only; CSS-encrypted discs are **not decrypted**.
- Use this software only for media you own and where local laws permit creating backups.

<img width="1082" height="343" alt="image" src="https://github.com/user-attachments/assets/f58640de-9ab6-44ad-a8d2-18b06dd0a918" />
<img width="930" height="419" alt="image" src="https://github.com/user-attachments/assets/a8166bb1-4362-4969-957c-f2eb1ce39a85" />

<img width="1095" height="236" alt="image" src="https://github.com/user-attachments/assets/d90e3e25-185e-4b13-9ff0-9b2aa436511b" />
<img width="1083" height="203" alt="image" src="https://github.com/user-attachments/assets/6f0990d1-0dc5-480b-a5c4-3cd127157d49" />

