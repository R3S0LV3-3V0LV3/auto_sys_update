-- System Maintenance Runner AppleScript
-- This script runs the comprehensive maintenance script on startup

on run
	try
		-- Display notification that maintenance is starting
		display notification "Starting system maintenance..." with title "System Overhaul" sound name "Glass"

		-- Path to the maintenance script (same directory as this runner)
		set scriptPath to POSIX path of (path to me)
		set scriptDir to (do shell script "dirname " & quoted form of scriptPath) & "/"
		set maintenanceScript to scriptDir & "system_maintenance.sh"

		-- Make sure the script is executable
		do shell script "chmod +x " & quoted form of maintenanceScript

		-- Run the maintenance script in Terminal with full output
		tell application "Terminal"
			activate
			set newWindow to do script "echo '🚀 SYSTEM MAINTENANCE INITIATED ON STARTUP' && " & quoted form of maintenanceScript & " 2>&1 | tee ~/system_maintenance_$(date +%Y%m%d_%H%M%S).log"

			-- Wait for the script to complete
			repeat
				delay 1
				if not busy of newWindow then exit repeat
			end repeat

			-- Notify completion
			do script "echo '' && echo '✅ Maintenance log saved to ~/system_maintenance_*.log' && echo 'Press any key to close...' && read -n 1" in newWindow
		end tell

		-- Display completion notification
		display notification "System maintenance completed! Check Terminal for details." with title "System Overhaul Complete" sound name "Hero"

	on error errMsg number errNum
		-- Handle errors gracefully
		display notification "Error: " & errMsg with title "Maintenance Failed" sound name "Basso"
		log "Error " & errNum & ": " & errMsg
	end try
end run
