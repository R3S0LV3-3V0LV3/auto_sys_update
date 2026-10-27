"""Bounded filesystem discovery. Fast modes execute no managers or startup files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import tomllib

from .core import expanded, read_json, redact, display_path

COMMANDS = "brew python python3 pip pip3 uv uvx pipx pyenv node npm npx corepack nvm rustup rustc cargo mise asdf proto fnm volta bun deno go ruby gem rbenv rvm java jenv sdk mas codex claude".split()
MANIFESTS = {".nvmrc", ".node-version", ".python-version", ".ruby-version", ".tool-versions", "rust-toolchain", "rust-toolchain.toml", "pyproject.toml", "package.json", "mise.toml", ".mise.toml", "go.mod", "Gemfile", "Cargo.toml"}
EXCLUDED = {".git", ".venv", "venv", "node_modules", "vendor", "target", "dist", "build", "Library", ".next"}


def directories(path, limit=300):
    return sorted(str(p) for p in Path(path).glob("*") if p.is_dir())[:limit]


def small_text(path, limit=1024 * 1024):
    p = Path(path)
    if p.is_symlink() or p.stat().st_size > limit:
        raise ValueError("Symlink/oversized source excluded")
    return p.read_text(errors="replace")


def package_inventory(prefix):
    root = Path(prefix) / "lib/node_modules"
    packages = {}
    for path in list(root.glob("*/package.json"))[:300] + list(root.glob("@*/*/package.json"))[:300]:
        try:
            data = read_json(path)
            name = data.get("name", str(path.parent.relative_to(root)))
            packages[name] = {k: data.get(k) for k in ("version", "engines", "bin")}
        except (ValueError, OSError):
            packages[str(path.parent)] = {"unreadable": True}
    return packages


def command_owner(path):
    p, real = Path(path), Path(path).resolve()
    parts = real.parts
    if "Cellar" in parts:
        index = parts.index("Cellar")
        return {"owner": "brew", "package": parts[index + 1], "prefix": str(Path(*parts[:index]))}
    if "Caskroom" in parts:
        index = parts.index("Caskroom")
        return {"owner": "brew-cask", "package": parts[index + 1]}
    if "node_modules" in parts:
        index = parts.index("node_modules")
        name = parts[index + 1]
        if name.startswith("@") and len(parts) > index + 2:
            name += "/" + parts[index + 2]
        return {"owner": "npm", "package": name, "prefix": str(Path(*parts[:index - 1]))}
    if ".rustup" in parts or ".cargo" in parts:
        return {"owner": "rustup/cargo"}
    if "claude" in parts and "versions" in parts:
        return {"owner": "native", "package": "claude"}
    if ".pyenv" in parts:
        return {"owner": "pyenv"}
    if ".nvm" in parts and "versions" in parts:
        return {"owner": "nvm"}
    if real.is_relative_to(Path("/System")) or real.is_relative_to(Path("/usr/bin")):
        return {"owner": "Apple/system"}
    return {"owner": "unverified-direct", "note": "File location alone does not establish installer provenance"}


class Inventory:
    def __init__(self, policy):
        self.policy, self.home = policy, policy.home
        self.data = {"architecture": platform.machine(), "commands": {}, "faults": [], "limitations": [
            "Only configured project roots and known manager locations are inspected.",
            "Startup files are inspected as text; dynamic shell code is not executed.",
            "No credentials, auth/settings payloads, or project hooks are loaded."]}

    def fault(self, identity, component, reason, correction, evidence=None, repairable=False):
        self.data["faults"].append(dict(id=identity, component=component, reason=reason, correction=correction,
                                       evidence=evidence, repairable=repairable))

    def scan(self):
        self.commands()
        self.runtimes()
        self.projects()
        self.shell()
        self.jobs()
        self.brew_receipts()
        self.python_default()
        return self.data

    def commands(self):
        dirs = [self.home / p for p in (".local/bin", ".cargo/bin", ".pyenv/bin", ".pyenv/shims", ".rbenv/bin", ".rbenv/shims",
                                        ".bun/bin", ".deno/bin", ".local/share/mise/bin", ".asdf/bin", ".proto/bin", ".local/share/proto/bin", ".volta/bin")]
        dirs += [self.policy.root / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")]
        dirs += [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        self.data["process_path"] = os.environ.get("PATH", "")
        for name in dict.fromkeys(COMMANDS + list(self.policy.data["tools"])):
            paths = [expanded(p, self.home) for p in self.policy.data["discovery"]["manager_paths"].get(name, [])]
            paths += [p / name for p in dirs]
            candidates = []
            for p in dict.fromkeys(paths):
                if p.is_file() and os.access(p, os.X_OK):
                    candidates.append({"path": str(p), "target": str(p.resolve()), **command_owner(p)})
            resolved = shutil.which(name)
            self.data["commands"][name] = {"resolved": resolved, "candidates": candidates, "desired_owner": self.policy.data["owners"].get(name)}

    def first(self, name):
        candidates = self.data["commands"].get(name, {}).get("candidates", [])
        return candidates[0]["path"] if candidates else None

    def runtimes(self):
        h = self.home
        nvm_dir = expanded(os.environ.get("NVM_DIR", str(h / ".nvm")), h)
        scripts = [expanded(p, h) for p in self.policy.data["discovery"]["manager_paths"].get("nvm", [])]
        scripts += [nvm_dir / "nvm.sh", h / ".config/nvm/nvm.sh", h / ".local/share/nvm/nvm.sh"]
        for brew in self.data["commands"]["brew"]["candidates"]:
            scripts.append(Path(brew["path"]).parent.parent / "opt/nvm/nvm.sh")
        script = next((str(p) for p in scripts if p.is_file()), None)
        aliases = {}
        for p in list((nvm_dir / "alias").glob("*"))[:100] + list((nvm_dir / "alias/lts").glob("*"))[:100]:
            if p.is_file() and not p.is_symlink() and p.stat().st_size < 4096:
                aliases[str(p.relative_to(nvm_dir / "alias"))] = p.read_text().strip()
        versions = {}
        for p in (nvm_dir / "versions/node").glob("v*"):
            if p.is_dir() and re.fullmatch(r"v\d+\.\d+\.\d+", p.name):
                versions[p.name] = {"root": str(p), "packages": package_inventory(p),
                                    "node": str(p / "bin/node"), "npm": str(p / "bin/npm"), "npx": str(p / "bin/npx")}
        self.data["nvm"] = {"dir": str(nvm_dir), "script": script, "aliases": aliases, "versions": versions,
                            "git_checkout": bool(script and (Path(script).parent / ".git").exists())}
        prefixes = {str(Path(c["path"]).parent.parent) for c in self.data["commands"]["npm"]["candidates"]}
        self.data["other_npm_prefixes"] = {p: package_inventory(p) for p in sorted(prefixes) if p not in {v["root"] for v in versions.values()}}
        if script and self.data["commands"]["node"]["resolved"]:
            current = Path(self.data["commands"]["node"]["resolved"]).resolve()
            if not current.is_relative_to(nvm_dir) and not current.is_relative_to(self.policy.root):
                self.fault("node.precedence", "node", "Process PATH selects a non-nvm Node", "Use planned managed environment integration or an explicit project selection", str(current))
        self.data["runtimes"] = {
            "uv_python": directories(Path(os.environ.get("UV_PYTHON_INSTALL_DIR", str(h / ".local/share/uv/python")))),
            "pyenv": directories(Path(os.environ.get("PYENV_ROOT", str(h / ".pyenv"))) / "versions"),
            "rbenv": directories(Path(os.environ.get("RBENV_ROOT", str(h / ".rbenv"))) / "versions"),
            "rust": directories(Path(os.environ.get("RUSTUP_HOME", str(h / ".rustup"))) / "toolchains"),
            "mise": directories(h / ".local/share/mise/installs"), "asdf": directories(h / ".asdf/installs"),
            "rvm": directories(h / ".rvm/rubies"), "sdkman": directories(h / ".sdkman/candidates"),
            "java": directories(h / "Library/Java/JavaVirtualMachines") + directories("/Library/Java/JavaVirtualMachines"),
        }
        self.data["isolated_tools"] = {}
        for owner, root in (("uv", h / ".local/share/uv/tools"), ("pipx", h / ".local/share/pipx/venvs"), ("pipx-legacy", h / ".local/pipx/venvs")):
            entries = {}
            for folder in directories(root):
                p = Path(folder)
                receipt = p / ("uv-receipt.toml" if owner == "uv" else "pipx_metadata.json")
                try:
                    metadata = tomllib.loads(small_text(receipt)) if owner == "uv" else read_json(receipt, {})
                    # Receipt can include sources; redact credentials before serialising any evidence.
                    entries[p.name] = {"root": str(p), "metadata": json.loads(redact(json.dumps(metadata))), "interpreter": str((p / "bin/python").resolve())}
                except (ValueError, OSError):
                    entries[p.name] = {"root": str(p), "unreadable": True}
            self.data["isolated_tools"][owner] = entries
        settings = Path(os.environ.get("RUSTUP_HOME", str(h / ".rustup"))) / "settings.toml"
        try:
            d = tomllib.loads(small_text(settings))
            self.data["rust_settings"] = {k: d.get(k) for k in ("default_toolchain", "profile", "overrides")}
        except (ValueError, OSError):
            self.data["rust_settings"] = None
        receipt = Path(os.environ.get("CARGO_HOME", str(h / ".cargo"))) / ".crates2.json"
        try:
            self.data["cargo_installs"] = list(read_json(receipt, {}).get("installs", {}))
        except (ValueError, OSError):
            self.data["cargo_installs"] = None

    def projects(self):
        cfg = self.policy.data["discovery"]
        result = {"roots": cfg["project_roots"], "records": [], "directories": 0, "truncated": False, "exclusions": []}
        for source in cfg["project_roots"]:
            root = expanded(source, self.home)
            if root == self.home or root == Path("/"):
                raise ValueError("Project discovery roots must be narrower than ~ or /")
            if not root.is_dir():
                result["exclusions"].append({"path": str(root), "reason": "root unavailable"})
                continue
            for folder, subdirs, files in os.walk(root, followlinks=False):
                if result["directories"] >= cfg["limit"]:
                    result["truncated"] = True
                    break
                result["directories"] += 1
                depth = len(Path(folder).relative_to(root).parts)
                subdirs[:] = sorted(d for d in subdirs if d not in EXCLUDED and not d.startswith(".") and not (Path(folder) / d).is_symlink()) if depth < cfg["depth"] else []
                for name in sorted(MANIFESTS & set(files)):
                    path = Path(folder) / name
                    rec = {"path": str(path), "kind": name}
                    try:
                        text = small_text(path)
                        if name in {".nvmrc", ".node-version", ".python-version", ".ruby-version", ".tool-versions", "rust-toolchain"}:
                            rec["selector"] = text.strip()[:8192]
                        elif name == "package.json":
                            data = json.loads(text)
                            rec.update({k: data.get(k) for k in ("engines", "packageManager", "volta")})
                        elif name.endswith(".toml"):
                            data = tomllib.loads(text)
                            if name == "rust-toolchain.toml":
                                rec["toolchain"] = {k: v for k, v in data.get("toolchain", {}).items() if k in {"channel", "components", "targets", "profile"}}
                            elif name == "pyproject.toml":
                                rec["requires_python"] = data.get("project", {}).get("requires-python")
                            elif name in {"mise.toml", ".mise.toml"}:
                                rec["tools"] = data.get("tools", {})
                        result["records"].append(rec)
                    except (ValueError, OSError) as exc:
                        result["exclusions"].append({"path": str(path), "reason": str(exc)})
            if result["truncated"]:
                break
        self.data["projects"] = result

    def shell(self):
        result = {"files": {}, "dynamic_resolution": "unverified; startup scripts were not executed", "order": [
            "/etc/zshenv", "~/.zshenv", "/etc/zprofile (login; path_helper)", "~/.zprofile (login)",
            "/etc/zshrc (interactive)", "~/.zshrc (interactive)", "/etc/zlogin and ~/.zlogin (login)"]}
        paths = [Path("/etc/zprofile")] + [self.home / n for n in (".zshenv", ".zprofile", ".zshrc", ".zlogin", ".profile", ".bash_profile", ".bashrc", ".config/macbook-rebuild/path.zsh")]
        for p in paths:
            if not p.is_file():
                continue
            records = []
            for number, line in enumerate(small_text(p).splitlines(), 1):
                if re.search(r"(?i)password|token|secret|credential|api.?key|authorization", line):
                    continue
                if re.search(r"PATH|path_helper|\b(?:nvm|pyenv|mise|proto)\b|\bsource\b|alias\s+(?:python|pip|node|npm|npx|codex|claude)|(?:python3?|pip3?|node|npm|npx|codex|claude)\s*\(\)", line):
                    records.append({"line": number, "text": line[:500]})
            result["files"][display_path(p, self.home)] = records
        self.data["shell"] = result
        if any("pyenv init" in r["text"] for records in result["files"].values() for r in records):
            self.fault("shell.competing-python", "integration", "pyenv initialization can override the selected uv user default", "Review bounded startup migration; preserve explicit pyenv project use without global shim competition")

    def jobs(self):
        result = []
        labels = set(self.policy.data["integration"]["legacy_jobs"] + ["com.devsys.maintenance"])
        for directory in (self.home / "Library/LaunchAgents", Path("/Library/LaunchAgents"), Path("/Library/LaunchDaemons")):
            for p in directory.glob("*.plist"):
                if not any(label in p.name for label in labels):
                    continue
                try:
                    d = plistlib.loads(p.read_bytes())
                    result.append({"path": str(p), **{k: d.get(k) for k in ("Label", "ProgramArguments", "RunAtLoad", "StartCalendarInterval")}})
                except (ValueError, OSError, plistlib.InvalidFileException) as exc:
                    result.append({"path": str(p), "invalid": str(exc)})
        self.data["jobs"] = {"files": result, "loaded_jobs": "unverified; use explicit deep diagnosis to query launchd", "cron": "not read (can contain secrets); review legacy scheduling manually if used"}
        if any(d.get("Label") != "com.devsys.maintenance" for d in result):
            self.fault("jobs.legacy", "integration", "Legacy maintenance job may compete with the unified policy", "Review schedule migration before enabling the unified job", result)

    def brew_receipts(self):
        prefixes = {}
        for candidate in self.data["commands"]["brew"]["candidates"]:
            prefix = Path(candidate["path"]).parent.parent
            if str(prefix) in prefixes:
                continue
            formulae = {}
            for rack in (prefix / "Cellar").glob("*"):
                if not rack.is_dir():
                    continue
                versions = []
                dependencies = set()
                for keg in rack.glob("*"):
                    receipt = keg / "INSTALL_RECEIPT.json"
                    if not receipt.is_file():
                        continue
                    try:
                        d = read_json(receipt)
                        runtime = d.get("runtime_dependencies", [])
                        dependencies.update(dep["full_name"] for dep in runtime if "full_name" in dep)
                        versions.append({"version": keg.name, "path": str(keg), "runtime_dependencies": runtime})
                    except (ValueError, OSError):
                        versions.append({"version": keg.name, "path": str(keg), "unreadable_receipt": True})
                formulae[rack.name] = {"installed": versions, "dependencies": sorted(dependencies),
                    "pinned": (prefix / "var/homebrew/pinned" / rack.name).exists(),
                    "opt_target": str((prefix / "opt" / rack.name).resolve()), "metadata": "installed receipts; not current upstream formula definitions"}
            prefixes[str(prefix)] = {"executable": candidate["path"], "formulae": formulae,
                "casks": {p.name: [v.name for v in p.glob("*") if v.is_dir()] for p in (prefix / "Caskroom").glob("*") if p.is_dir()}}
        self.data["brew"] = prefixes

    def python_default(self):
        state = read_json(self.policy.root / "selection.json", {})
        self.data["selection"] = state
        selected = state.get("python", {}).get("current")
        if not selected or not (Path(selected) / "bin/python").is_file():
            self.fault("python.default-missing", "python", "Managed casual Python environment is not selected or its interpreter is missing", "devsys install --only python --apply", selected, True)
        else:
            metadata = read_json(Path(selected) / "devsys-generation.json", {})
            self.data["python_default"] = {"path": selected, "base_interpreter": str((Path(selected) / "bin/python").resolve()), "metadata": metadata}
        for consumer in self.policy.data["consumers"]:
            for source, expected in consumer.get("paths", {}).items():
                path = expanded(source, self.home)
                if not path.exists() or path.resolve() != expanded(expected, self.home):
                    self.fault("consumer." + consumer["name"], "consumer", "Required executable/library resolution differs", "Review explicit consumer migration; do not change global links", {"path": str(path), "expected": expected, "actual": str(path.resolve())})
