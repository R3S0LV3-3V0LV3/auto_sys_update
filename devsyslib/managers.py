"""Native manager adapters sharing one policy and one action boundary."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil

from .core import REPO, CommandFailure, atomic_write, dependency_closure, fingerprint, read_json, write_json
from .environment import Environment, numeric
from .inventory import Inventory, command_owner, package_inventory


class Managers:
    def __init__(self, session, inventory):
        self.s, self.p, self.inv = session, session.policy, inventory
        self.env = Environment(self.p, self.inv, self.s)
        self.pending_node = None

    def first(self, name):
        return next((c["path"] for c in self.inv["commands"].get(name, {}).get("candidates", [])
                     if not Path(c["path"]).is_relative_to(self.p.root / "bin")), None)

    def refresh_inventory(self):
        self.inv = Inventory(self.p).scan()
        self.env = Environment(self.p, self.inv, self.s)

    def isolated(self, component, operation):
        try:
            operation()
        except (CommandFailure, ValueError, OSError, KeyError, TypeError) as exc:
            self.s.item(component, "failed", f"Stopped dependent operations: {exc}. Independent ecosystems continue")

    def consumers(self, phase, execute=True):
        ok = True
        records = []
        for consumer in self.p.data["consumers"]:
            resolutions = {}
            for source, expected in consumer.get("paths", {}).items():
                from .core import expanded
                path, target = expanded(source, self.p.home), expanded(expected, self.p.home)
                resolutions[source] = str(path.resolve())
                if not path.exists() or path.resolve() != target:
                    self.s.item("consumer:" + consumer["name"], "failed", f"{phase}: required executable/library path differs", expected=str(target), actual=str(path.resolve()))
                    ok = False
            if execute and consumer.get("smoke"):
                rc, _ = self.s.run(consumer["smoke"], cwd=consumer.get("cwd"), check=False)
                ok &= rc == 0
            else:
                self.s.item("consumer:" + consumer["name"], "unverified", "Application smoke not executed or not configured")
            records.append({"phase": phase, "consumer": consumer["name"], "paths": resolutions})
        self.s.observed.setdefault("consumer_checks", []).extend(records)
        return ok

    def nvm(self, *args, change=False):
        nvm = self.inv["nvm"]
        if not nvm["script"]:
            raise ValueError("nvm.sh unavailable; configure discovery.manager_paths.nvm / NVM_DIR")
        shell = 'export NVM_DIR="$1"; . "$2" --no-use || exit $?; shift 2; nvm "$@"'
        return self.s.run(["/bin/bash", "--noprofile", "--norc", "-c", shell, "devsys-nvm", nvm["dir"], nvm["script"], *args], change=change,
                          env={"NVM_NO_COLORS": "1", "NPM_CONFIG_PREFIX": "", "npm_config_prefix": ""})

    def refresh(self, only=None):
        if only in {None, "brew"}:
            for prefix, installed in self.inv["brew"].items():
                def brew_refresh(prefix=prefix, installed=installed):
                    brew = installed["executable"]
                    self.s.run([brew, "update"], change=True)
                    info = self.s.json([brew, "info", "--installed", "--json=v2"])
                    outdated = self.s.json([brew, "outdated", "--json=v2", "--greedy"])
                    self.s.cache("brew:" + prefix, {"info": info, "outdated": outdated, "architecture": platform.machine(), "executable": brew})
                    self.s.item("brew:" + prefix, "updated", "Homebrew metadata refreshed; no packages upgraded")
                self.isolated("brew:" + prefix, brew_refresh)
        if only in {None, "node"}:
            if self.inv["nvm"]["script"]:
                selectors = list(dict.fromkeys(self.p.data["node"]["install"] + ["node", "lts/*"]))
                for selector in selectors:
                    def node_refresh(selector=selector):
                        _, out = self.nvm("version-remote", selector)
                        if not re.fullmatch(r"v\d+\.\d+\.\d+", out.strip()):
                            raise ValueError("Remote selector did not resolve to a stable release")
                        self.s.cache("node:" + selector, out.strip())
                        self.s.item("node:" + selector, "updated", "Remote availability refreshed; no runtime installed", version=out.strip())
                    self.isolated("node:" + selector, node_refresh)
            else:
                self.s.item("node", "unverified", "nvm is absent; remote Node selectors cannot be resolved through the selected owner yet")
            git = shutil.which("git") or "/usr/bin/git"
            def tags_refresh():
                _, out = self.s.run([git, "ls-remote", "--refs", "--tags", "https://github.com/nvm-sh/nvm.git"], change=True)
                versions = re.findall(r"refs/tags/(v\d+\.\d+\.\d+)\s*$", out, re.M)
                if not versions:
                    raise ValueError("No stable upstream nvm tags returned")
                self.s.cache("nvm-release", max(versions, key=numeric))
            self.isolated("nvm-metadata", tags_refresh)
        if only in {None, "rust"}:
            rustup = self.first("rustup")
            if rustup:
                self.isolated("rust-metadata", lambda: self.s.cache("rust-check", self.s.run([rustup, "check"], change=True)[1]))
        if only in {None, "python"}:
            uv = self.first("uv")
            if uv:
                self.isolated("python-metadata", lambda: self.s.cache("python-versions", self.s.json([uv, "python", "list", "--output-format", "json", "--no-config"])))
        if only in {None, "tools"}:
            npm = self.first("npm")
            if npm:
                for name, rule in self.p.data["tools"].items():
                    if rule["owner"] == "npm":
                        self.isolated("tool-metadata:" + name, lambda name=name, rule=rule: self.s.cache("tool:" + name, self.s.json([npm, "view", rule["package"] + "@" + rule["version"], "version", "engines", "--json"], change=True)))
            mas = self.first("mas")
            if mas:
                self.isolated("mas-metadata", lambda: self.s.cache("mas-outdated", self.s.run([mas, "outdated"], change=True)[1]))

    def brew_reason(self, name, formulae, outdated, graph):
        cfg = self.p.data["brew"]
        f = formulae[name]
        roots = set(cfg["protected"]) | {n for n, item in formulae.items() if item.get("pinned")}
        for consumer in self.p.data["consumers"]:
            roots.update(consumer.get("brew", {}))
        protected = dependency_closure(roots, graph)
        deps = dependency_closure([name], graph)
        if deps & protected:
            return "Required/pinned transitive dependency closure protected: " + ", ".join(sorted(deps & protected))
        if any(n != name and name in dependency_closure([n], graph) for n in graph):
            return "Installed dependents require a coordinated migration; no implicit dependent upgrades"
        if any(dep not in formulae for dep in deps):
            return "Current dependency closure contains missing/unknown formulae; plan installation prerequisites first"
        if (deps - {name}) & outdated:
            return "An implicit dependency upgrade would occur; review that dependency separately"
        if any(formulae[d].get("service") for d in deps):
            return "Service lifecycle or persistent data needs an explicit migration policy"
        if not f.get("linked_keg") and not f.get("keg_only"):
            return "Intentionally unlinked formula preserved; no automatic link repair"
        if name.startswith("node") and self.inv["nvm"]["script"]:
            return "Preserve Homebrew Node for its dependents; nvm owns user Node"
        if cfg["updates"] == "report":
            return "Set brew.updates to configured or eligible-leaves to enable this class of updates"
        if cfg["updates"] == "configured" and name not in cfg["formulae"]:
            return "Formula is outside the configured update set"
        if not cfg["allow_unknown_consumers"]:
            return "Unknown consumers remain; configure known consumers and explicitly enable this update class"
        if name not in cfg["checks"]:
            return "No meaningful executable smoke check configured for this formula"
        return None

    def brew(self, operation):
        if not self.inv["brew"]:
            self.s.item("brew", "deferred", "Homebrew absent; bootstrap requires its privileged upstream installation and is outside unattended maintenance")
            return
        for prefix, observed in self.inv["brew"].items():
            self.isolated("brew:" + prefix, lambda prefix=prefix, observed=observed: self.brew_prefix(prefix, observed, operation))

    def brew_prefix(self, prefix, observed, operation):
        cached = self.s.cached("brew:" + prefix)
        if not cached or cached["stale"]:
            self.s.item("brew:" + prefix, "deferred", "Availability metadata absent/stale. Run refresh --only brew --apply; refresh changes manager state", cache=cached)
            return
        meta = cached["value"]
        if meta["architecture"] != platform.machine() or meta["executable"] != observed["executable"]:
            raise ValueError("Homebrew prefix/architecture changed since metadata refresh")
        brew = observed["executable"]
        info = meta["info"]
        formulae = {f.get("full_name", f["name"]): f for f in info["formulae"]}
        graph = {n: set(f.get("dependencies", [])) | {d["full_name"] for i in f.get("installed", []) for d in i.get("runtime_dependencies", [])} for n, f in formulae.items()}
        candidates = {f["name"]: f for f in meta["outdated"].get("formulae", [])}
        self.s.observed.setdefault("brew_metadata", {})[prefix] = {"time": cached["time"], "age_hours": cached["age_hours"], "source": "explicit refresh"}
        # Both recipes and installed transitive requirements are retained in recovery evidence.
        self.s.observed.setdefault("brew_retention", {})[prefix] = {"graph": {n: sorted(v) for n, v in graph.items()}, "before": info}
        if self.s.apply:
            current = self.s.json([brew, "info", "--installed", "--json=v2"])
            versions = lambda data: {f["name"]: sorted(i["version"] for i in f["installed"]) for f in data["formulae"]}
            if versions(current) != versions(info):
                raise ValueError("Installed state changed since metadata refresh; refresh/replan before mutation")
            self.s.run([brew, "missing"])
            self.s.run([brew, "linkage", "--test"])
            for consumer in self.p.data["consumers"]:
                for name, required in consumer.get("brew", {}).items():
                    if required not in [i["version"] for i in formulae.get(name, {}).get("installed", [])]:
                        raise ValueError(f"Required {name} {required} is missing for {consumer['name']}")
        if operation in {"install", "repair"}:
            for name in self.p.data["brew"]["formulae"]:
                if name not in formulae:
                    # Missing dependencies can themselves upgrade/relink existing packages. Require a reviewed recipe.
                    self.s.item("brew-install:" + name, "deferred", "Missing desired formula. Review current recipe/dependency installation impact with native brew info before granting installation; existing dependencies will not be changed speculatively")
                else:
                    self.s.item("brew-install:" + name, "unchanged", "Desired formula already installed; ordinary repair does not upgrade it")
            return
        retained = [Path(i["path"]) for f in observed["formulae"].values() for i in f["installed"] if Path(i["path"]).is_dir()]
        for name, candidate in candidates.items():
            if name not in formulae:
                self.s.item("brew:" + name, "deferred", "No exact installed metadata identity")
                continue
            reason = self.brew_reason(name, formulae, set(candidates), graph)
            if reason or not self.s.apply:
                self.s.item("brew:" + name, "deferred" if reason else "proposed", reason or "Eligible native leaf update with retention and smoke checks", candidate=candidate)
                continue
            check = self.p.data["brew"]["checks"][name]
            # Use this prefix's executable for the smoke, not a competing manager's command.
            check_path = Path(prefix) / "bin" / check[0]
            if not check_path.exists():
                check_path = Path(prefix) / "opt" / formulae[name]["name"] / "bin" / check[0]
            if not check_path.exists():
                raise ValueError(f"Smoke executable for {name} is unavailable in its owning prefix")
            self.s.run([check_path] + check[1:])
            self.s.run([brew, "upgrade", "--formula", name], change=True)
            self.s.run([brew, "missing"])
            self.s.run([brew, "linkage", "--test", name])
            after = self.s.json([brew, "info", "--formula", "--json=v2", name])
            self.s.run([check_path] + check[1:])
            if not all(p.is_dir() for p in retained) or not self.consumers("after brew " + name):
                raise ValueError("Retention or consumer verification failed; remaining prefix operations stopped")
            before_versions = [i["version"] for i in formulae[name]["installed"]
            after_versions = [i["version"] for i in after["formulae"][0]["installed"]]
            self.s.item("brew:" + name, "updated" if after_versions != before_versions else "unchanged", "Native dependency checks, retained keg paths, linkage and configured consumer checks passed", before=before_versions, after=after_versions)
        for cask in meta["outdated"].get("casks", []):
            name = cask["name"]
            # A cask can run installers/uninstall scripts and destroy a required app version. Do not guess recovery.
            self.s.item("brew-cask:" + name, "deferred", "Greedy candidate recorded. Automatic cask replacement is unsupported without an application-specific recovery and lifecycle adapter; use deliberate native upgrade after reviewing that application", candidate=cask)
        for name, f in formulae.items():
            if not f.get("linked_keg"):
                self.s.item("brew-link:" + name, "unchanged", "Keg-only or intentionally unlinked state preserved; no forced link repair")

    def node(self, operation):
        cfg = self.p.data["node"]
        nvm = self.inv["nvm"]
        if not nvm["script"]:
            release = self.s.cached("nvm-release")
            if operation == "install" and self.s.apply and release and not release["stale"] and not Path(nvm["dir"]).exists():
                self.s.run([shutil.which("git") or "/usr/bin/git", "clone", "--depth", "1", "--branch", release["value"], "https://github.com/nvm-sh/nvm.git", nvm["dir"]], change=True)
                self.refresh_inventory()
                nvm = self.inv["nvm"]
            else:
                self.s.item("node", "deferred", "nvm absent or nonempty unknown installation directory; refresh nvm tags then install, or resolve ownership manually")
                return
        if cfg["update_nvm"]:
            script = Path(nvm["script"])
            if command_owner(script.resolve())["owner"] == "brew":
                self.s.item("nvm-manager", "deferred", "Homebrew owns this nvm script; manager updates belong to Homebrew")
            elif not (script.parent / ".git").exists():
                self.s.item("nvm-manager", "deferred", "Manual/script installation has no supported in-place Git update; preserve it and plan an upstream migration")
            else:
                release = self.s.cached("nvm-release")
                if not release or release["stale"]:
                    self.s.item("nvm-manager", "deferred", "Stable tag metadata requires refresh")
                elif self.s.apply:
                    git = shutil.which("git") or "/usr/bin/git"
                    _, origin = self.s.run([git, "-C", script.parent, "remote", "get-url", "origin"])
                    if origin.strip() not in {"https://github.com/nvm-sh/nvm.git", "https://github.com/nvm-sh/nvm", "git@github.com:nvm-sh/nvm.git"}:
                        raise ValueError("nvm origin is not the recognised upstream")
                    _, dirty = self.s.run([git, "-C", script.parent, "status", "--porcelain"])
                    if dirty.strip():
                        raise ValueError("nvm checkout has local changes; manager update deferred")
                    _, head = self.s.run([git, "-C", script.parent, "rev-parse", "HEAD"])
                    atomic_write(self.s.run_dir / "recovery/nvm-head.txt", head)
                    self.s.run([git, "-C", script.parent, "fetch", "origin", "tag", release["value"]], change=True)
                    self.s.run([git, "-C", script.parent, "checkout", "--detach", release["value"]], change=True)
                    self.nvm("--version")
        selectors = cfg["install"] + ([cfg["default"]] if cfg["default"] != "existing" else [])
        for selector in dict.fromkeys(selectors):
            cached = self.s.cached("node:" + selector)
            if re.fullmatch(r"v?\d+\.\d+\.\d+", selector):
                version = "v" + selector.lstrip("v")
            elif cached and not cached["stale"]:
                version = cached["value"]
            else:
                self.s.item("node:" + selector, "deferred", "Floating selector needs explicit fresh native resolution; run refresh --only node --apply")
                continue
            root = Path(nvm["dir"]) / "versions/node" / version
            if not (root / "bin/node").is_file():
                if not self.s.apply:
                    self.s.item("node:" + selector, "proposed", "Install alongside existing versions; preserve aliases and old global environments", version=version)
                    continue
                aliases = Path(nvm["dir"]) / "alias"
                before = {str(p.relative_to(aliases)): p.read_bytes() for p in aliases.rglob("*") if p.is_file() and not p.is_symlink()}
                write_json(self.s.run_dir / "recovery/nvm-aliases.json", {k: v.decode(errors="replace") for k, v in before.items()})
                try:
                    self.nvm("install", version, "--skip-default-packages", change=True)
                finally:
                    for p in aliases.rglob("*"):
                        if p.is_file() and not p.is_symlink() and str(p.relative_to(aliases)) not in before:
                            p.unlink()
                    for name, content in before.items():
                        path = aliases / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content)
                _, actual = self.s.run([root / "bin/node", "--version"])
                if actual.strip() != version:
                    raise ValueError("Installed Node did not report selected version; default unchanged")
                self.s.item("node:" + version, "updated", "New runtime installed alongside retained versions; aliases restored")
                self.refresh_inventory()
            else:
                self.s.item("node:" + version, "unchanged", "Selected runtime is installed; this is not a claim of upstream currency")
            if self.s.apply:
                path_env = {"PATH": str(root / "bin") + os.pathsep + self.s.env.get("PATH", ""), "NPM_CONFIG_PREFIX": "", "npm_config_prefix": ""}
                _, prefix = self.s.run([root / "bin/npm", "prefix", "-g"], env=path_env)
                if Path(prefix.strip()).resolve() != root.resolve():
                    raise ValueError(f"npm prefix conflicts with {version}; no tools/default changes")
                for executable in ("npm", "npx"):
                    self.s.run([root / "bin" / executable, "--version"], env=path_env)
                if cfg["globals"] or cfg["latest_compatible_npm"]:
                    # Same-path recovery copy, never used as a relocated environment.
                    backup = self.s.run_dir / "recovery" / ("node-" + version)
                    shutil.copytree(root, backup, symlinks=True)
                    write_json(backup / "restore-to-original-path.json", {"original": str(root), "note": "Recovery only at the original absolute path; native modules and entry points are not portable"})
                    if cfg["latest_compatible_npm"]:
                        shell = 'export NVM_DIR="$1"; . "$2" --no-use || exit $?; nvm use "$3" >/dev/null || exit $?; nvm install-latest-npm'
                        self.s.run(["/bin/bash", "--noprofile", "--norc", "-c", shell, "devsys-npm", nvm["dir"], nvm["script"], version], change=True, env=path_env)
                    for package, rule in cfg["globals"].items():
                        if package in {"npm", "corepack"} or not isinstance(rule, dict) or not rule.get("smoke"):
                            raise ValueError("Global tool needs a registry spec and smoke check; npm/Corepack use separate supported policies")
                        spec = rule.get("spec")
                        if not isinstance(spec, str) or not re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+@[A-Za-z0-9.*^~<>=|+ -]+", package + "@" + spec):
                            raise ValueError("Unsupported global registry package/spec")
                        self.s.run([root / "bin/npm", "install", "--global", "--engine-strict", package + "@" + spec], change=True, env=path_env)
                        self.s.run(rule["smoke"], env=path_env)
                        metadata = read_json(root / "lib/node_modules" / package / "package.json", {})
                        bins = metadata.get("bin", {})
                        bins = {package.split("/")[-1]: bins} if isinstance(bins, str) else bins
                        if not bins or not all((root / "bin" / b).exists() and (root / "bin" / b).resolve().is_relative_to(root) for b in bins):
                            raise ValueError("Global tool entry points resolve outside the target nvm environment")
                        self.s.item("npm:" + package, "unverified", "Selected tool installed and smoke/entrypoint checked; native version evidence is in command logs; legacy environments retained")
                if cfg["default"] == selector and cfg["switch_default"]:
                    self.pending_node = root

    def select_node(self):
        if not self.pending_node:
            return
        for name, rule in self.p.data["tools"].items():
            if rule["enabled"]:
                command, _, env = self.env.resolve(name, cwd=self.s.run_dir)
                self.s.run(command + rule["smoke"][1:], env=env)
        if not self.consumers("before Node default migration"):
            raise ValueError("Consumer checks failed; Node default unchanged")
        self.env.select("node", self.pending_node)
        self.s.item("node-default", "updated", "Explicit user default pointer changed after essential CLI checks; exact nvm aliases/project requirements remain intact")

    def tools(self, operation):
        for name, rule in self.p.data["tools"].items():
            if rule["enabled"]:
                self.isolated("tool:" + name, lambda name=name, rule=rule: self.tool(name, rule, operation))

    def tool(self, name, rule, operation):
        current = self.env.selections().get("tool:" + name, {})
        rule_hash = fingerprint(rule)
        if current.get("current") and Path(current["current"]).is_file() and current.get("metadata", {}).get("rule_sha256") == rule_hash and operation in {"install", "repair"}:
            command, _, env = self.env.resolve(name, cwd=self.s.run_dir)
            self.s.run(command + rule["smoke"][1:], env=env)
            self.s.item("tool:" + name, "unchanged", "Configured tool is healthy; setup/repair does not reinstall it")
            return
        if not self.s.apply:
            self.s.item("tool:" + name, "proposed", f"Establish/maintain {rule['owner']} ownership; preserve authentication/configuration and existing installations")
            return
        if rule["version"] == "existing":
            candidates = self.inv["commands"].get(name, {}).get("candidates", [])
            expected = rule["owner"]
            found = next((c for c in candidates if c["owner"] == expected), None)
            if not found:
                self.s.item("tool:" + name, "deferred", "No existing installation with the selected verified owner; choose an explicit supported version/channel")
                return
            self.s.run([found["path"]] + rule["smoke"][1:])
            self.env.select("tool:" + name, found["path"], {"rule_sha256": rule_hash, "owner": expected})
            self.s.item("tool:" + name, "updated", "Verified existing owner selected; duplicates and user data retained. External native updater retention remains outside devsys control")
            return
        generation = self.p.root / "tools" / name / self.s.run_dir.name
        generation.mkdir(parents=True, exist_ok=False)
        runtime = None
        if rule["owner"] == "native":
            contract = read_json(REPO / "config/upstream-installers.json").get(name)
            if not contract:
                self.s.item("tool:" + name, "deferred", "No reviewed upstream native installation contract for this tool")
                return
            script = self.s.run_dir / (name + "-installer.sh")
            self.s.run(["/usr/bin/curl", "--fail", "--location", "--silent", "--show-error", "--max-time", "60", contract["url"], "--output", script], change=True)
            actual = hashlib.sha256(script.read_bytes()).hexdigest()
            if actual != contract["sha256"]:
                raise ValueError("Upstream installer changed since review; retain downloaded source and review contract before execution")
            # Installer has its own private HOME at its final path. It cannot rewrite real startup/auth files.
            installer_home = generation / "installer-home"
            installer_home.mkdir(mode=0o700)
            env = {"HOME": str(installer_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + str(generation / "bin")}
            if name == "codex":
                env.update({"CODEX_HOME": str(installer_home / ".codex"), "CODEX_INSTALL_DIR": str(generation / "bin"), "CODEX_NON_INTERACTIVE": "true"})
                args = ["/bin/sh", script, "--release", rule["version"]]
                entry = generation / "bin/codex"
            else:
                if not re.fullmatch(r"stable|latest|\d+\.\d+\.\d+", rule["version"]):
                    raise ValueError("Explicit stable/latest or exact native Claude version required")
                args, entry = ["/bin/bash", script, rule["version"]], installer_home / ".local/bin/claude"
            self.s.run(args, change=True, env=env)
            write_json(generation / "installer-provenance.json", {"url": contract["url"], "sha256": actual, "home_isolated": True})
        elif rule["owner"] == "npm":
            runtime, _ = self.env.node_root(self.s.run_dir)
            npm = runtime / "bin/npm"
            env = {"PATH": str(runtime / "bin") + os.pathsep + self.s.env.get("PATH", "")}
            package = rule.get("package")
            if not package or not re.fullmatch(r"(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+", package):
                raise ValueError("npm tool needs a supported registry package identity")
            self.s.run([npm, "install", "--prefix", generation, "--engine-strict", package + "@" + rule["version"]], change=True, env=env)
            entry = generation / "node_modules/.bin" / name
        elif rule["owner"] in {"uv", "pipx"}:
            owner = rule["owner"]
            manager = self.first(owner)
            if not manager:
                raise ValueError(f"Selected {owner} manager is missing")
            selected = self.env.selections().get("python", {}).get("current")
            if not selected:
                raise ValueError("Select the maintained Python interpreter before installing isolated Python tools")
            base = read_json(Path(selected) / "devsys-generation.json")["base_interpreter"]
            spec = rule.get("package", name) + rule["version"]
            if owner == "uv":
                self.s.run([manager, "tool", "install", spec, "--python", base, "--no-config", "--no-python-downloads"], change=True,
                           env={"UV_TOOL_DIR": str(generation / "tools"), "UV_TOOL_BIN_DIR": str(generation / "bin")})
            else:
                self.s.run([manager, "install", spec, "--python", base, "--skip-maintenance"], change=True,
                           env={"PIPX_HOME": str(generation / "pipx"), "PIPX_BIN_DIR": str(generation / "bin"), "PIPX_SHARED_LIBS": str(generation / "pipx/shared")})
            entry = generation / "bin" / name
        elif rule["owner"] == "go":
            go = self.first("go")
            if not go or not rule.get("package"):
                raise ValueError("Go tool needs an installed compiler and explicit module package")
            self.s.run([go, "install", rule["package"] + "@" + rule["version"]], change=True,
                       env={"GOBIN": str(generation / "bin"), "GOCACHE": str(self.s.run_dir / "cache/go"), "GOTOOLCHAIN": "local"})
            entry = generation / "bin" / name
        else:
            self.s.item("tool:" + name, "deferred", f"{rule['owner']} tool migration needs preserved feature/build/gemset constraints; automatic isolated installation adapter not implemented")
            return
        if not entry.is_file():
            raise ValueError("Replacement tool entry point missing; prior selection unchanged")
        check_env = {"PATH": str(runtime / "bin") + os.pathsep + self.s.env.get("PATH", "")} if runtime else {}
        _, version = self.s.run([entry] + rule["smoke"][1:], env=check_env)
        if not self.consumers("after candidate " + name):
            raise ValueError("Consumer verification failed; prior tool selection unchanged")
        metadata = {"rule_sha256": rule_hash, "owner": rule["owner"], "runtime": str(runtime) if runtime else None, "smoke_output": version.strip()}
        self.env.select("tool:" + name, entry, metadata)
        self.s.item("tool:" + name, "updated", "Replacement command verified before selection; prior installations, generations and original user configuration retained", target=str(entry))

    def rust(self, operation):
        rustup = self.first("rustup")
        if not rustup:
            self.s.item("rust", "deferred", "rustup absent; upstream bootstrap needs an explicit reviewed installer. Any Homebrew Rust remains owned by Homebrew")
            return
        _, listed = self.s.run([rustup, "toolchain", "list"])
        installed = [line.split()[0] for line in listed.splitlines() if line.strip()]
        for channel in installed:
            floating = bool(re.fullmatch(r"(?:stable|beta|nightly)(?:-[A-Za-z0-9_]+-[A-Za-z0-9_]+-[A-Za-z0-9_]+)?", channel))
            eligible = channel in self.p.data["rust"]["channels"] and floating
            _, components = self.s.run([rustup, "component", "list", "--installed", "--toolchain", channel])
            _, targets = self.s.run([rustup, "target", "list", "--installed", "--toolchain", channel])
            self.s.observed.setdefault("rust_components", {})[channel] = {"components": components.splitlines(), "targets": targets.splitlines()}
            if not eligible or operation != "update":
                self.s.item("rust:" + channel, "unchanged" if not floating else "deferred", "Fixed/custom toolchain retained" if not floating else "Existing floating channel requires explicit rust.channels policy; no new prerelease channel introduced")
                continue
            if not self.s.apply:
                self.s.item("rust:" + channel, "proposed", "Update existing channel after preserving complete toolchain, components, targets and overrides")
                continue
            root = Path(os.environ.get("RUSTUP_HOME", str(self.p.home / ".rustup"))) / "toolchains" / channel
            if root.is_symlink():
                raise ValueError("Custom/symlink toolchain cannot be replaced")
            backup = self.s.run_dir / "recovery" / ("rust-" + channel)
            shutil.copytree(root, backup, symlinks=True)
            _, before = self.s.run([rustup, "run", channel, "rustc", "--version"])
            self.s.run([rustup, "update", channel, "--no-self-update"], change=True)
            _, after = self.s.run([rustup, "run", channel, "rustc", "--version"])
            _, after_components = self.s.run([rustup, "component", "list", "--installed", "--toolchain", channel])
            _, after_targets = self.s.run([rustup, "target", "list", "--installed", "--toolchain", channel])
            if not set(components.splitlines()) <= set(after_components.splitlines()) or not set(targets.splitlines()) <= set(after_targets.splitlines()):
                raise ValueError("Rust component/target retention failed; restore complete toolchain at original path after review")
            if not self.consumers("after Rust " + channel):
                raise ValueError("Rust consumer check failed; full prior toolchain remains in recovery")
            write_json(backup / "devsys-restore.json", {"original": str(root), "before": before, "after": after, "components": components, "targets": targets})
            self.s.item("rust:" + channel, "updated" if before != after else "unchanged", "Native update and component/target retention checked; overrides/default selections not changed", recovery=str(backup))
        if self.p.data["rust"]["update_manager"]:
            if command_owner(rustup)["owner"] == "brew":
                self.s.item("rustup", "deferred", "Package-owned manager must update through Homebrew")
            elif self.s.apply and operation == "update":
                _, text = self.s.run([rustup, "self", "--help"])
                if not re.search(r"\bupdate\b", text):
                    raise ValueError("Installed rustup has no supported self update command")
                shutil.copy2(Path(rustup).resolve(), self.s.run_dir / "recovery/rustup")
                _, before = self.s.run([rustup, "--version"])
                self.s.run([rustup, "self", "update"], change=True)
                _, after = self.s.run([rustup, "--version"])
                self.s.item("rustup", "updated" if before != after else "unchanged", "Owning manager self-update verified; original executable retained")

    def coverage(self):
        reasons = {
            "uv": "Managed Python generations and configured isolated tools supported; duplicate Homebrew uv retained. Standalone uv self-update is reported for deliberate native execution because replacing the manager used by an active operation needs separate binary recovery",
            "pipx": "Configured registry tools can be built in separate final-path environments. Existing injected/editable/pinned environments remain untouched until their full constraints are configured",
            "pyenv": "Installed interpreters/project pins discovered; explicit existing selections supported without global shims. New source-built interpreters need build dependency/ABI policy",
            "mise": "Installed runtimes discovered; backend upgrades can rewrite selections/remove versions. Per-backend retention/migration adapter not implemented",
            "asdf": "Installed runtimes discovered; plugin-specific build/retention operations not implemented",
            "proto": "Known installation/command discovery supported; runtime/tool migration not implemented. No competing Node owner initialized",
            "fnm": "Node ownership conflict reported; migration into nvm requires explicit consumer selection",
            "volta": "Node/tool ownership conflict reported; project pin migration into nvm requires explicit consumer selection",
            "bun": "Homebrew-owned runtime handled by brew policy. Native/global-tool changes require saved dependency/lockfile/install provenance; no incompatible self-update",
            "deno": "Homebrew-owned runtime handled by brew policy. Native/global-tool changes require URL/permission/source policy; no incompatible self-update",
            "go": "Configured Go-installed tools supported in separate binary generations with local compiler policy. Unconfigured binaries/build flags require explicit module provenance",
            "ruby": "Runtime and duplicate commands discovered; Homebrew runtime uses brew policy. Application/Bundler gem migration is outside scope",
            "gem": "Global gem changes lack per-Ruby/gemset/native-extension recovery policy; no gem update-all or cleanup",
            "rbenv": "Installed Rubies and project pins discovered; new native builds/default migration need explicit dependency policy",
            "java": "Known JDK locations discovered; Homebrew packages use brew policy. No global JAVA_HOME or system symlink changes",
            "jenv": "Command discovery supported; JDK installation belongs to its actual owner; default migration not implemented",
            "mas": "Availability refresh supported. Automatic app replacement lacks per-application recovery/authentication/lifecycle adapters",
            "cargo": "Installed crate identities recorded; automatic cargo-update would not preserve feature/target/source constraints. Explicit tool-generation adapter not implemented",
        }
        for name, reason in reasons.items():
            present = bool(self.inv["commands"].get(name, {}).get("candidates"))
            self.s.item("coverage:" + name, "deferred" if present else "unverified", reason if present else "Manager/executable not detected in bounded locations; no assumption of active managed installations")
        for name in ("rvm", "sdkman"):
            present = bool(self.inv["runtimes"].get(name))
            self.s.item("coverage:" + name, "deferred" if present else "unverified", "Known runtime directories inventoried without sourcing initialization scripts. Native candidate migrations not implemented" if present else "No runtimes found in known directories")
        self.s.item("coverage:excluded", "unverified", "Project dependencies/lockfiles, application plugins/bundled runtimes, containers, databases, persistent service data and macOS installs require separate scope. Unknown application consumers untested")
