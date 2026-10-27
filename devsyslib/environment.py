"""Runtime selection and immutable Python generations. Never relocate a virtualenv."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

from .core import REPO, CommandFailure, atomic_write, expanded, fingerprint, read_json, write_json

SNAPSHOT_CODE = "import importlib.metadata as m,json;print(json.dumps({d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions() if d.metadata['Name']},sort_keys=True))"


def numeric(version):
    return tuple(map(int, version.lstrip("v").split(".")))


def python_matches(version, request):
    if re.fullmatch(r"\d+(?:\.\d+){0,2}", request):
        return numeric(version)[:len(request.split("."))] == numeric(request)
    for part in request.split(","):
        m = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*(\d+(?:\.\d+){0,2})(\.\*)?\s*", part)
        if not m:
            raise ValueError(f"Unsupported interpreter selector {request}; use an explicit installed version or project environment")
        operator, bound, wildcard = m.groups()
        target = numeric(bound)
        actual = numeric(version)[:len(target)] if operator == "==" and wildcard else numeric(version)
        target = target if operator == "==" and wildcard else target + (0,) * (3 - len(target))
        if not {">=": actual >= target, "<=": actual <= target, "==": actual == target,
                ">": actual > target, "<": actual < target}[operator]:
            return False
    return True


class Environment:
    def __init__(self, policy, inventory, session=None):
        self.policy, self.inv, self.session = policy, inventory, session
        self.root, self.home = policy.root, policy.home

    def selections(self):
        return read_json(self.root / "selection.json", {})

    def select(self, component, target, metadata=None):
        if not self.session or not self.session.apply:
            raise ValueError("Changing a default selection requires --apply")
        state = self.selections()
        old = state.get(component, {})
        if old.get("current") == str(target):
            return False
        history = list(dict.fromkeys([old.get("current")] + old.get("history", [])))
        state[component] = {"current": str(target), "previous": old.get("current"), "history": [v for v in history if v],
                            "metadata": metadata or {}}
        write_json(self.root / "selection.json", state)
        return True

    def nearest(self, cwd, filenames):
        p = Path(cwd).absolute()
        for _ in range(30):
            for name in filenames:
                candidate = p / name
                if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size < 1024 * 1024:
                    return candidate
            if p == p.parent or p == self.home:
                break
            p = p.parent
        return None

    def python_interpreter(self, cwd, project=None):
        if project:
            path = expanded(project, self.home) / ".venv/bin/python"
            if not path.is_file():
                raise ValueError(f"Explicit project environment is missing: {path}; create it with its project owner")
            return path, "explicit project environment"
        active = os.environ.get("VIRTUAL_ENV")
        if active:
            path = Path(active) / "bin/python"
            if not path.is_file():
                raise ValueError("VIRTUAL_ENV selects a missing interpreter; deactivate or repair that environment")
            return path, "activated virtual environment"
        requested = os.environ.get("PYENV_VERSION") or os.environ.get("UV_PYTHON")
        pin = self.nearest(cwd, [".python-version"])
        if not requested and pin:
            requested = pin.read_text().strip()
        if requested:
            if Path(requested).is_absolute():
                path = Path(requested)
                if not path.is_file():
                    raise ValueError("Explicit interpreter path does not exist")
                return path, "explicit interpreter path"
            # Preserve an installed pyenv selection without initializing global shims.
            pyenv = Path(os.environ.get("PYENV_ROOT", str(self.home / ".pyenv"))) / "versions" / requested / "bin/python"
            if pyenv.is_file():
                return pyenv, f"existing pyenv project selection {requested}"
            installed = []
            for folder in self.inv.get("runtimes", {}).get("uv_python", []):
                m = re.search(r"cpython-(\d+\.\d+\.\d+)-", Path(folder).name)
                if m and python_matches(m.group(1), requested) and (Path(folder) / "bin/python3").is_file():
                    installed.append((numeric(m.group(1)), Path(folder) / "bin/python3"))
            if installed:
                return max(installed)[1], f"declared Python {requested}; casual packages excluded"
            raise ValueError(f"Declared Python {requested} is unavailable; install that exact requirement through its owner, or explicitly select the project environment")
        manifest = self.nearest(cwd, ["pyproject.toml"])
        if manifest:
            import tomllib
            requirement = tomllib.loads(manifest.read_text()).get("project", {}).get("requires-python")
            if requirement:
                raise ValueError(f"Project declares requires-python={requirement}; activate its environment or use exec --project PATH. Casual package visibility is not injected")
        selected = self.selections().get("python", {}).get("current")
        path = Path(selected) / "bin/python" if selected else None
        if not path or not path.is_file():
            raise ValueError("Casual Python is not installed; review plan --operation install --only python")
        return path, "managed casual Python generation"

    def node_version(self, selector, aliases=None):
        versions = self.inv["nvm"]["versions"]
        aliases = aliases or self.inv["nvm"]["aliases"]
        visited = set()
        while selector in aliases:
            if selector in visited:
                raise ValueError("Cycle in nvm aliases")
            visited.add(selector)
            selector = aliases[selector]
        if selector in {"node", "stable"}:
            choices = list(versions)
        elif re.fullmatch(r"v?\d+(?:\.\d+){0,2}", selector):
            prefix = numeric(selector)
            choices = [v for v in versions if numeric(v)[:len(prefix)] == prefix]
        else:
            raise ValueError(f"Selector {selector} needs native nvm resolution/metadata refresh; no guessed fallback")
        if not choices:
            raise ValueError(f"Required Node {selector} is not installed")
        return max(choices, key=numeric)

    def node_root(self, cwd):
        nvm = self.inv["nvm"]
        active = os.environ.get("NVM_BIN")
        if active:
            root = Path(active).parent
            if str(root) in {v["root"] for v in nvm["versions"].values()}:
                return root, "explicit nvm use selection"
        pin = self.nearest(cwd, [".nvmrc", ".node-version"])
        if pin:
            selector = pin.read_text().strip()
            return Path(nvm["versions"][self.node_version(selector)]["root"]), f"declared project selector {selector}"
        selected = self.selections().get("node", {}).get("current")
        if selected:
            if not (Path(selected) / "bin/node").is_file():
                raise ValueError("Selected Node executable is missing")
            return Path(selected), "configured user Node selection"
        selector = nvm["aliases"].get("default")
        if not selector:
            raise ValueError("No Node default is selected; configure node.default and switch_default explicitly")
        return Path(nvm["versions"][self.node_version(selector)]["root"]), "existing nvm default preserved"

    def resolve(self, name, cwd=None, project=None):
        cwd = Path(cwd or os.getcwd())
        if name in {"python", "python3", "pip", "pip3"}:
            path, reason = self.python_interpreter(cwd, project)
            return [str(path)] + (["-m", "pip"] if name.startswith("pip") else []), reason, {}
        if name in {"node", "npm", "npx", "corepack", "yarn", "pnpm"}:
            root, reason = self.node_root(cwd)
            path = root / "bin" / name
            if not path.exists():
                raise ValueError(f"{name} is not installed in selected Node {root.name}; no cross-manager fallback")
            prefix = os.environ.get("npm_config_prefix") or os.environ.get("NPM_CONFIG_PREFIX")
            if name in {"npm", "npx"} and prefix and Path(prefix).resolve() != root.resolve():
                raise ValueError("npm environment prefix conflicts with selected nvm runtime; remove that explicit conflict before use")
            return [str(path)], reason, {"PATH": str(root / "bin") + os.pathsep + os.environ.get("PATH", "")}
        selected = self.selections().get("tool:" + name, {})
        if selected.get("current"):
            path = Path(selected["current"])
            if not path.exists():
                raise ValueError(f"Selected {name} entry point is missing; run diagnosis")
            env = {"DISABLE_AUTOUPDATER": "1"}
            runtime = selected.get("metadata", {}).get("runtime")
            if runtime:
                if not (Path(runtime) / "bin/node").exists():
                    raise ValueError("Essential CLI's dedicated Node runtime is missing")
                env["PATH"] = str(Path(runtime) / "bin") + os.pathsep + os.environ.get("PATH", "")
            return [str(path)], "selected essential tool; independent of project Node", env
        # Never resolve our wrapper recursively; do not silently substitute a configured essential tool.
        if name in self.policy.data["tools"]:
            raise ValueError(f"No verified {name} selection; diagnose and install the selected owner")
        paths = [p for p in os.environ.get("PATH", "").split(os.pathsep) if Path(p) != self.root / "bin"]
        executable = shutil.which(name, path=os.pathsep.join(paths))
        if not executable:
            raise ValueError(f"Command unavailable: {name}")
        return [executable], "inherited command owner preserved", {}

    def process_env(self, cwd=None, project=None):
        env = dict(os.environ)
        additions = [str(self.root / "bin")]
        if project:
            project = expanded(project, self.home)
            if not (project / ".venv/bin/python").is_file():
                raise ValueError("Explicit project .venv is unavailable")
            env["VIRTUAL_ENV"] = str(project / ".venv")
            additions.insert(0, str(project / ".venv/bin"))
        elif env.get("VIRTUAL_ENV"):
            additions.insert(0, str(Path(env["VIRTUAL_ENV"]) / "bin"))
        env["PATH"] = os.pathsep.join(dict.fromkeys(additions + env.get("PATH", "").split(os.pathsep)))
        return env

    def launchers(self):
        names = ["devsys", "python", "python3", "pip", "pip3", "node", "npm", "npx", "corepack", "yarn", "pnpm"] + list(self.policy.data["tools"])
        changed = False
        for name in dict.fromkeys(names):
            path = self.root / "bin" / name
            script = "#!/bin/sh\n# devsys-managed launcher\nexec " + shlex.join([
                str(Path(sys.executable).resolve()), "-B", str(REPO / "devsys.py"), "--config", str(self.policy.path),
            ] + ([] if name == "devsys" else ["exec", "--managed-command", name, "--"])) + ' "$@"\n'
            if path.exists() and (path.is_symlink() or "# devsys-managed launcher" not in path.read_text(errors="replace")):
                raise ValueError(f"Unmanaged executable occupies {path}; resolve owner before replacement")
            changed |= atomic_write(path, script, 0o755)
        self.session.item("launchers", "updated" if changed else "unchanged", "Managed executable launchers converge; no files in another manager's bin directory were overwritten")

    def requirements(self, cfg=None):
        cfg = cfg or self.policy.data["python"]
        profiles = read_json(REPO / "config/python/profiles.json")
        requirements, constraints, imports = [], [], []
        for name in cfg["profiles"]:
            if name not in profiles:
                raise ValueError(f"Unknown Python profile {name}")
            group = profiles[name]
            if not group.get("supported", True):
                raise ValueError(f"Profile {name} requires explicit compatibility work: {group['reason']}")
            requirements += group["requirements"]
            constraints += group.get("constraints", [])
            imports += group.get("imports", [])
        requirements += cfg["additions"]
        for req in requirements + constraints:
            if not isinstance(req, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.,\[\]<>=!~+* -]*", req):
                raise ValueError("Only registry requirements are supported; URL/editable/local inputs need a separate reviewed build policy")
        imports += cfg["imports"]
        if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name) for name in imports):
            raise ValueError("Imports must be module names")
        return {"requirements": sorted(set(requirements)), "constraints": sorted(set(constraints)), "imports": sorted(set(imports)), "selector": cfg["selector"]}

    def snapshot(self, interpreter):
        return self.session.json([interpreter, "-I", "-B", "-c", SNAPSHOT_CODE])

    def check_python(self, path, imports=None):
        imports = imports if imports is not None else self.requirements()["imports"]
        code = "import importlib,json,sys; mods=" + repr(imports) + "; print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,'imports':{n:getattr(importlib.import_module(n),'__file__',None) for n in mods}}))"
        return self.session.json([Path(path) / "bin/python", "-I", "-B", "-c", code])

    def drift(self, current):
        old = read_json(Path(current) / "devsys-generation.json", {})
        actual = self.snapshot(Path(current) / "bin/python")
        expected = old.get("packages", {})
        return {n: {"recorded": expected.get(n), "actual": actual.get(n)} for n in set(actual) | set(expected) if expected.get(n) != actual.get(n)}

    def build_python(self, cfg=None, force=False):
        session = self.session
        cfg = cfg or self.policy.data["python"]
        inputs = self.requirements(cfg)
        current = self.selections().get("python", {}).get("current")
        previous_meta = read_json(Path(current) / "devsys-generation.json", {}) if current else {}
        if current and (Path(current) / "bin/python").is_file():
            drift = self.drift(current)
            if drift and not cfg["allow_untracked"]:
                session.item("python", "deferred", "Untracked package/version drift detected; adopt explicit top-level additions with python add, or explicitly allow replacement. Current generation retained", drift=drift)
                return False
            if not force and previous_meta.get("input_sha256") == fingerprint(inputs):
                self.check_python(current, inputs["imports"])
                session.item("python", "unchanged", "Configured environment and imports already verified; no rebuild")
                return True
        uv = next((c["path"] for c in self.inv["commands"]["uv"]["candidates"] if not Path(c["path"]).is_relative_to(self.root / "bin")), None)
        if not uv:
            session.item("python", "deferred", "uv is missing. Install its verified upstream distribution, or configure an existing absolute manager path; no unverified bootstrap script is run")
            return False
        data = session.json([uv, "python", "list", "--only-downloads", "--output-format", "json", "--no-config"])
        candidates = set()
        for entry in data:
            if entry.get("implementation") not in {None, "cpython"}:
                continue
            version = str(entry.get("version", ""))
            if re.fullmatch(r"\d+\.\d+\.\d+", version) and not any(tag in str(entry.get("key", "")) for tag in ("freethreaded", "debug")):
                if cfg["selector"] == "latest-compatible" or python_matches(version, cfg["selector"]):
                    candidates.add(version)
        if not candidates:
            session.item("python", "deferred", "Installed uv manifest has no matching stable CPython. Update the owning uv installation or revise the explicit selector")
            return False
        req_path, constraints_path = session.run_dir / "requirements.in", session.run_dir / "constraints.txt"
        atomic_write(req_path, "\n".join(inputs["requirements"]) + "\n")
        atomic_write(constraints_path, "\n".join(inputs["constraints"]) + "\n")
        lock = session.run_dir / "requirements.lock"
        selected = None
        # Bounded attempts: newest patch of each available minor, up to six minors; never install while solving.
        minors = set()
        for version in sorted(candidates, key=numeric, reverse=True):
            if numeric(version)[:2] in minors:
                continue
            minors.add(numeric(version)[:2])
            if len(minors) > 6:
                break
            rc, _ = session.run([uv, "pip", "compile", req_path, "--constraint", constraints_path,
                                 "--generate-hashes", "--only-binary", ":all:", "--no-build", "--prerelease", "disallow",
                                 "--python-version", version, "--output-file", lock, "--no-config"],
                                change=True, check=False, expected_failure=True)
            if not rc:
                selected = version
                break
            session.item("python-resolution", "deferred", f"No wheel-only solution for {version}; details retained in command log")
        if not selected:
            session.item("python", "failed", "No compatible baseline resolved; no interpreter/default changed")
            return False
        session.run([uv, "python", "install", selected, "--no-config"], change=True, env={"UV_PYTHON_DOWNLOADS": "manual"})
        _, out = session.run([uv, "python", "find", selected, "--managed-python", "--no-python-downloads", "--no-config"])
        base = Path(out.strip()).resolve()
        _, version_out = session.run([base, "-I", "-B", "-c", "import platform;print(platform.python_version())"])
        if version_out.strip() != selected:
            raise ValueError("uv returned an interpreter other than the exact selected patch; default unchanged")
        generation = self.root / "python/generations" / (session.run_dir.name + "-" + selected)
        # This is the final installation path. Never rename/move the created venv.
        session.run([uv, "venv", "--seed", "--python", base, generation, "--no-config", "--no-python-downloads"], change=True)
        session.run([uv, "pip", "sync", "--python", generation / "bin/python", "--require-hashes", "--only-binary", ":all:", "--no-build", lock, "--no-config"], change=True)
        session.run([uv, "pip", "check", "--python", generation / "bin/python", "--no-config"])
        checks = self.check_python(generation, inputs["imports"])
        packages = self.snapshot(generation / "bin/python")
        metadata = {"version": selected, "base_interpreter": str(base), "inputs": inputs, "input_sha256": fingerprint(inputs),
                    "packages": packages, "checks": checks, "built_at_final_path": True,
                    "created": dt.datetime.now(dt.timezone.utc).isoformat()}
        write_json(generation / "devsys-generation.json", metadata)
        shutil.copy2(lock, generation / "requirements.lock")
        self.select("python", generation, {"base_interpreter": str(base)})
        session.item("python", "updated", "New generation resolved, built at final path and import/pip checked before selection. Previous environment and exact interpreter retained", current=str(generation), previous=current)
        return True

    def rollback(self, component):
        state = self.selections()
        old = state.get(component, {})
        target = old.get("previous")
        if not target:
            raise ValueError(f"No previous selection recorded for {component}")
        if component == "python":
            metadata = read_json(Path(target) / "devsys-generation.json", {})
            if not metadata or not Path(metadata["base_interpreter"]).is_file():
                raise ValueError("Previous exact base interpreter/environment is unavailable; do not switch")
            self.check_python(target, metadata["inputs"]["imports"])
        elif component == "node":
            self.session.run([Path(target) / "bin/node", "--version"])
        elif component.startswith("tool:"):
            if not Path(target).is_file():
                raise ValueError("Previous CLI executable is missing")
            rule = self.policy.data["tools"][component[5:]]
            self.session.run([target] + rule["smoke"][1:])
        else:
            raise ValueError("Only managed Python, Node and tool selections support pointer recovery; brew/cask rollback is not generic")
        if self.session.apply:
            self.select(component, target, old.get("metadata", {}))
            self.session.item(component, "updated", "Previous selection verified and restored; failed candidate retained")
        else:
            self.session.item(component, "proposed", "Restore previous selection after verification; use --apply", target=target)
