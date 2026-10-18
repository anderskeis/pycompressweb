"""
Keis ImageCompress - Batch Image Compression Web Application
Compresses JPG and PNG images to a target KB size with optimal quality/resolution balance.
"""

import os
import io
import re
import uuid
import logging
import zipfile
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
from PIL import Image, ImageCms, ImageOps

# Configure logging
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Security: UUID validation pattern to prevent path traversal
UUID_PATTERN = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$')


def is_valid_session_id(session_id):
    """Validate session_id is a proper UUID to prevent path traversal attacks."""
    return bool(session_id and UUID_PATTERN.match(session_id))

# Configuration
UPLOAD_FOLDER = '/tmp/pycompressweb/uploads'
OUTPUT_FOLDER = '/tmp/pycompressweb/output'
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png'}
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500MB max total upload to handle multiple large files
MAX_WORKERS_CAP = 8  # Cap thread pool to avoid thread thrashing on high-core machines

# Output formats: form value -> (Pillow format, file extension)
OUTPUT_FORMATS = {
    'webp': ('WEBP', '.webp'),
    'jpeg': ('JPEG', '.jpg'),  # always progressive
}
DEFAULT_OUTPUT_FORMAT = 'webp'

# Quality search bounds
MAX_QUALITY = 95
DEFAULT_MIN_QUALITY = 70
MIN_QUALITY_LOWER_BOUND = 10  # lowest floor a user may choose

# Max long edge (0 = off)
MAX_EDGE_LIMIT = 20000
WEBP_MAX_DIMENSION = 16383  # libwebp hard limit per side

# Metadata handling
EXIF_MODES = {'strip', 'camera'}
DEFAULT_EXIF_MODE = 'strip'

# EXIF tags kept in 'camera' mode. GPS (IFD 0x8825) is never copied.
# Orientation is not kept either: pixels are already rotated upright.
CAMERA_BASE_TAGS = (
    0x010F,  # Make
    0x0110,  # Model
    0x013B,  # Artist
    0x8298,  # Copyright
)
CAMERA_EXIF_IFD = 0x8769
CAMERA_EXIF_TAGS = (
    0x829A,  # ExposureTime
    0x829D,  # FNumber
    0x8822,  # ExposureProgram
    0x8827,  # ISOSpeedRatings
    0x9003,  # DateTimeOriginal
    0x9204,  # ExposureBiasValue
    0x920A,  # FocalLength
    0xA405,  # FocalLengthIn35mmFilm
    0xA433,  # LensMake
    0xA434,  # LensModel
)

# sRGB profile used as conversion target and embedded in every output
_SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
SRGB_ICC_BYTES = _SRGB_PROFILE.tobytes()

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

@app.errorhandler(413)
def request_entity_too_large(error):
    """Handle large file uploads gracefully."""
    return jsonify({'error': 'File too large. Maximum upload size is 500MB.'}), 413

# Store session data (in production, use Redis or similar)
sessions = {}
_sessions_lock = threading.Lock()


