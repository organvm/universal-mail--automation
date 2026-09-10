#!/usr/bin/env python3
"""Compatibility entrypoint for the canonical mail workflow."""
from core.maintenance import main

if __name__ == "__main__":
    raise SystemExit(main())
