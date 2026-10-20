# Automatic System Maintenance on Startup

This setup provides a comprehensive system maintenance script that runs automatically on macOS startup.

## What It Does

The maintenance script performs:

### 🍺 Homebrew Maintenance
- Updates Homebrew itself
- Checks for missing dependencies
- Lists outdated packages
- Upgrades all formulas and casks (with --greedy)
- Verifies dependency tree
- Deep cleans caches and old versions
- Relinks broken formulae
- Runs `brew doctor` diagnostics
- Purges logs and caches

### 📦 npm Maintenance
- Checks for outdated global packages
- Updates all global npm packages
- Cleans npm cache
- Runs `npm doctor` diagnostics
- Lists all global packages

### 🦀 Rust Maintenance
- Updates rustup itself
- Updates all installed toolchains
- Shows installed toolchains
- Updates cargo-installed binaries (if cargo-update is installed)
- Cleans up old artifacts

### 🍎 macOS System Updates
- Scans for available updates
- Installs all available updates
- Checks if restart is required

## Files Created

1. **system_maintenance.sh** - The main maintenance script
2. **SystemMaintenanceRunner.scpt** - AppleScript wrapper that runs the script in Terminal
3. **com.user.systemmaintenance.plist** - Launch Agent configuration
4. **setup_startup_maintenance.sh** - Installation script

## Installation

Run the setup script:

```bash
chmod +x ~/git_clones/auto_sys_update/setup_startup_maintenance.sh
~/git_clones/auto_sys_update/setup_startup_maintenance.sh
```

## Testing

To test the script without restarting:

```bash
osascript ~/git_clones/auto_sys_update/SystemMaintenanceRunner.scpt
```

Or run the shell script directly:

```bash
~/git_clones/auto_sys_update/system_maintenance.sh
```

## Logs

Each run creates a timestamped log file:
- Location: `~/system_maintenance_YYYYMMDD_HHMMSS.log`

## Management

### Disable auto-run on startup:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.systemmaintenance.plist
```

### Re-enable:
```bash
launchctl load ~/Library/LaunchAgents/com.user.systemmaintenance.plist
```

### Remove completely:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.systemmaintenance.plist
rm ~/Library/LaunchAgents/com.user.systemmaintenance.plist
```

## Important Notes

- The script requires sudo access for macOS system updates
- You'll be prompted for your password when macOS updates run
- The Terminal window will open automatically on startup showing progress
- Updates can take several minutes depending on what needs updating
- Some updates may require a system restart

## Customization

Edit `~/git_clones/auto_sys_update/system_maintenance.sh` to modify:
- Add/remove maintenance tasks
- Change emoji/messages
- Adjust error handling
- Add notifications

## Troubleshooting

If the launch agent doesn't run:
1. Check if it's loaded: `launchctl list | grep systemmaintenance`
2. Check error logs: `cat /tmp/systemmaintenance.err`
3. Verify script permissions: `ls -l ~/git_clones/auto_sys_update/system_maintenance.sh`
4. Test manually first to ensure all commands work

## Security Considerations

- The script has sudo access for system updates
- Review the script before running to understand what it does
- Logs may contain system information
- The script modifies system packages and configurations
