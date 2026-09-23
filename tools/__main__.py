"""Run with REasy's Python environment: python -m tools --help."""
import sys
from .cli.main import main

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
raise SystemExit(main())
