"""Small policy, process, logging, and recovery primitives. No third-party imports."""
from __future__ import annotations

import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import tempfile

REPO = Path(__file__).resolve().parent.parent
STATUSES = {"updated", "unchanged", "deferred", "failed", "unverified", "proposed"}


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return copy.deepcopy(default)


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Refusing to overwrite symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_file() and path.read_bytes() == content.encode() and stat.S_IMODE(path.stat().st_mode) == mode:
        return False
    fd, temp = tempfile.mkstemp(prefix=".devsys-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return True


def write_json(path, data):
    return atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def redact(value):
    text = str(value)
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)([?&](?:token|key|signature|password)=)[^&\s]+", r"\1[REDACTED]", text)
    return re.sub(r"(?i)((?:token|password|_authToken|authorization|secret)[\s=:]+)\S+", r"\1[REDACTED]", text)


def expanded(value, home=None):
    home = Path(home or Path.home())
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"Expected absolute or ~/ path: {value}")
    return path


def display_path(path, home=None):
    home = Path(home or Path.home())
    path = Path(path)
    return "~/" + str(path.relative_to(home)) if path.is_relative_to(home) else str(path)


def argv_valid(argv):
    return isinstance(argv, list) and bool(argv) and all(isinstance(a, str) and a and "\0" not in a for a in argv)


