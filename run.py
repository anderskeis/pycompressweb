#!/usr/bin/env python3
"""Run the application on a custom port."""
from app import app

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=True)
