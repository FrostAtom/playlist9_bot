"""Docker healthcheck: pass if the heartbeat file is fresh."""
import sys
import time
from pathlib import Path

HEARTBEAT_FILE = Path("/tmp/heartbeat")
MAX_AGE = 60

try:
    age = time.time() - HEARTBEAT_FILE.stat().st_mtime
except OSError:
    sys.exit(1)

sys.exit(0 if age < MAX_AGE else 1)