def allowed_file(filename):
    """Check if file has allowed extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _parse_int(value, default):
    """Parse an optional integer form field. Missing -> default, invalid -> None."""
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def encode_image(image, quality, save_format, icc_profile=None, exif_bytes=None):
    """Encode image to a BytesIO buffer and return (size_kb, buffer).

    save_format is 'WEBP' or 'JPEG'. JPEG output is always progressive.
    Buffer position is at 0 after return for immediate reading."""
    buffer = io.BytesIO()
    params = {'quality': quality}
    if icc_profile:
        params['icc_profile'] = icc_profile
    if exif_bytes:
        params['exif'] = exif_bytes

    if save_format == 'WEBP':
        # method 4 is libwebp's default speed/size trade-off; 6 is ~2-3x slower
        # for a few % gain, too slow inside a binary search on large photos.
        params['method'] = 4
    else:
        params['optimize'] = True
        params['progressive'] = True
        # Full chroma resolution at high quality, 4:2:0 otherwise
        params['subsampling'] = 0 if quality >= 90 else 2
        # Explicitly clear JPEG comment marker (COM) so source comments are never retained
        params['comment'] = b''

    image.save(buffer, format=save_format, **params)
    size_kb = buffer.tell() / 1024
    buffer.seek(0)
    return size_kb, buffer


def _write_buffer_to_file(buffer, output_path):
    """Stream buffer contents to file without loading entire buffer into memory."""
    buffer.seek(0)
    with open(output_path, 'wb') as f:
        shutil.copyfileobj(buffer, f, length=1024 * 1024)


def _has_transparency(image):
    """True if the image carries transparency (alpha channel, palette transparency, or color-key tRNS)."""
    return (
        image.mode in ('RGBA', 'LA', 'PA')
        or 'transparency' in image.info
    )


def to_srgb(image):
    """Convert image to sRGB using its embedded ICC profile, if any.

    Returns (image, description). The returned image is 'RGB' or 'RGBA'.
    Images without a profile are assumed to already be sRGB."""
    icc = image.info.get('icc_profile')
    alpha = None

    if image.mode in ('RGBA', 'LA'):
        alpha = image.getchannel('A')
        image = image.convert('RGB') if image.mode == 'RGBA' else image.convert('L')

    description = 'sRGB (assumed, no embedded profile)'
    if icc:
        try:
            src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            src_name = (ImageCms.getProfileDescription(src_profile) or 'unknown').strip()
            if icc == SRGB_ICC_BYTES:
                description = f'sRGB (source: {src_name})'
                image = image.convert('RGB')
            else:
                image = ImageCms.profileToProfile(
                    image, src_profile, _SRGB_PROFILE,
                    renderingIntent=ImageCms.Intent.PERCEPTUAL,
                    outputMode='RGB',
                )
                description = f'sRGB (converted from {src_name})'
        except (ImageCms.PyCMSError, OSError, ValueError) as e:
            logger.warning(f'ICC conversion failed, assuming sRGB: {e}')
            image = image.convert('RGB')
            description = 'sRGB (profile unreadable, assumed)'
    else:
        image = image.convert('RGB')

    if alpha is not None:
        image.putalpha(alpha)
    return image, description


def build_camera_exif(source_exif):
    """Return EXIF bytes with camera/lens/exposure tags only (no GPS), or None."""
    if not source_exif:
        return None

    kept = Image.Exif()
    for tag in CAMERA_BASE_TAGS:
        if tag in source_exif:
            kept[tag] = source_exif[tag]

    try:
        exif_ifd = source_exif.get_ifd(CAMERA_EXIF_IFD)
    except (KeyError, ValueError, TypeError):
        exif_ifd = {}
    sub = {tag: exif_ifd[tag] for tag in CAMERA_EXIF_TAGS if tag in exif_ifd}
    if sub:
        kept[CAMERA_EXIF_IFD] = sub

    if not len(kept):
        return None
    return kept.tobytes()


def _resize_to_long_edge(image, long_edge):
    """Downscale so the longest side equals long_edge (never upscales)."""
    width, height = image.size
    current = max(width, height)
    if long_edge >= current:
        return image
    scale = long_edge / current
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(new_size, Image.LANCZOS)


def compress_to_target_size(image_path, target_kb, output_path, output_format=DEFAULT_OUTPUT_FORMAT,
                            max_edge=0, min_quality=DEFAULT_MIN_QUALITY, exif_mode=DEFAULT_EXIF_MODE,
                            max_width=0):
    """
    Compress image to fit target_kb with the best possible quality.

    Pipeline:
      1. Apply EXIF orientation, then downscale to max_edge/max_width (if set).
      2. Convert colours to sRGB and embed an sRGB profile.
      3. Binary-search quality in [min_quality, 95] at the current size.
      4. If even min_quality doesn't fit, binary-search the largest size that
         fits at min_quality. Quality is never pushed below the floor.

    Args:
        image_path: Path to source image
        target_kb: Target file size in KB (never exceeded unless impossible)
        output_path: Path for compressed output (extension must match format)
        output_format: 'webp' or 'jpeg' (progressive)
        max_edge: Max long edge in px, 0 = keep original size
        min_quality: Quality floor (10-95)
        exif_mode: 'strip' (remove all metadata) or 'camera' (keep camera/lens
                   info, always remove GPS)
        max_width: Max width in px, 0 = no width limit

    Returns dict with compression results.
    """
    save_format, _ = OUTPUT_FORMATS[output_format]
    min_quality = max(MIN_QUALITY_LOWER_BOUND, min(MAX_QUALITY, int(min_quality)))

    with Image.open(image_path) as opened:
        source_exif = opened.getexif()
        # Fix orientation from EXIF data so rotated photos compress correctly
        image = ImageOps.exif_transpose(opened)
        # exif_transpose returns a copy without the original info in some cases
        if 'icc_profile' in opened.info and 'icc_profile' not in image.info:
            image.info['icc_profile'] = opened.info['icc_profile']
        if 'transparency' in opened.info and 'transparency' not in image.info:
            image.info['transparency'] = opened.info['transparency']
        image.load()

    original_size_kb = os.path.getsize(image_path) / 1024
    orig_w, orig_h = image.size

    # 16-bit greyscale PNGs: scale to 8-bit, extracting alpha mask if tRNS is present
    if image.mode in ('I', 'I;16', 'I;16B', 'I;16L'):
        img_i = image.convert('I')
        l8 = img_i.point(lambda v: v / 256).convert('L')
        l8.info.update(image.info)
        key = image.info.get('transparency')
        if key is not None and isinstance(key, int) and 0 <= key < 65536:
            lut = [255] * 65536
            lut[key] = 0
            alpha = img_i.point(lut, 'L')
            image = Image.merge('LA', (l8, alpha))
            image.info.update(l8.info)
            image.info.pop('transparency', None)
        else:
            image = l8

    # 1. Normalize transparency and palette images up front before any resizing
    # or color conversion alters color keys or triggers nearest-neighbor scaling.
    # Preserve grayscale transparent images as LA so embedded grayscale ICC profiles match in to_srgb.
    if image.mode == 'L' and 'transparency' in image.info:
        image = image.convert('LA')
    elif image.mode == 'P':
        if 'transparency' in image.info or image.mode == 'PA':
            image = image.convert('RGBA')
        else:
            image = image.convert('RGB')
    elif image.mode == 'PA':
        image = image.convert('RGBA')
    elif image.mode == 'RGB' and 'transparency' in image.info:
        image = image.convert('RGBA')

    # 2. Apply all resize limits in one pass, without cropping or upscaling.
    width, height = image.size
    edge_limit = int(max_edge) if max_edge else max(image.size)
    if save_format == 'WEBP':
        edge_limit = min(edge_limit, WEBP_MAX_DIMENSION)
    scale = min(1.0, edge_limit / max(image.size),
                int(max_width) / width if max_width else 1.0)
    if scale < 1.0:
        image = image.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                             Image.LANCZOS)

    # 3. Colour: everything becomes sRGB with an embedded sRGB profile
    image, color_description = to_srgb(image)

    # 4. Transparency: WebP keeps it, JPEG flattens onto white
    if image.mode == 'RGBA':
        if save_format == 'JPEG':
            background = Image.new('RGB', image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel('A'))
            image = background
        elif image.getchannel('A').getextrema() == (255, 255):
            image = image.convert('RGB')  # fully opaque, drop the useless alpha

    # 5. Metadata
    exif_bytes = build_camera_exif(source_exif) if exif_mode == 'camera' else None
    had_gps = bool(source_exif) and 0x8825 in source_exif

    def encode(img, quality):
        return encode_image(img, quality, save_format, SRGB_ICC_BYTES, exif_bytes)

    def find_best_quality(img):
        """Highest quality in [min_quality, MAX_QUALITY] that fits.
        Returns ((q, size, buf) or None, min_size)."""
        min_size, min_buf = encode(img, min_quality)
        if min_size > target_kb:
            return None, min_size
        best = (min_quality, min_size, min_buf)
        lo, hi = min_quality + 1, MAX_QUALITY
        while lo <= hi:
            mid = (lo + hi) // 2
            size_kb, buf = encode(img, mid)
            if size_kb <= target_kb:
                best = (mid, size_kb, buf)
                lo = mid + 1
            else:
                hi = mid - 1
        return best, min_size

    target_met = True
    working = image
    found, min_size_full = find_best_quality(working)

    if found is None:
        # Need to downscale. Search the long edge (in px) that fits at min_quality.
        long_edge = max(working.size)
        floor_edge = 1
        lo, hi = floor_edge, long_edge - 1
        best_fit = None
        while lo <= hi:
            guess = (lo + hi) // 2
            candidate = _resize_to_long_edge(working, guess)
            size_kb, buf = encode(candidate, min_quality)
            if size_kb <= target_kb:
                best_fit = candidate
                lo = guess + 1
            else:
                hi = guess - 1

        if best_fit is not None:
            working = best_fit
            found, _ = find_best_quality(working)
        if found is None:
            # Even the smallest size at the quality floor is too big (or long_edge was 1)
            working = _resize_to_long_edge(working, floor_edge)
            size_kb, buf = encode(working, min_quality)
            found = (min_quality, size_kb, buf)
            target_met = size_kb <= target_kb

    quality, size_kb, buf = found
    _write_buffer_to_file(buf, output_path)
    final_w, final_h = working.size

    if exif_mode == 'camera':
        metadata = 'Camera info kept' + (' (GPS removed)' if had_gps else '') if exif_bytes else 'No camera info in source'
    else:
        metadata = 'All metadata stripped'

    return {
        'original_size_kb': round(original_size_kb, 2),
        'original_resolution': f'{orig_w}x{orig_h}',
        'final_size_kb': round(size_kb, 2),
        'final_resolution': f'{final_w}x{final_h}',
        'quality_used': quality,
        'scale_factor': round(final_w / orig_w, 3) if orig_w else 1.0,
        'output_format': 'WebP' if save_format == 'WEBP' else 'Progressive JPEG',
        'color_profile': color_description,
        'metadata': metadata,
        'target_met': target_met,
        'output_filename': os.path.basename(output_path),
    }


_last_cleanup_time = 0.0
_cleanup_lock = threading.Lock()
_CLEANUP_INTERVAL_SECONDS = 60


def cleanup_old_sessions(max_age_hours=1):
    """Remove session folders and in-memory session entries older than max_age_hours.
    Rate-limited to once per minute."""
    global _last_cleanup_time

    now = time.monotonic()
    if now - _last_cleanup_time < _CLEANUP_INTERVAL_SECONDS:
        return

    # Use a lock so only one thread runs cleanup at a time
    if not _cleanup_lock.acquire(blocking=False):
        return
    try:
        _last_cleanup_time = now
        cutoff = datetime.now() - timedelta(hours=max_age_hours)

        cleaned_count = 0
        for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
            if os.path.exists(folder):
                for session_id in os.listdir(folder):
                    session_path = os.path.join(folder, session_id)
                    if os.path.isdir(session_path):
                        mtime = datetime.fromtimestamp(os.path.getmtime(session_path))
                        if mtime < cutoff:
                            shutil.rmtree(session_path, ignore_errors=True)
                            cleaned_count += 1

        # Also clean up in-memory session dict entries to prevent memory leak
        expired_sessions = []
        with _sessions_lock:
            for sid, data in sessions.items():
                if data.get('created', datetime.now()) < cutoff:
                    expired_sessions.append(sid)
            for sid in expired_sessions:
                del sessions[sid]

        if cleaned_count > 0 or expired_sessions:
            logger.debug(f'Cleaned up {cleaned_count} expired session folders, '
                         f'{len(expired_sessions)} in-memory session entries')
    finally:
        _cleanup_lock.release()


@app.route('/')
def index():
    """Render the main upload page."""
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_files():
    """Handle batch file upload and compression."""
    # Cleanup old sessions periodically
    cleanup_old_sessions()

    if 'files[]' not in request.files:
        logger.warning('Upload request received with no files')
        return jsonify({'error': 'No files provided'}), 400

    files = request.files.getlist('files[]')
    target_kb = request.form.get('target_kb', type=float)
    output_format = request.form.get('output_format', DEFAULT_OUTPUT_FORMAT)
    max_edge = _parse_int(request.form.get('max_edge'), default=0)
    max_width = _parse_int(request.form.get('max_width'), default=0)
    min_quality = _parse_int(request.form.get('min_quality'), default=DEFAULT_MIN_QUALITY)
    exif_mode = request.form.get('exif_mode', DEFAULT_EXIF_MODE)

    if not target_kb or target_kb <= 0:
        logger.warning(f'Invalid target size requested: {target_kb}')
        return jsonify({'error': 'Invalid target size'}), 400

    if output_format not in OUTPUT_FORMATS:
        logger.warning(f'Invalid output format requested: {output_format}')
        return jsonify({'error': f'Invalid output format. Use one of: {", ".join(OUTPUT_FORMATS)}'}), 400

    if max_edge is None or not (0 <= max_edge <= MAX_EDGE_LIMIT):
        return jsonify({'error': f'Invalid max edge. Use 0 (off) to {MAX_EDGE_LIMIT} px'}), 400

    if max_width is None or not (0 <= max_width <= MAX_EDGE_LIMIT):
        logger.warning(f'Invalid max width requested: {request.form.get("max_width")}')
        return jsonify({'error': f'Invalid max width. Use 0 (off) to {MAX_EDGE_LIMIT} px'}), 400

    if min_quality is None or not (MIN_QUALITY_LOWER_BOUND <= min_quality <= MAX_QUALITY):
        return jsonify({'error': f'Invalid min quality. Use {MIN_QUALITY_LOWER_BOUND}-{MAX_QUALITY}'}), 400

    if exif_mode not in EXIF_MODES:
        return jsonify({'error': f'Invalid metadata mode. Use one of: {", ".join(sorted(EXIF_MODES))}'}), 400

    if not files or all(f.filename == '' for f in files):
        logger.warning('Upload request with empty file list')
        return jsonify({'error': 'No files selected'}), 400

    logger.info(f'Upload received: {len(files)} files, target={target_kb}KB, format={output_format}, '
                f'max_edge={max_edge or "off"}, max_width={max_width or "off"}, '
                f'min_quality={min_quality}, exif={exif_mode}')

    # Create session folders
    session_id = str(uuid.uuid4())
    upload_path = os.path.join(UPLOAD_FOLDER, session_id)
    output_path = os.path.join(OUTPUT_FOLDER, session_id)
    os.makedirs(upload_path, exist_ok=True)
    os.makedirs(output_path, exist_ok=True)
    logger.debug(f'Created session {session_id[:8]}')

    results = []
    processed_count = 0
    _, out_ext = OUTPUT_FORMATS[output_format]
    used_output_names = set()

    # Save all uploaded files first, then compress in parallel
    compress_jobs = []
    for file in files:
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)

            # Handle duplicate filenames
            base, ext = os.path.splitext(filename)
            counter = 1
            original_filename = filename
            while os.path.exists(os.path.join(upload_path, filename)):
                filename = f"{base}_{counter}{ext}"
                counter += 1

            # Output names must be unique too: photo.jpg and photo.png would
            # both become photo.webp. Resolved here, before parallel work starts.
            out_base = os.path.splitext(filename)[0]
            out_name = out_base + out_ext
            counter = 1
            while out_name in used_output_names:
                out_name = f"{out_base}_{counter}{out_ext}"
                counter += 1
            used_output_names.add(out_name)

            input_file = os.path.join(upload_path, filename)
            output_file = os.path.join(output_path, out_name)

            # Save uploaded file
            file.save(input_file)
            compress_jobs.append((input_file, target_kb, output_file, output_format, filename, original_filename))

    def _compress_one(job):
        input_file, t_kb, output_file, out_fmt, filename, original_filename = job
        try:
            compression_result = compress_to_target_size(
                input_file, t_kb, output_file, out_fmt,
                max_edge=max_edge, min_quality=min_quality, exif_mode=exif_mode,
                max_width=max_width,
            )
            compression_result['filename'] = compression_result.get('output_filename', filename)
            compression_result['original_filename'] = original_filename
            compression_result['success'] = True
            logger.info(f'Compressed {filename}: {compression_result["original_size_kb"]}KB → {compression_result["final_size_kb"]}KB '
                        f'({compression_result["final_resolution"]}, quality={compression_result["quality_used"]}, '
                        f'target_met={compression_result["target_met"]})')
            return compression_result
        except Exception as e:
            logger.error(f'Failed to compress {filename}: {str(e)}')
            # Clean up the uploaded file on failure to avoid disk waste
            try:
                os.remove(input_file)
            except OSError:
                pass
            return {
                'filename': filename,
                'original_filename': original_filename,
                'success': False,
                'error': str(e)
            }

    # Compress images in parallel using threads (Pillow releases the GIL for C operations)
    # Cap workers to avoid thread thrashing on high-core-count machines
    max_workers = min(len(compress_jobs), os.cpu_count() or 2, MAX_WORKERS_CAP)
    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(_compress_one, compress_jobs))
    else:
        results = [_compress_one(job) for job in compress_jobs]

    processed_count = sum(1 for r in results if r.get('success'))

    if processed_count == 0:
        # Cleanup empty session folders
        shutil.rmtree(upload_path, ignore_errors=True)
        shutil.rmtree(output_path, ignore_errors=True)
        return jsonify({'error': 'No valid image files were processed'}), 400

    # Store session info (thread-safe)
    with _sessions_lock:
        sessions[session_id] = {
            'created': datetime.now(),
            'results': results,
            'target_kb': target_kb
        }

    logger.info(f'Session {session_id[:8]}: Processed {processed_count} images successfully')
    return jsonify({
        'session_id': session_id,
        'results': results,
        'processed_count': processed_count,
        'target_kb': target_kb
    })


@app.route('/download/<session_id>')
def download_zip(session_id):
    """Create and download ZIP file with compressed images."""
    # Security: Validate session_id to prevent path traversal
    if not is_valid_session_id(session_id):
        logger.warning(f'Invalid session ID attempted: {session_id[:50]}')
        return jsonify({'error': 'Invalid session ID'}), 400

    output_path = os.path.join(OUTPUT_FOLDER, session_id)

    if not os.path.exists(output_path):
        logger.warning(f'Download requested for expired/missing session: {session_id[:8]}')
        return jsonify({'error': 'Session not found or expired'}), 404

    logger.info(f'ZIP download started for session {session_id[:8]}')

    # Create ZIP on disk to avoid loading entire archive into memory
    zip_fd, zip_path = tempfile.mkstemp(suffix='.zip', prefix=f'zip_{session_id[:8]}_')
    try:
        with os.fdopen(zip_fd, 'wb') as zip_raw:
            with zipfile.ZipFile(zip_raw, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for filename in sorted(os.listdir(output_path)):
                    file_path = os.path.join(output_path, filename)
                    if os.path.isfile(file_path):
                        zip_file.write(file_path, filename)

        return send_file(
            zip_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'compressed_images_{session_id[:8]}.zip'
        )
    finally:
        # send_file reads the file during the response; schedule cleanup after
        # The file will be removed by the OS temp cleanup or next session cleanup
        pass


@app.route('/download/<session_id>/<filename>')
def download_single(session_id, filename):
    """Download a single compressed image from a session."""
    # Security: Validate session_id to prevent path traversal
    if not is_valid_session_id(session_id):
        logger.warning(f'Invalid session ID attempted: {session_id[:50]}')
        return jsonify({'error': 'Invalid session ID'}), 400

    # Security: sanitize filename to prevent path traversal
    filename = secure_filename(filename)
    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400

    output_path = os.path.join(OUTPUT_FOLDER, session_id)
    file_path = os.path.join(output_path, filename)

    # Ensure the resolved path is within the output directory
    if not os.path.realpath(file_path).startswith(os.path.realpath(output_path)):
        logger.warning(f'Path traversal attempt in single download: {filename}')
        return jsonify({'error': 'Invalid filename'}), 400

    if not os.path.isfile(file_path):
        return jsonify({'error': 'File not found'}), 404

    logger.info(f'Single file download: {filename} from session {session_id[:8]}')
    return send_file(file_path, as_attachment=True, download_name=filename)


@app.route('/cleanup/<session_id>', methods=['POST'])
def cleanup_session(session_id):
    """Manually cleanup a session's files."""
    # Security: Validate session_id to prevent path traversal
    if not is_valid_session_id(session_id):
        logger.warning(f'Invalid session ID in cleanup: {session_id[:50]}')
        return jsonify({'error': 'Invalid session ID'}), 400

    upload_path = os.path.join(UPLOAD_FOLDER, session_id)
    output_path = os.path.join(OUTPUT_FOLDER, session_id)

    shutil.rmtree(upload_path, ignore_errors=True)
    shutil.rmtree(output_path, ignore_errors=True)

    with _sessions_lock:
        if session_id in sessions:
            del sessions[session_id]

    logger.info(f'Session {session_id[:8]} cleaned up manually')
    return jsonify({'success': True})


if __name__ == '__main__':
    # Ensure folders exist
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    logger.info('Keis ImageCompress starting on http://0.0.0.0:5050')
    logger.info(f'Log level: {LOG_LEVEL}')

    # Security: debug=False to prevent interactive debugger exposure
    app.run(host='0.0.0.0', port=5050, debug=False)
