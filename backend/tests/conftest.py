"""Pytest configuration for backend tests.

Enables explicit local test override for legacy test suites while keeping
fail-closed authentication enforced in default production environments.
"""

from __future__ import annotations

import os

# Default to explicit local/test override so existing tests run without auth
os.environ.setdefault("AUTH_OVERRIDE_LOCAL_ONLY", "true")
