#!/bin/bash
# Double-click this ONCE on a new Mac. It clears the "downloaded from the
# internet" quarantine flag from TubeRipper (so macOS lets it run) and opens it.
# After this, you can launch TubeRipper.app normally.
cd "$(dirname "$0")"
echo "Unblocking TUBE-RIPPER DELUXE 2000…"
xattr -dr com.apple.quarantine "TubeRipper.app" 2>/dev/null || true
echo "Done. Launching…"
open "TubeRipper.app"
echo "You can close this window."
