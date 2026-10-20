#!/bin/zsh
# Enable strict mode for debugging
set -euxo pipefail

echo "🚀 ENGAGING MAXIMUM SYSTEM OVERHAUL MODE... BUCKLE UP! 🔥"
sleep 1

# ==========================================
# 🍺 HOMEBREW SECTION
# ==========================================

echo ""
echo "=========================================="
echo "🍺 HOMEBREW MAINTENANCE INITIATED"
echo "=========================================="
echo ""

# 1️⃣ Update Homebrew
echo "🔄 Updating Homebrew... (Gotta stay ahead of the curve)"
brew update && echo "✅ Homebrew is fresh as a daisy!" || echo "❌ Update failed! Check logs."

# 2️⃣ Check for missing dependencies before upgrading
echo "🕵️ Checking for missing dependencies..."
brew missing || echo "✅ No missing dependencies! Your setup is tight."

# 3️⃣ Display outdated formulas and dependencies
echo "📜 Outdated software check (Know thy enemy)..."
brew outdated --verbose || echo "✅ Everything is already up to date!"

# 4️⃣ Upgrade everything (FORCE & VERBOSE & GREEDY)
echo "⚡ Forcing a brutal upgrade (no survivors)..."
brew upgrade --verbose --greedy && echo "🎉 Upgrades complete!" || echo "⚠️ Upgrade issues detected. Review logs!"

# 5️⃣ Ensure all dependencies are intact
echo "🔗 Checking installed dependencies..."
brew deps --tree --installed

# 6️⃣ Clean up unnecessary files
echo "🧹 Deep cleaning Homebrew... (Marie Kondo style)"
brew cleanup -s && brew autoremove && echo "✨ Cleanup complete!" || echo "❌ Cleanup encountered issues."

# 7️⃣ Check for unlinked formulae safely
echo "🔗 Checking for unlinked formulae... (No soldier left behind!)"

unlinked_formulae=()

for formula in ${(f)"$(brew list --formula)"}; do
    echo "🔎 Inspecting: $formula"

    info="$(brew info --formula "$formula" 2>/dev/null || true)"

    # Skip keg-only formulae because they are intentionally not globally linked
    if printf '%s\n' "$info" | grep "keg-only" >/dev/null; then
        echo "⏭️  Skipping keg-only formula: $formula"
        continue
    fi

    # Avoid grep -q because it can close the pipe early and trigger Homebrew broken-pipe errors
    if printf '%s\n' "$info" | grep "Linked" >/dev/null; then
        echo "✅ Linked: $formula"
    else
        echo "⚠️ Possibly unlinked: $formula"
        unlinked_formulae+=("$formula")
    fi
done

