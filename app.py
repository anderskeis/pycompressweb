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
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
from PIL import Image

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

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

@app.errorhandler(413)
def request_entity_too_large(error):
    """Handle large file uploads gracefully."""
    return jsonify({'error': 'File too large. Maximum upload size is 500MB.'}), 413

# Store session data (in production, use Redis or similar)
sessions = {}


def allowed_file(filename):
    """Check if file has allowed extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_size_kb(image, quality, format='JPEG'):
    """Get the file size in KB for an image at given quality."""
    buffer = io.BytesIO()
    if format == 'PNG':
        # PNG uses compress_level (0-9) instead of quality
        compress_level = max(1, min(9, 9 - (quality // 11)))
        image.save(buffer, format=format, optimize=True, compress_level=compress_level)
    else:
        image.save(buffer, format=format, quality=quality, optimize=True)
    return buffer.tell() / 1024


def compress_to_target_size(image_path, target_kb, output_path, output_format='original'):
    """
    Compress image to target KB size with best possible quality.
    Uses binary search to find optimal quality, then reduces resolution if needed.
    
    Args:
        image_path: Path to source image
        target_kb: Target file size in KB
        output_path: Path for compressed output
        output_format: 'original', 'jpg', or 'png'
    
    Returns dict with compression results.
    """
    original_image = Image.open(image_path)
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
    
    current_image = original_image.copy()
    scale_factor = 1.0
    
    # Try different scale factors (100%, 90%, 80%, ..., 10%)
    for scale in [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]:
        if scale < 1.0:
            new_width = int(width * scale)
            new_height = int(height * scale)
            current_image = original_image.resize((new_width, new_height), Image.LANCZOS)
        
        # optimization: check if lowest quality is already too big at this scale
        # This saves doing the full binary search if there's no hope for this scale
        min_quality_check = 25
        min_size_at_scale = get_file_size_kb(current_image, min_quality_check, save_format)
        
        if min_size_at_scale > target_kb:
            continue

        # Binary search for optimal quality at this resolution
        min_quality = 25
        max_quality = 95
        best_quality = min_quality
        best_size = float('inf')
        
        while min_quality <= max_quality:
            mid_quality = (min_quality + max_quality) // 2
            size_kb = get_file_size_kb(current_image, mid_quality, save_format)
            
            if size_kb <= target_kb:
                best_quality = mid_quality
                best_size = size_kb
                min_quality = mid_quality + 1  # Try higher quality
            else:
                max_quality = mid_quality - 1  # Try lower quality
        
        # Check if we found a valid solution at this scale
        if best_size <= target_kb:
            scale_factor = scale
            # Save the result
            if save_format == 'PNG':
                compress_level = max(1, min(9, 9 - (best_quality // 11)))
                current_image.save(output_path, 'PNG', optimize=True, compress_level=compress_level)
            else:
                current_image.save(output_path, 'JPEG', quality=best_quality, optimize=True)
            final_size = os.path.getsize(output_path) / 1024
            
            result['final_size_kb'] = round(final_size, 2)
            new_w, new_h = current_image.size
            result['final_resolution'] = f'{new_w}x{new_h}'
            result['quality_used'] = best_quality
            result['scale_factor'] = scale_factor
            result['output_filename'] = os.path.basename(output_path)
            return result
    
    # Fallback: use minimum quality at minimum scale
    min_scale_image = original_image.resize(
        (int(width * 0.1), int(height * 0.1)), 
        Image.LANCZOS
    )
    if save_format == 'PNG':
        min_scale_image.save(output_path, 'PNG', optimize=True, compress_level=9)
    else:
        min_scale_image.save(output_path, 'JPEG', quality=25, optimize=True)
    final_size = os.path.getsize(output_path) / 1024
    
    result['final_size_kb'] = round(final_size, 2)
    new_w, new_h = min_scale_image.size
    result['final_resolution'] = f'{new_w}x{new_h}'
    result['quality_used'] = 25
    result['scale_factor'] = 0.1
    result['output_filename'] = os.path.basename(output_path)
    
    return result


def cleanup_old_sessions(max_age_hours=1):
    """Remove session folders older than max_age_hours."""
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
    if cleaned_count > 0:
        logger.debug(f'Cleaned up {cleaned_count} expired session folders')


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
            
            try:
                # Compress the image
                compression_result = compress_to_target_size(input_file, target_kb, output_file, output_format)
                compression_result['filename'] = compression_result.get('output_filename', filename)
                compression_result['original_filename'] = original_filename
                compression_result['success'] = True
                results.append(compression_result)
                processed_count += 1
                logger.info(f'Compressed {filename}: {compression_result["original_size_kb"]}KB → {compression_result["final_size_kb"]}KB (quality={compression_result["quality_used"]}, scale={compression_result["scale_factor"]})')
            except Exception as e:
                logger.error(f'Failed to compress {filename}: {str(e)}')
                results.append({
                    'filename': filename,
                    'original_filename': original_filename,
                    'success': False,
                    'error': str(e)
                })
    
    if processed_count == 0:
        # Cleanup empty session folders
        shutil.rmtree(upload_path, ignore_errors=True)
        shutil.rmtree(output_path, ignore_errors=True)
        return jsonify({'error': 'No valid image files were processed'}), 400
    
    # Store session info
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
    
    # Create ZIP file in memory
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for filename in os.listdir(output_path):
            file_path = os.path.join(output_path, filename)
            if os.path.isfile(file_path):
                zip_file.write(file_path, filename)
    
    zip_buffer.seek(0)
    
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'compressed_images_{session_id[:8]}.zip'
    )


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
