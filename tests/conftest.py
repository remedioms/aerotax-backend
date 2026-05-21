"""Pytest conftest — ENV defaults für ALL Test-Imports.

P0 #10 Fix: app.py macht beim Import einen Boot-Check für RECOVERY_SECRET.
Ohne Flag würden alle Tests beim `import app` crashen. Tests laufen damit
explizit im non-production-Modus.
"""
import os

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