if [[ ${#unlinked_formulae[@]} -gt 0 ]]; then
    echo "🛠️ Found ${#unlinked_formulae[@]} unlinked non-keg-only formulae."

    for formula in "${unlinked_formulae[@]}"; do
        echo "🔄 Dry-run relink check: $formula"

        if brew link --overwrite --dry-run "$formula"; then
            echo "🔗 Relinking: $formula"
            brew link --overwrite --verbose "$formula" \
                && echo "✅ $formula successfully relinked!" \
                || echo "❌ Failed to relink: $formula"
        else
            echo "⚠️ Dry-run failed for $formula. Skipping automatic relink."
        fi
    done

    echo "🎯 Relinking operation complete!"
else
    echo "✅ No problematic unlinked formulae detected."
fi

# 8️⃣ Run a full diagnostic check
echo "🩺 Running Homebrew doctor... (Paging Dr. Brew!)"
brew doctor && echo "💊 No health issues detected!" || echo "🚑 Doctor found some issues. Follow the prescription."

# 9️⃣ Verify all installed formulas and casks
echo "🔍 Listing installed packages for final verification..."
brew list --verbose
brew info --installed --json=v2 > /tmp/brew_info.json

# 🔟 Extra: Clean up logs & cached downloads
echo "🧼 Removing stale logs and caches..."
brew cleanup -s
echo "✅ Logs & caches purged!"

# ==========================================
# 📦 NPM SECTION
# ==========================================

echo ""
echo "=========================================="
echo "📦 NPM GLOBAL PACKAGE ANNIHILATION MODE"
echo "=========================================="
echo ""

# 1️⃣ Check if npm is installed
if command -v npm &> /dev/null; then
    echo "🔍 npm detected! Version: $(npm --version)"

    # 2️⃣ Check for outdated global packages
    echo "📊 Scanning for outdated global packages..."
    npm outdated -g --depth=0 || echo "✅ All global packages are current!"

    # 3️⃣ Update all global packages with verbose output
    echo "⚡ UNLEASHING GLOBAL NPM UPDATE... (No package survives outdated!)"
    npm update -g --verbose && echo "🎉 Global npm packages obliterated and rebuilt!" || echo "❌ npm update encountered resistance. Check logs!"

    # 4️⃣ Clean npm cache
    echo "🧹 Cleaning npm cache... (Purging the old guard)"
    npm cache clean --force && echo "✨ npm cache sanitized!" || echo "⚠️ Cache cleanup had issues."

    # 5️⃣ Verify npm installation integrity
    echo "🩺 Running npm doctor... (Health check engaged!)"
    npm doctor || echo "⚠️ npm doctor found some issues. Review output!"

    # 6️⃣ List all global packages for verification
    echo "📋 Final global package inventory:"
    npm list -g --depth=0

else
    echo "⚠️ npm not found on this system. Skipping npm maintenance."
fi

# ==========================================
# 🦀 RUST SECTION
# ==========================================

echo ""
echo "=========================================="
echo "🦀 RUST TOOLCHAIN FORGE MODE"
echo "=========================================="
echo ""

# 1️⃣ Check if rustup is installed
if command -v rustup &> /dev/null; then
    echo "🔍 rustup detected! Version: $(rustup --version)"

    # 2️⃣ Check current Rust version
    echo "📊 Current Rust installation:"
    rustc --version || echo "⚠️ rustc not found!"
    cargo --version || echo "⚠️ cargo not found!"

    # 3️⃣ Check for updates
    echo "🔎 Checking for Rust toolchain updates..."
    rustup check

    # 4️⃣ Update rustup itself
    echo "⚡ Updating rustup installer... (Meta-update engaged!)"
    rustup self update && echo "✅ rustup self-updated successfully!" || echo "❌ rustup self-update failed!"

    # 5️⃣ Update all installed toolchains
    echo "🔥 UPDATING ALL RUST TOOLCHAINS... (Forging the latest and greatest!)"
    rustup update --verbose && echo "🎉 Rust toolchains updated and ready!" || echo "❌ Rust update encountered issues!"

    # 6️⃣ Show installed toolchains
    echo "📋 Installed Rust toolchains:"
    rustup show

    # 7️⃣ Update cargo-installed binaries (if cargo-update is installed)
    if command -v cargo-install-update &> /dev/null; then
        echo "📦 Updating cargo-installed binaries..."
        cargo install-update -a && echo "✅ Cargo binaries updated!" || echo "⚠️ Some cargo binaries failed to update."
    else
        echo "💡 Tip: Install 'cargo-update' for automatic binary updates (cargo install cargo-update)"
    fi

    # 8️⃣ Clean up old toolchains and cached data
    echo "🧹 Cleaning up old Rust artifacts..."
    rustup cleanup && echo "✨ Rust cleanup complete!" || echo "⚠️ Cleanup had issues."

else
    echo "⚠️ rustup not found on this system. Skipping Rust maintenance."
fi

# ==========================================
# 🍎 macOS SYSTEM UPDATES
# ==========================================

echo ""
echo "=========================================="
echo "🍎 macOS SYSTEM UPDATE ASSAULT MODE"
echo "=========================================="
echo ""

# 1️⃣ Check for available updates
echo "🔍 Scanning for macOS system updates..."
softwareupdate --list || echo "✅ No updates available (already cutting edge!)"

# 2️⃣ Install all available updates
echo "⚡ DEPLOYING ALL macOS UPDATES... (System-wide upgrade imminent!)"
echo "⚠️ This requires sudo access. You may be prompted for your password."
sudo softwareupdate --install --all --verbose && echo "🎉 macOS updates installed successfully!" || echo "⚠️ Some updates failed or require restart."

# 3️⃣ Check if restart is required
if softwareupdate --list 2>&1 | grep -q "restart"; then
    echo "🔄 ⚠️ RESTART REQUIRED to complete updates!"
else
    echo "✅ No restart needed. System is good to go!"
fi

# ==========================================
# 🏁 FINAL SYSTEM STATUS
# ==========================================

echo ""
echo "=========================================="
echo "🏁 MAINTENANCE COMPLETE - SYSTEM STATUS"
echo "=========================================="
echo ""

echo "📊 Final System Inventory:"
echo "  🍺 Homebrew: $(brew --version | head -n 1)"
command -v node &> /dev/null && echo "  📦 Node.js: $(node --version)" || echo "  📦 Node.js: Not installed"
command -v npm &> /dev/null && echo "  📦 npm: $(npm --version)" || echo "  📦 npm: Not installed"
command -v rustc &> /dev/null && echo "  🦀 Rust: $(rustc --version)" || echo "  🦀 Rust: Not installed"
echo "  🍎 macOS: $(sw_vers -productVersion)"

echo ""
echo "🎬 MAXIMUM SYSTEM OVERHAUL COMPLETE! 🚀"
echo "Your Mac is now cleaner, faster, and bulletproof. 🍻"
echo "=========================================="
