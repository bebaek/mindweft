"""Per-user local instance discovery and lifetime locks (macOS/Linux).

Registry entries are routing metadata, not credentials. Identity checks protect against
stale ports, not hostile same-user processes. Lock descriptors can be inherited by services
so killing a launcher cannot allow a second writer while its children are still alive.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Iterator, Mapping
from uuid import uuid4

import httpx

from mindweft_client.state import state_dir_path


def validate_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError(
            "Instance name must be 1-64 letters/digits, underscores, or hyphens, starting with a letter/digit."
        )
    return name


def package_version() -> str:
    try:
        return version("mindweft")
    except PackageNotFoundError:
        return "unknown"


def registry_dir(env: Mapping[str, str] | None = None) -> Path:
    return state_dir_path(os.environ if env is None else env) / "instances"


@dataclass(frozen=True)
class InstanceRecord:
    name: str
    launch_id: str
    api_url: str
    gateway_url: str
    pid: int
    version: str

    @classmethod
    def read(cls, path: Path) -> InstanceRecord:
        with path.open(encoding="utf-8") as stream:
            data = json.loads(stream.read(16384))
        record = cls(**data)
        validate_name(record.name)
        if (
            type(record.pid) is not int
            or record.pid <= 0
            or not isinstance(record.version, str)
            or len(record.version) > 100
        ):
            raise ValueError("Invalid instance metadata")
        if not re.fullmatch(r"[0-9a-f]{32}", record.launch_id):
            raise ValueError("Invalid instance launch ID")
        for url in (record.api_url, record.gateway_url):
            match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", url)
            if match is None or not 1 <= int(match[1]) <= 65535:
                raise ValueError("Invalid local instance URL")
        if path.stem != record.name:
            raise ValueError("Instance name does not match registry entry")
        return record


def verify_record(record: InstanceRecord) -> None:
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get(record.api_url + "/local-instance")
            response.raise_for_status()
            identity = response.json()
        if identity.get("name") == record.name and identity.get("launch_id") == record.launch_id:
            return
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    raise RuntimeError(
        f"Instance '{record.name}' is unavailable or its registry entry is stale; refusing to connect."
    )


def resolve_instance(name: str, env: Mapping[str, str] | None = None) -> InstanceRecord:
    validate_name(name)
    try:
        record = InstanceRecord.read(registry_dir(env) / f"{name}.json")
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"No valid registry entry for instance '{name}'. Start it with mindweft code --instance {name}."
        ) from exc
    verify_record(record)
    return record


def list_instances(env: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    rows = []
    for path in sorted(registry_dir(env).glob("*.json")):
        try:
            record = InstanceRecord.read(path)
        except (OSError, ValueError, TypeError):
            continue
        try:
            verify_record(record)
            status = "running"
        except RuntimeError:
            status = "stale"
        rows.append(
            {
                "name": record.name,
                "url": record.api_url,
                "version": record.version,
                "status": status,
            }
        )
    return rows


class InstanceLease:
    def __init__(self, name: str, env: Mapping[str, str]) -> None:
        self.name = validate_name(name)
        self.launch_id = uuid4().hex
        self.root = registry_dir(env)
        self.state_dir = self.root / name if name != "default" else self.root.parent
        self.fd = -1

    def __enter__(self) -> InstanceLease:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(
            self.root / f"{self.name}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = -1
            raise RuntimeError(
                f"Instance '{self.name}' is already running. Choose a different --instance name."
            ) from exc
        try:
            if self.name != "default" and self.state_dir.is_symlink():
                raise OSError("Named instance state directory must not be a symlink")
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            os.close(self.fd)
            raise
        return self

    def publish(self, api_port: int, gateway_port: int) -> InstanceRecord:
        record = InstanceRecord(
            self.name,
            self.launch_id,
            f"http://127.0.0.1:{api_port}",
            f"http://127.0.0.1:{gateway_port}",
            os.getpid(),
            package_version(),
        )
        fd, temporary = tempfile.mkstemp(prefix=f".{self.name}-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(record), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / f"{self.name}.json")
        finally:
            Path(temporary).unlink(missing_ok=True)
        return record

    def __exit__(self, *_args: object) -> None:
        path = self.root / f"{self.name}.json"
        try:
            if InstanceRecord.read(path).launch_id == self.launch_id:
                path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            # Do not LOCK_UN: inherited descriptors must keep orphaned children locked.
            os.close(self.fd)


@contextmanager
def reserve_port(requested: int | None, preferred: int, flag: str) -> Iterator[socket.socket]:
    if requested is not None and not 1 <= requested <= 65535:
        raise ValueError(f"{flag} must be between 1 and 65535.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", preferred if requested is None else requested))
        except OSError as exc:
            if requested is not None:
                raise RuntimeError(
                    f"Local port {requested} is unavailable; choose another with {flag}."
                ) from exc
            listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        yield listener


@contextmanager
def reserve_ports(
    api_port: int | None, gateway_port: int | None
) -> Iterator[tuple[socket.socket, socket.socket]]:
    if api_port is not None and api_port == gateway_port:
        raise ValueError("--port and --gateway-port must be different.")
    with ExitStack() as stack:
        # Honor exact requests before allowing automatic allocations to consume ports.
        if gateway_port is not None:
            gateway = stack.enter_context(reserve_port(gateway_port, 8765, "--gateway-port"))
            api = stack.enter_context(reserve_port(api_port, 8000, "--port"))
        else:
            api = stack.enter_context(reserve_port(api_port, 8000, "--port"))
            gateway = stack.enter_context(reserve_port(gateway_port, 8765, "--gateway-port"))
        yield api, gateway


@contextmanager
def lock_storage(env: Mapping[str, str]) -> Iterator[tuple[int, ...]]:
    """Prevent new launchers from concurrently opening explicitly shared store paths."""
    descriptors: list[int] = []
    paths: set[Path] = set()
    try:
        for suffix in ("THREAD_DB_PATH", "ATTACHMENT_DB_PATH", "OAUTH_STORE_PATH"):
            value = env.get(f"MINDWEFT_{suffix}", env.get(f"MINIGENT_{suffix}"))
            if not value:
                continue
            path = Path(value).expanduser().resolve()
            if path in paths:
                continue
            paths.add(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(
                str(path) + ".mindweft-lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            descriptors.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    f"Storage for {suffix} is in use by another coding instance. Use a different --instance name."
                ) from exc
        yield tuple(descriptors)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
