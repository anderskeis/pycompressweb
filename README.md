# PyCompressWeb

A local web application for batch compressing JPG images to a target file size while maintaining optimal quality.

## Features

- 🖼️ **Batch Upload**: Drag & drop or select multiple JPG images at once
- 🎯 **Target Size**: Specify your desired output file size in KB
- ⚡ **Smart Compression**: Automatically balances quality and resolution to achieve target size
- 📊 **Detailed Results**: See compression stats for each image
- 📦 **ZIP Download**: Download all compressed images in a single ZIP file
- 🐳 **Docker Ready**: Easy deployment with Docker Compose

## How It Works

The compression algorithm:
1. First tries to achieve target size by adjusting JPEG quality (binary search from 10-95)
2. If quality adjustment alone isn't enough, progressively reduces resolution (90%, 80%, 70%... down to 10%)
3. At each resolution, finds the highest quality setting that meets the size target
4. Result: Best possible visual quality under your KB limit

## Quick Start

### Using Docker Compose (Recommended)

```bash
# Build and start the container
docker-compose up -d

# Access the web interface
open http://localhost:5050
```

### Using Docker directly

```bash
# Build the image
docker build -t pycompressweb .

# Run the container
docker run -p 5050:5050 pycompressweb
```

### Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the app
python app.py
```

## Usage

1. Open http://localhost:5050 in your browser
2. Drag & drop your JPG images onto the upload zone (or click to select)
3. Set your target file size in KB (default: 200 KB)
4. Click "Compress Images"
5. Review the results showing original vs compressed sizes
6. Click "Download ZIP" to get all optimized images

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| Port | 5050 | Web server port |
| Max Upload | 100 MB | Maximum total upload size |
| Session Cleanup | 1 hour | Automatic deletion of temporary files |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Main web interface |
| `/upload` | POST | Upload and compress images |
| `/download/<session_id>` | GET | Download compressed images as ZIP |
| `/cleanup/<session_id>` | POST | Manually cleanup session files |

## Tech Stack

- **Backend**: Python, Flask, Pillow
- **Frontend**: Vanilla HTML/CSS/JavaScript
- **Production Server**: Gunicorn
- **Container**: Docker

## License

MIT
