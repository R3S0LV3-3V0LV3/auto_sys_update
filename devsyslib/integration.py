"""Bounded shell integration and a single login LaunchAgent policy."""
from __future__ import annotations

import difflib
import os
from pathlib import Path
import plistlib
import re
import shlex
import sys

from .core import REPO, atomic_write, fingerprint, read_json, write_json

BEGIN = "# >>> devsys managed environment >>>"
END = "# <<< devsys managed environment <<<"
LABEL = "com.devsys.maintenance"


def replace_block(text, block):
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError("Malformed/duplicate devsys startup blocks require manual review")
    if BEGIN in text:
        start = text.index(BEGIN)
        finish = text.index(END, start) + len(END)
        return text[:start] + block.rstrip("\n") + text[finish:]
    return text.rstrip("\n") + ("\n\n" if text else "") + block.rstrip("\n") + "\n"


class Integration:
    def __init__(self, session, inventory, environment):
        self.s, self.p, self.inv, self.env = session, session.policy, inventory, environment

    def loader(self):
        root = shlex.quote(str(self.p.root / "bin"))
        lines = ["# devsys-managed; no project hooks or downloads", "typeset -U path", f"path=({root} $path)",
                 'if [[ -n "${VIRTUAL_ENV:-}" && -d "$VIRTUAL_ENV/bin" ]]; then path=("$VIRTUAL_ENV/bin" $path); fi', "export PATH"]
        # Shell functions keep essential CLIs available even after nvm use prepends another bin directory.
        for name, rule in self.p.data["tools"].items():
            if rule["enabled"]:
                lines.append(f"{name}() {{ command {shlex.quote(str(self.p.root / 'bin' / name))} \"$@\"; }}")
        return "\n".join(lines) + "\n"

    def shell_changes(self):
        loader = self.p.root / "environment.zsh"
        block = BEGIN + "\n" + f"[[ -r {shlex.quote(str(loader))} ]] && source {shlex.quote(str(loader))}" + "\n" + END + "\n"
        changes = []
        for name in (".zprofile", ".zshrc"):
            path = self.p.home / name
            if path.is_symlink():
                raise ValueError(f"Startup file {name} is a symlink; target needs explicit review")
            before = path.read_text() if path.is_file() else ""
            after = before
            # Recognised competing global initialization only. Preserve all other user code.
            if self.p.data["owners"].get("python3") == "uv":
                after = re.sub(r'(?m)^(\s*)eval "\$\(pyenv init - zsh\)"\s*$', r'\1: # devsys: explicit pyenv projects remain supported; uv owns user default', after)
            after = replace_block(after, block)
            if before != after:
                changes.append((path, before, after))
        return loader, changes

    def shell(self):
        loader, changes = self.shell_changes()
        if not self.p.data["integration"]["shell"]:
            self.s.item("integration:shell", "deferred", "Shell integration disabled. Enable integration.shell, inspect the bounded startup diff, then apply. Automation can use devsys exec without profile edits")
            return
        for path, before, after in changes:
            # Persist only edited hunks, never full startup files that might contain credentials.
            diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=str(path), tofile=str(path), n=0))
            self.s.item("integration:" + path.name, "proposed", "Bounded managed block/global-init correction; unrelated user content retained", diff=diff)
        if not self.s.apply:
            return
        from .core import CommandFailure
        # Syntax-check candidate files before any live edit; the loader is never sourced by maintenance.
        candidate = self.s.run_dir / "environment.zsh"
        atomic_write(candidate, self.loader())
        self.s.run(["/bin/zsh", "-n", candidate])
        for index, (path, before, after) in enumerate(changes):
            candidate = self.s.run_dir / f"startup-{index}.zsh"
            atomic_write(candidate, after)
            self.s.run(["/bin/zsh", "-n", candidate])
            if path.exists() and path.read_text() != before:
                raise ValueError("Startup file changed during planning; refusing stale edit")
        atomic_write(loader, self.loader())
        for index, (path, before, after) in enumerate(changes):
            # The reversible patch contains only owned/recognised lines, not surrounding private configuration.
            patch = "".join(difflib.unified_diff(after.splitlines(True), before.splitlines(True), fromfile=str(path), tofile=str(path), n=0))
            atomic_write(self.s.run_dir / "recovery" / f"startup-{index}.reverse.patch", patch)
            atomic_write(path, after, (path.stat().st_mode & 0o777) if path.exists() else 0o600)
        self.s.item("integration:shell", "updated" if changes else "unchanged", "One managed loader configured for login/interactive zsh; explicit automation uses launchers. Reverse patches retained. No project hook executed")

    def plist(self):
        path = self.p.home / "Library/LaunchAgents" / (LABEL + ".plist")
        logs = self.s.state / "launchd"
        data = {"Label": LABEL, "ProgramArguments": [str(Path(sys.executable).resolve()), "-B", str(REPO / "devsys.py"),
                "--config", str(self.p.path), "--state-dir", str(self.s.state), "plan", "--unattended"],
                "RunAtLoad": True, "StartCalendarInterval": {"Hour": 9, "Minute": 0},
                "ProcessType": "Background", "StandardOutPath": str(logs / "report.log"), "StandardErrorPath": str(logs / "error.log"),
                "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.p.home)}}
        return path, data

    def schedule(self):
        path, data = self.plist()
        legacy = [job for job in self.inv["jobs"]["files"] if job.get("Label") != LABEL]
        self.s.observed["schedule_plan"] = {"target": str(path), "plist": data, "legacy": legacy,
                                           "behaviour": "User login/loading and daily reporting; never boot-time privileged maintenance"}
        if not self.p.data["integration"]["schedule"]:
            self.s.item("integration:schedule", "deferred", "Schedule disabled; generated policy is report-only. Legacy jobs must be migrated before enabling")
            return
        if not self.s.apply:
            self.s.item("integration:schedule", "proposed", "Disable/unload recognised idle legacy jobs before installing one report-only LaunchAgent", path=str(path))
            return
        if sys.platform != "darwin":
            raise ValueError("Live LaunchAgent installation is supported only on macOS")
        domain = f"gui/{os.getuid()}"
        # Detect jobs loaded from nonstandard paths as well as files found by bounded discovery.
        loaded = {}
        for label in self.p.data["integration"]["legacy_jobs"] + [LABEL]:
            rc, output = self.s.run(["/bin/launchctl", "print", domain + "/" + label], check=False, accepted=(0, 113, 3))
            if rc and not re.search(r"Could not find service|service.*not found", output, re.I):
                raise ValueError("Cannot establish loaded-job state; no scheduling mutation")
            if not rc:
                if re.search(r"\bstate = running\b", output):
                    raise ValueError(f"{label} is running; wait for completion. No job will be killed")
                loaded[label] = output
                if label != LABEL and not any(j.get("Label") == label for j in legacy):
                    raise ValueError("Loaded legacy job source is outside the known user LaunchAgents path; review migration manually")
        for job in legacy:
            source = Path(job["path"])
            if not source.is_relative_to(self.p.home / "Library/LaunchAgents") or source.is_symlink() or job.get("invalid"):
                raise ValueError("Legacy job is invalid/system-owned/symlinked; explicit migration required")
            old = plistlib.loads(source.read_bytes())
            if old.get("Label") not in self.p.data["integration"]["legacy_jobs"]:
                raise ValueError("Legacy job identity changed")
            atomic_write(self.s.run_dir / "recovery" / source.name, plistlib.dumps(old).decode())
            old["Disabled"] = True
            atomic_write(source, plistlib.dumps(old).decode())
            if old["Label"] in loaded:
                self.s.run(["/bin/launchctl", "bootout", domain + "/" + old["Label"]], change=True)
        contents = plistlib.dumps(data).decode()
        if path.exists() and path.read_text() == contents and LABEL in loaded:
            self.s.item("integration:schedule", "unchanged", "One report-only job already installed; no reload")
            return
        if LABEL in loaded:
            self.s.run(["/bin/launchctl", "bootout", domain + "/" + LABEL], change=True)
        (self.s.state / "launchd").mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(path, contents)
        self.s.run(["/usr/bin/plutil", "-lint", path])
        self.s.run(["/bin/launchctl", "bootstrap", domain, path], change=True)
        self.s.item("integration:schedule", "updated", "Legacy jobs disabled before one canonical reporting job was loaded. Source ADS checkout unchanged")

    def perform(self):
        if self.s.apply:
            self.env.launchers()
        self.shell()
        self.schedule()