class Policy:
    def __init__(self, path=None, home=None):
        self.path = Path(path or REPO / "config/environment.json").absolute()
        self.home = Path(home or Path.home())
        self.data = read_json(self.path)
        if not isinstance(self.data, dict):
            raise ValueError("Configuration must be a JSON object")
        self.validate()
        self.root = expanded(self.data["root"], self.home)
        self.state = expanded(self.data["state_dir"], self.home)
        self.digest = fingerprint(self.data)

    def validate(self):
        allowed = {"schema", "root", "state_dir", "discovery", "timeouts", "unattended_apply", "owners",
                   "python", "node", "brew", "rust", "tools", "consumers", "integration", "metadata_max_age_hours"}
        if set(self.data) - allowed or self.data.get("schema") != 1:
            raise ValueError("Unknown configuration keys or schema; see config/environment.json")
        for name in allowed - {"schema"}:
            if name not in self.data:
                raise ValueError(f"Missing configuration section: {name}")
        for key in ("root", "state_dir"):
            expanded(self.data[key], self.home)
        for key, fields in {
            "discovery": {"project_roots", "depth", "limit", "manager_paths"},
            "timeouts": {"query", "update"}, "python": {"selector", "profiles", "additions", "imports", "allow_untracked"},
            "node": {"install", "default", "switch_default", "globals", "latest_compatible_npm", "update_nvm"},
            "brew": {"formulae", "casks", "protected", "updates", "allow_unknown_consumers", "checks", "cask_recovery_ack"},
            "rust": {"channels", "update_manager"}, "integration": {"shell", "schedule", "legacy_jobs"},
        }.items():
            if not isinstance(self.data[key], dict) or set(self.data[key]) != fields:
                raise ValueError(f"Invalid {key} keys; expected {sorted(fields)}")
        for key in ("unattended_apply",):
            if type(self.data[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
        for group, keys in {"python": ["allow_untracked"], "node": ["switch_default", "latest_compatible_npm", "update_nvm"],
                            "brew": ["allow_unknown_consumers", "cask_recovery_ack"], "rust": ["update_manager"],
                            "integration": ["shell", "schedule"]}.items():
            for key in keys:
                if type(self.data[group][key]) is not bool:
                    raise ValueError(f"{group}.{key} must be boolean")
        for key in ("query", "update"):
            if type(self.data["timeouts"][key]) is not int or not 1 <= self.data["timeouts"][key] <= 7200:
                raise ValueError("Timeouts must be integer seconds between 1 and 7200")
        d = self.data["discovery"]
        if type(d["depth"]) is not int or not 0 <= d["depth"] <= 10 or type(d["limit"]) is not int or not 1 <= d["limit"] <= 20000:
            raise ValueError("Discovery bound must be depth <=10 and <=20000 directories")
        for group, keys in {"discovery": ["project_roots"], "python": ["profiles", "additions", "imports"],
                            "node": ["install"], "brew": ["formulae", "casks", "protected"],
                            "rust": ["channels"], "integration": ["legacy_jobs"]}.items():
            for key in keys:
                if not isinstance(self.data[group][key], list) or not all(isinstance(v, str) for v in self.data[group][key]):
                    raise ValueError(f"{group}.{key} must be a string array")
        if self.data["brew"]["updates"] not in {"report", "eligible-leaves", "configured"}:
            raise ValueError("Unknown Homebrew update policy")
        if not isinstance(self.data["metadata_max_age_hours"], (float, int)) or self.data["metadata_max_age_hours"] <= 0:
            raise ValueError("metadata_max_age_hours must be positive")
        if not isinstance(d["manager_paths"], dict):
            raise ValueError("manager_paths must be an object")
        for name, paths in d["manager_paths"].items():
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise ValueError(f"manager_paths.{name} must be a path array")
            for p in paths:
                expanded(p, self.home)
        for spec in self.data["node"]["install"] + [self.data["node"]["default"]]:
            if spec != "existing" and not re.fullmatch(r"v?\d+(?:\.\d+){0,2}|node|stable|lts/(?:\*|[a-z]+)", spec):
                raise ValueError(f"Unsupported Node selector {spec}; no implicit prerelease channels")
        if not isinstance(self.data["node"]["globals"], dict) or not isinstance(self.data["brew"]["checks"], dict):
            raise ValueError("node.globals and brew.checks must be objects")
        for check in self.data["brew"]["checks"].values():
            if not argv_valid(check):
                raise ValueError("Homebrew checks must be argv arrays")
        if not isinstance(self.data["owners"], dict) or not all(isinstance(v, str) for v in self.data["owners"].values()):
            raise ValueError("owners must map command names to owner names")
        if not isinstance(self.data["tools"], dict):
            raise ValueError("tools must be an object")
        for name, rule in self.data["tools"].items():
            if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) or not isinstance(rule, dict):
                raise ValueError("Invalid tool identity")
            if set(rule) - {"owner", "package", "version", "smoke", "enabled", "features", "runtime"}:
                raise ValueError(f"Unknown tool rule for {name}")
            if rule.get("owner") not in {"native", "npm", "uv", "pipx", "go", "cargo", "gem", "brew"}:
                raise ValueError(f"Unsupported owner for {name}")
            if not argv_valid(rule.get("smoke")) or type(rule.get("enabled")) is not bool:
                raise ValueError(f"{name} needs a smoke argv array and boolean enabled")
        if not isinstance(self.data["consumers"], list):
            raise ValueError("consumers must be an array")
        for consumer in self.data["consumers"]:
            if not isinstance(consumer, dict) or set(consumer) - {"name", "paths", "brew", "smoke", "cwd"}:
                raise ValueError("Invalid consumer fields")
            if not isinstance(consumer.get("name"), str) or not isinstance(consumer.get("paths", {}), dict) or not isinstance(consumer.get("brew", {}), dict):
                raise ValueError("Consumer needs name, paths object and brew version requirements object")
            if "smoke" in consumer and not argv_valid(consumer["smoke"]):
                raise ValueError("Consumer smoke must be an argv array")


class CommandFailure(Exception):
    def __init__(self, argv, rc, output):
        super().__init__(f"exit {rc}: {shlex.join(map(str, argv[:3]))}")
        self.argv, self.rc, self.output = argv, rc, output


class Session:
    def __init__(self, policy, apply=False, operation="plan", state=None):
        self.policy, self.apply, self.operation = policy, apply, operation
        self.state = Path(state or policy.state)
        self.items, self.commands, self.observed = [], [], {}
        self.run_dir = None
        self.lock = None
        self.active = None
        self.env = dict(os.environ)
        self.env.update({"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1",
                         "HOMEBREW_NO_AUTOREMOVE": "1", "HOMEBREW_NO_ANALYTICS": "1",
                         "HOMEBREW_NO_UPGRADE_QUIT_CASKS": "1", "HOMEBREW_NO_ENV_HINTS": "1",
                         "CI": "1", "GIT_TERMINAL_PROMPT": "0", "PIP_NO_INPUT": "1",
                         "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1",
                         "UV_PYTHON_DOWNLOADS": "never", "UV_NO_CONFIG": "1",
                         "npm_config_update_notifier": "false", "npm_config_audit": "false", "npm_config_fund": "false",
                         "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})

    def __enter__(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.state.is_symlink() or self.state.stat().st_uid != os.getuid() or self.state.stat().st_mode & 0o077:
            raise ValueError("State directory must be a real private directory owned by this user (chmod 700)")
        fd = os.open(self.state / "maintenance.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise BlockingIOError("Another maintenance operation holds the shared lock") from None
        self.lock.write(str(os.getpid()))
        self.lock.flush()
        runs = self.state / "runs"
        runs.mkdir(exist_ok=True, mode=0o700)
        self.run_dir = Path(tempfile.mkdtemp(prefix=dt.datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=runs))
        for name in ("tmp", "cache", "recovery"):
            (self.run_dir / name).mkdir(mode=0o700)
        self.env.update({"HOMEBREW_CACHE": str(self.run_dir / "cache/brew"), "HOMEBREW_LOGS": str(self.run_dir / "brew-logs"),
                         "HOMEBREW_TEMP": str(self.run_dir / "tmp"), "UV_CACHE_DIR": str(self.run_dir / "cache/uv"),
                         "PIP_CACHE_DIR": str(self.run_dir / "cache/pip"), "PIPX_LOG_DIR": str(self.run_dir / "pipx-logs"),
                         "npm_config_cache": str(self.run_dir / "cache/npm"), "MISE_CACHE_DIR": str(self.run_dir / "cache/mise")})
        return self

    def __exit__(self, kind, exc, trace):
        self.stop()
        if exc:
            self.item("run", "failed", str(exc))
        self.save()
        if self.lock:
            self.lock.close()

    def item(self, component, status, reason, **details):
        if status not in STATUSES:
            raise ValueError(f"Unknown result status {status}")
        self.items.append(dict(component=component, status=status, reason=redact(reason), **details))
        self.save()

    def save(self):
        if not self.run_dir:
            return
        report = {"operation": self.operation, "apply": self.apply, "policy_sha256": self.policy.digest,
                  "time": dt.datetime.now(dt.timezone.utc).isoformat(), "items": self.items,
                  "commands": self.commands, "observed": self.observed,
                  "limitations": ["Configured roots and consumers only; unknown applications remain untested.",
                                   "Package metadata does not establish universal compatibility."]}
        write_json(self.run_dir / "report.json", json.loads(redact(json.dumps(report, default=str))))

    def stop(self):
        if self.active and self.active.poll() is None:
            os.killpg(self.active.pid, signal.SIGTERM)
            try:
                self.active.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.active.pid, signal.SIGKILL)
                self.active.wait()

    def run(self, argv, *, change=False, env=None, cwd=None, accepted=(0,), check=True, expected_failure=False):
        if change and not self.apply:
            raise ValueError("A mutating command requires --apply")
        command = [str(a) for a in argv]
        effective = dict(self.env)
        effective.update(env or {})
        record = {"argv": command, "changes_state": change}
        self.commands.append(record)
        logfile = self.run_dir / f"command-{len(self.commands):04d}.log"
        timeout = self.policy.data["timeouts"]["update" if change else "query"]
        try:
            with tempfile.TemporaryFile(dir=self.run_dir / "tmp") as raw:
                self.active = subprocess.Popen(command, cwd=cwd or self.run_dir, env=effective, stdin=subprocess.DEVNULL,
                                               stdout=raw, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    rc = self.active.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.stop()
                    rc = 124
                raw.seek(0)
                output = raw.read(16 * 1024 * 1024 + 1)
                if len(output) > 16 * 1024 * 1024:
                    rc = 65
                    output = output[:16 * 1024 * 1024] + b"\n[TRUNCATED; result unusable]\n"
                output = redact(output.decode(errors="replace"))
        except OSError as exc:
            rc, output = 127, redact(exc)
        finally:
            self.active = None
        atomic_write(logfile, output)
        record.update(returncode=rc, log=logfile.name)
        self.save()
        if rc not in accepted:
            if not expected_failure:
                self.item("command", "failed", f"exit {rc}: {shlex.join(command[:3])}; see {logfile.name}")
            if check:
                raise CommandFailure(command, rc, output)
        return rc, output

    def json(self, argv, **kwargs):
        _, output = self.run(argv, **kwargs)
        try:
            return json.loads(output)
        except ValueError as exc:
            raise ValueError(f"Invalid structured output from {argv[0]}; no dependent changes permitted") from exc

    def metadata_path(self, key):
        return self.state / "metadata" / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")

    def cache(self, key, value):
        if not self.apply:
            raise ValueError("Metadata refresh requires --apply")
        write_json(self.metadata_path(key), {"key": key, "time": dt.datetime.now(dt.timezone.utc).isoformat(), "value": value})

    def cached(self, key):
        data = read_json(self.metadata_path(key))
        if data:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(data["time"])).total_seconds() / 3600
            data["age_hours"] = round(age, 2)
            data["stale"] = age > self.policy.data["metadata_max_age_hours"]
        return data


def dependency_closure(roots, graph):
    visited, todo = set(), list(roots)
    while todo:
        name = todo.pop()
        if name not in visited:
            visited.add(name)
            todo.extend(graph.get(name, []))
    return visited
