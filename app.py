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
from PIL import Image, ImageOps

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


def encode_image(image, quality, format='JPEG'):
    """Encode image to a BytesIO buffer and return (size_kb, buffer).
    Buffer position is at 0 after return for immediate reading."""
    buffer = io.BytesIO()
    if format == 'PNG':
        compress_level = max(1, min(9, 9 - (quality // 11)))
        image.save(buffer, format=format, optimize=True, compress_level=compress_level)
    else:
        image.save(buffer, format=format, quality=quality, optimize=True)
    size_kb = buffer.tell() / 1024
    buffer.seek(0)
    return size_kb, buffer


def _write_buffer_to_file(buffer, output_path):
    """Stream buffer contents to file without loading entire buffer into memory."""
    buffer.seek(0)
    with open(output_path, 'wb') as f:
        shutil.copyfileobj(buffer, f, length=1024 * 1024)


def compress_to_target_size(image_path, target_kb, output_path, output_format='original'):
    """
    Compress image to target KB size with best possible quality.
    Uses binary search on both scale factor and quality for fast convergence,
    and reuses the encoded buffer to avoid redundant encoding.

    Args:
        image_path: Path to source image
        target_kb: Target file size in KB
        output_path: Path for compressed output
        output_format: 'original', 'jpg', or 'png'

    Returns dict with compression results.
    """
    original_image = Image.open(image_path)
    # Fix orientation from EXIF data so rotated photos compress correctly
    original_image = ImageOps.exif_transpose(original_image)
    # Pre-load image data to avoid lazy I/O during resize/encode
    original_image.load()
    original_ext = os.path.splitext(image_path)[1].lower()

    # Determine output format
    if output_format == 'jpg':
        save_format = 'JPEG'
        out_ext = '.jpg'
    elif output_format == 'png':
        save_format = 'PNG'
        out_ext = '.png'
    else:  # original
        if original_ext in ['.png']:
            save_format = 'PNG'
            out_ext = '.png'
        else:
            save_format = 'JPEG'
            out_ext = '.jpg'

    # Update output path with correct extension
    output_base = os.path.splitext(output_path)[0]
    output_path = output_base + out_ext

    # Convert to appropriate mode
    if save_format == 'JPEG' and original_image.mode in ('RGBA', 'P', 'LA'):
        original_image = original_image.convert('RGB')
    elif save_format == 'PNG' and original_image.mode == 'P':
        original_image = original_image.convert('RGBA')

    original_size_kb = os.path.getsize(image_path) / 1024
    width, height = original_image.size

    result = {
        'original_size_kb': round(original_size_kb, 2),
        'original_resolution': f'{width}x{height}',
        'final_size_kb': 0,
        'final_resolution': '',
        'quality_used': 0,
        'scale_factor': 1.0,
        'output_format': save_format
    }

    # If already under target, just optimize
    if original_size_kb <= target_kb:
        if save_format == 'PNG':
            original_image.save(output_path, 'PNG', optimize=True)
        else:
            original_image.save(output_path, 'JPEG', quality=95, optimize=True)
        final_size = os.path.getsize(output_path) / 1024
        result['final_size_kb'] = round(final_size, 2)
        result['final_resolution'] = f'{width}x{height}'
        result['quality_used'] = 95
        result['output_filename'] = os.path.basename(output_path)
        return result

    def find_best_quality(image, save_fmt, target):
        """Binary search for best quality that fits target. Returns (quality, size, buffer) or None."""
        min_q, max_q = 25, 95
        best_quality = None
        best_size = float('inf')
        best_buffer = None

        # Early exit: if minimum quality is already too big, no solution at this scale
        min_size, _ = encode_image(image, min_q, save_fmt)
        if min_size > target:
            return None

        while min_q <= max_q:
            mid_q = (min_q + max_q) // 2
            size_kb, buf = encode_image(image, mid_q, save_fmt)

            if size_kb <= target:
                best_quality = mid_q
                best_size = size_kb
                best_buffer = buf
                min_q = mid_q + 1
            else:
                max_q = mid_q - 1

        if best_quality is not None:
            return best_quality, best_size, best_buffer
        return None

    # Try quality-only first (scale=1.0)
    quality_result = find_best_quality(original_image, save_format, target_kb)
    if quality_result:
        best_quality, best_size, best_buffer = quality_result
        _write_buffer_to_file(best_buffer, output_path)
        result['final_size_kb'] = round(best_size, 2)
        result['final_resolution'] = f'{width}x{height}'
        result['quality_used'] = best_quality
        result['scale_factor'] = 1.0
        result['output_filename'] = os.path.basename(output_path)
        return result

    # Estimate starting scale from area ratio: file size roughly scales with pixel count.
    # Narrow the binary search range around the estimate for faster convergence.
    estimated_scale = (target_kb / original_size_kb) ** 0.5
    estimated_scale = max(0.1, min(0.9, estimated_scale))
    estimated_idx = int(estimated_scale * 10)

    # Search a window around the estimate first, then fall back to full range
    window = 2  # search ±2 around estimate (covers 5 scale values)
    scale_lo = max(1, estimated_idx - window)
    scale_hi = min(9, estimated_idx + window)

    best_scale_result = None

    while scale_lo <= scale_hi:
        scale_mid = (scale_lo + scale_hi) // 2
        scale = scale_mid / 10.0
        new_w = int(width * scale)
        new_h = int(height * scale)
        if new_w < 1 or new_h < 1:
            scale_lo = scale_mid + 1
            continue

        scaled_image = original_image.resize((new_w, new_h), Image.LANCZOS)
        quality_result = find_best_quality(scaled_image, save_format, target_kb)

        if quality_result:
            best_scale_result = (scale, scaled_image, quality_result)
            scale_lo = scale_mid + 1  # try larger scale (higher value = less downscaling)
        else:
            scale_hi = scale_mid - 1  # need smaller scale (more downscaling)

    # If no result in the estimated window, expand to full range
    if best_scale_result is None and (estimated_idx - window > 1 or estimated_idx + window < 9):
        scale_lo, scale_hi = 1, 9
        while scale_lo <= scale_hi:
            scale_mid = (scale_lo + scale_hi) // 2
            scale = scale_mid / 10.0
            new_w = int(width * scale)
            new_h = int(height * scale)
            if new_w < 1 or new_h < 1:
                scale_lo = scale_mid + 1
                continue

            scaled_image = original_image.resize((new_w, new_h), Image.LANCZOS)
            quality_result = find_best_quality(scaled_image, save_format, target_kb)

            if quality_result:
                best_scale_result = (scale, scaled_image, quality_result)
                scale_lo = scale_mid + 1
            else:
                scale_hi = scale_mid - 1

    if best_scale_result:
        scale, scaled_image, (best_quality, best_size, best_buffer) = best_scale_result
        _write_buffer_to_file(best_buffer, output_path)
        new_w, new_h = scaled_image.size
        result['final_size_kb'] = round(best_size, 2)
        result['final_resolution'] = f'{new_w}x{new_h}'
        result['quality_used'] = best_quality
        result['scale_factor'] = scale
        result['output_filename'] = os.path.basename(output_path)
        return result

    # Fallback: use minimum quality at minimum scale
    min_scale_image = original_image.resize(
        (max(1, int(width * 0.1)), max(1, int(height * 0.1))),
        Image.LANCZOS
    )
    _, fallback_buffer = encode_image(min_scale_image, 25, save_format)
    _write_buffer_to_file(fallback_buffer, output_path)
    final_size = os.path.getsize(output_path) / 1024

    result['final_size_kb'] = round(final_size, 2)
    new_w, new_h = min_scale_image.size
    result['final_resolution'] = f'{new_w}x{new_h}'
    result['quality_used'] = 25
    result['scale_factor'] = 0.1
    result['output_filename'] = os.path.basename(output_path)

    return result


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
    output_format = request.form.get('output_format', 'original')

    if not target_kb or target_kb <= 0:
        logger.warning(f'Invalid target size requested: {target_kb}')
        return jsonify({'error': 'Invalid target size'}), 400

    if not files or all(f.filename == '' for f in files):
        logger.warning('Upload request with empty file list')
        return jsonify({'error': 'No files selected'}), 400

    logger.info(f'Upload received: {len(files)} files, target={target_kb}KB, format={output_format}')

    # Create session folders
    session_id = str(uuid.uuid4())
    upload_path = os.path.join(UPLOAD_FOLDER, session_id)
    output_path = os.path.join(OUTPUT_FOLDER, session_id)
    os.makedirs(upload_path, exist_ok=True)
    os.makedirs(output_path, exist_ok=True)
    logger.debug(f'Created session {session_id[:8]}')

    results = []
    processed_count = 0

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

            input_file = os.path.join(upload_path, filename)
            output_file = os.path.join(output_path, filename)

            # Save uploaded file
            file.save(input_file)
            compress_jobs.append((input_file, target_kb, output_file, output_format, filename, original_filename))

    def _compress_one(job):
        input_file, t_kb, output_file, out_fmt, filename, original_filename = job
        try:
            compression_result = compress_to_target_size(input_file, t_kb, output_file, out_fmt)
            compression_result['filename'] = compression_result.get('output_filename', filename)
            compression_result['original_filename'] = original_filename
            compression_result['success'] = True
            logger.info(f'Compressed {filename}: {compression_result["original_size_kb"]}KB → {compression_result["final_size_kb"]}KB (quality={compression_result["quality_used"]}, scale={compression_result["scale_factor"]})')
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
