#!/usr/bin/env python3
"""Run the application."""
from app import app

if __name__ == '__main__':
    # Security: debug=False to prevent interactive debugger exposure
    app.run(host='0.0.0.0', port=5050, debug=False)
