# ImageCompress

Batch-compress JPG and PNG images to a target file size in your browser, as **WebP** or **Progressive JPEG**. No upload to third parties — everything runs locally.

[![Docker Hub](https://img.shields.io/docker/pulls/anderskeis/pycompressweb?style=flat-square)](https://hub.docker.com/r/anderskeis/pycompressweb)

![ImageCompress UI](assets/screenshot.png)

## Quick Start

```bash
# Run with Docker (recommended)
docker run -d -p 5050:5050 anderskeis/pycompressweb:latest
```

Open **http://localhost:5050** and start compressing.

### Other options

```bash
# Docker Compose (from source)
git clone https://github.com/anderskeis/pycompressweb.git
cd pycompressweb && docker compose up -d

# Local development (no Docker)
pip install -r requirements.txt && python app.py
```

## How to Use

1. **Drop images** onto the upload zone (or click to browse, or paste with Ctrl+V)
2. **Pick a profile**: click **Blog preset** for web publishing, or set the options yourself
3. **Set target size** in KB — use presets (50/100/200/500 KB, 1 MB) or enter custom
4. **Pick output format**: WebP (default) or Progressive JPEG
5. Optionally set **Max long edge**, **Min quality**, and **Metadata** (see [Options](#options))
6. Click **Compress Images** (or press Ctrl+Enter)
7. **Download** — get all files as ZIP, or download individual images
8. Click **New Session** to start fresh

**Shortcuts**: Ctrl+V paste · Esc clear files · Ctrl+Enter compress

![Compression Results](assets/screenshot-results.png)

## Options

| Option | Values | Default | What it does |
|--------|--------|---------|--------------|
| Target size | 10–10000 KB | 200 | Output never exceeds this unless impossible (then the result is flagged) |
| Output format | WebP, Progressive JPEG | WebP | WebP is smaller and keeps transparency. JPEG is always progressive and flattens transparency onto white |
| Max long edge | Off, 1600, 2048, 2400, 3000, 4000 px | Off | Resizes the longest side before compressing. Never upscales |
| Min quality | 10–95 | 70 | Quality floor. If the target can't be met above it, the image is **shrunk** instead of degraded |
| Metadata | Strip all, Keep camera info | Strip all | *Camera info* keeps make, model, lens, exposure, ISO, focal length, date and copyright. **GPS is always removed.** Orientation is applied to the pixels, never stored |

**Colour:** every output is converted to **sRGB** (from the embedded Display P3, Adobe RGB, etc.) and carries an sRGB profile, so colours look the same in every browser. Images without a profile are assumed to be sRGB.

### Blog preset

One click sets: **WebP · 250 KB · max 2400 px · min quality 70 · keep camera info (GPS removed)**. The button highlights only while all settings still match it.

### How compression works

1. Apply EXIF orientation, then resize to *Max long edge* (if set)
2. Convert to sRGB; flatten transparency if the output is JPEG
3. Binary-search the highest quality in `[Min quality, 95]` that fits the target
4. If even *Min quality* is too big, binary-search the largest size that fits at *Min quality*

The old behaviour (resolution fixed, quality driven down to 25) produced heavy artifacts on large photos. Shrinking first gives sharper images at the same file size. For the previous behaviour, set Max long edge to *Off* and Min quality to *10*.

### API

`POST /upload` (multipart form): `files[]`, `target_kb`, and optionally `output_format` (`webp`|`jpeg`), `max_edge` (px, `0` = off), `min_quality` (10–95), `exif_mode` (`strip`|`camera`). Invalid values return `400`. Each result reports `final_resolution`, `quality_used`, `output_format`, `color_profile`, `metadata`, and `target_met`.

## Features

- Batch upload: drag & drop, file picker, or clipboard paste
- Image thumbnails with total size indicator (500 MB limit)
- Output as WebP or Progressive JPEG, with a one-click Blog preset
- Smart compression: quality search with a configurable floor, then resolution scaling
- sRGB conversion with embedded profile; optional camera metadata, GPS always stripped
- Warns when a target can't be reached at the chosen minimum quality
- EXIF orientation preserved — rotated photos stay upright
- Per-file results: original vs compressed size, resolution, quality, reduction %
- Download all as ZIP or individual files
- Auto-cleanup of temporary files after 1 hour

## ⚠️ Security Warning

**LOCAL USE ONLY.** No authentication, no rate limiting. Do not expose to the public internet.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| Port | 5050 | Web server port |
| Max Upload | 500 MB | Maximum total upload size |
| Min Quality | 70% | Default quality floor (configurable in the UI, 10–95) |
| Max Quality | 95% | Maximum quality |
| Session Cleanup | 1 hour | Auto-delete temporary files |
| LOG_LEVEL | INFO | DEBUG, INFO, WARNING, or ERROR |

Set `LOG_LEVEL` via environment variable: `docker run -e LOG_LEVEL=DEBUG -p 5050:5050 anderskeis/pycompressweb:latest`

## Development

```bash
pip install -r requirements.txt
python -m unittest discover -s tests
```

## Upgrading

> [!NOTE]
> **Breaking change:** the *Keep Original*, *Convert to JPG* and *Convert to PNG* formats were removed. `output_format` now accepts only `webp` or `jpeg`, and the old values return `400`. PNG output is no longer produced. Transparent images should use WebP.

## Tech Stack

Python 3.11 · Flask · Pillow 11 · Gunicorn · Vanilla HTML/CSS/JS · Docker (amd64/arm64)

## License

Do whatever you want :)
