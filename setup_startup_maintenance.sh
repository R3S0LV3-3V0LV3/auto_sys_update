#!/bin/zsh
# Setup script for automatic system maintenance on startup

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PLIST_NAME="com.user.systemmaintenance.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST_NAME"

echo "🔧 Setting up automatic system maintenance..."
echo ""

# Make the maintenance script executable
chmod +x "$SCRIPT_DIR/system_maintenance.sh"
echo "✅ Made maintenance script executable"

# Copy the plist to LaunchAgents directory
mkdir -p "$LAUNCH_AGENTS_DIR"
cp "$SCRIPT_DIR/$PLIST_NAME" "$PLIST_PATH"
echo "✅ Copied plist to LaunchAgents"

# Load the launch agent
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "✅ Loaded launch agent"

# Verify it's loaded
if launchctl list | grep -q "com.user.systemmaintenance"; then
    echo "✅ Launch agent is active and will run on startup!"
else
    echo "⚠️ Launch agent may not be loaded properly. Check logs."
fi

echo ""
echo "🎉 Setup complete!"
echo ""
echo "📋 Your system will now run maintenance automatically on startup."
echo ""
echo "To test it now, run:"
echo "  osascript \"$SCRIPT_DIR/SystemMaintenanceRunner.scpt\""
echo ""
echo "To disable auto-run on startup:"
echo "  launchctl unload ~/Library/LaunchAgents/com.user.systemmaintenance.plist"
echo ""
echo "To re-enable:"
echo "  launchctl load ~/Library/LaunchAgents/com.user.systemmaintenance.plist"
echo ""
echo "Logs will be saved to:"
echo "  ~/system_maintenance_[timestamp].log"
