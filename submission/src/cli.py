"""
CLI entry point.

Usage: python -m src.cli replay --pack <path> --out sample_run/ --mock
"""
from .replay import main

if __name__ == "__main__":
    main()
