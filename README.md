# ImageCompress

Batch-compress JPG and PNG images to a target file size as **WebP** or **Progressive JPEG**. Images are processed on your server, not sent to third-party services.

[![Docker Hub](https://img.shields.io/docker/pulls/anderskeis/pycompressweb?style=flat-square)](https://hub.docker.com/r/anderskeis/pycompressweb)

![Upload and compression settings with Blog, Instagram and Facebook presets](assets/screenshot.png)

## Run

```bash
docker run -d --name pycompressweb \
  -p 127.0.0.1:5050:5050 anderskeis/pycompressweb:latest
```

Open **http://localhost:5050**.

To build from source:

```bash
git clone https://github.com/anderskeis/pycompressweb.git
cd pycompressweb
docker compose up -d --build
```

**Trusted local use only:** there is no authentication or rate limiting. Do not expose the app to the public internet. The included Compose file publishes port 5050 on all host interfaces; restrict access to your trusted network.

## Use

1. Drop, browse or paste JPG/PNG files (up to **500 MB per batch**).
2. Choose a preset or adjust the target size, format, resize limits, quality floor and metadata.
3. Click **Compress Images**, then download individual files or a ZIP.

**Shortcuts:** Ctrl/Cmd+V paste, Esc clear files, Ctrl/Cmd+Enter compress.

![Compression results with file sizes, dimensions, quality and download controls](assets/screenshot-results.png)

## Presets

| Preset | Output | Target | Resize limits | Min quality | Metadata |
|--------|--------|--------|---------------|-------------|----------|
| Blog | WebP | 250 KB | 2400 px long edge | 70 | Keep camera info |
| Instagram | Progressive JPEG | 10000 KB | 1080 px wide, 1440 px long edge | 95 | Strip all |
| Facebook | Progressive JPEG | 10000 KB | 2048 px long edge | 95 | Strip all |

Social presets are for **feed photographs**, not Stories, covers, profile pictures or ads. Their 10000 KB target is a generous compression budget, not a platform requirement.

Instagram examples: **1080 × 1080** square, **1080 × 1350** or **1080 × 1440** portrait, approximately **1080 × 566** landscape. Images retain their aspect ratio: there is **no cropping, padding or upscaling**. Platforms may still crop previews or recompress uploads.

Sizing references: [Instagram feed sizes](https://buffer.com/resources/instagram-image-size/) · [Facebook photo exports](https://photographylife.com/how-to-properly-resize-images-for-facebook).

## Compression settings

Default settings: **WebP, 200 KB, no resize limits, minimum quality 70, strip metadata**.

- **Target size:** 10–10000 KB in the UI. The compressor selects the highest quality up to 95 that fits; if the quality floor is still too large, it reduces dimensions. Unreachable targets are flagged.
- **Resize limits:** optional maximum long edge and width, applied after EXIF orientation. Never upscales or crops.
- **Format:** WebP preserves transparency; Progressive JPEG flattens it onto white.
- **Colour:** outputs carry an sRGB profile. Valid embedded profiles are converted; missing or unreadable profiles are treated as sRGB.
- **Metadata:** strip all or retain camera/lens/exposure information, date and copyright. GPS is always removed.

Expired sessions are cleaned during subsequent uploads after one hour. **New Session** removes the current session's files immediately. Set the `LOG_LEVEL` environment variable to `DEBUG`, `INFO` (default), `WARNING` or `ERROR` for server logging.

## API

`POST /upload` accepts multipart `files[]` and a positive `target_kb`, plus:

| Optional field | Values | Default |
|----------------|--------|---------|
| `output_format` | `webp`, `jpeg` | `webp` |
| `max_edge`, `max_width` | 0–20000 px; `0` disables the limit | `0` |
| `min_quality` | 10–95 | `70` |
| `exif_mode` | `strip`, `camera` | `strip` |

Invalid options return `400`. The response includes `session_id` and per-file results with sizes, dimensions, quality, colour profile, metadata handling and `target_met`.

`GET /download/<session_id>` downloads a ZIP; append `/<filename>` for one file. `POST /cleanup/<session_id>` deletes a session.

## Development

Flask, Pillow, vanilla HTML/CSS/JS; the Docker image uses Python 3.11 and Gunicorn.

```bash
pip install -r requirements.txt
python -m unittest discover -s tests
python app.py
```

## License

Do whatever you want :)
