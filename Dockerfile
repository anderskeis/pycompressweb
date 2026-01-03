FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies for Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories for uploads and output
RUN mkdir -p /tmp/pycompressweb/uploads /tmp/pycompressweb/output

# Expose port
EXPOSE 5050

# Run with gunicorn for production
# Increased timeout to 600s for large batch processing
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "600", "--graceful-timeout", "600", "app:app"]
