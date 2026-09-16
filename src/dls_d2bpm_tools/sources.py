"""Where firmware releases come from: the CI build area, or GitLab.

Both sources present the same small interface (`FirmwareSource`) and hand back
an `Artifact` holding a local path, so everything downstream is identical
whether the file came off a filesystem mount or over HTTP.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit

from .firmware import SCRIPT_DEVICE, TARGET, Artifact, Device, LogFn

__all__ = [
    "DEFAULT_CACHE_DIR",
    "FIRMWARE_BASE",
    "GITLAB_PROJECT",
    "GITLAB_REPOSITORY",
    "GITLAB_URL",
    "MAX_RELEASE_PAGES",
    "RELEASES_PER_PAGE",
    "GitLabSource",
    "LocalSource",
    "SourceError",
]

#: Root of the firmware release tree the CI writes to.
FIRMWARE_BASE = Path("/dls_sw/work/ci-builds/d2afe-firmware")

#: The GitLab instance and project publishing firmware releases.
GITLAB_URL = "https://gitlab.diamond.ac.uk"
GITLAB_PROJECT = "diagnostics/d2afe-firmware"
GITLAB_REPOSITORY = f"{GITLAB_URL}/{GITLAB_PROJECT}"

#: Downloaded artifacts are kept here so a re-flash costs nothing.
DEFAULT_CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "dls-d2bpm-tools"
    / "firmware"
)

#: Release assets point at the GitLab *web* URL for a job artifact, which
#: redirects to a login page. The API serves the same file directly.
_JOB_ARTIFACT_RE = re.compile(r"/-/jobs/(?P<job>\d+)/artifacts/file/(?P<path>.+)$")

_TIMEOUT = 15.0

#: The releases endpoint is paginated; 100 is the API's maximum page size.
RELEASES_PER_PAGE = 100

#: A sanity bound, so a misbehaving endpoint cannot loop forever.
MAX_RELEASE_PAGES = 20


class SourceError(RuntimeError):
    """A source could not be reached or understood."""


def _network_reason(error: BaseException) -> str:
    """Describe a failed request, naming certificate trust when that is it.

    A verification failure arrives wrapped in a URLError and otherwise reads
    as if the server were unreachable, which sends people hunting for a
    network problem that isn't there.
    """
    cause: BaseException = error
    while isinstance(cause, urllib.error.URLError) and isinstance(
        cause.reason, BaseException
    ):
        cause = cause.reason

    if isinstance(cause, ssl.SSLCertVerificationError):
        return (
            f"TLS certificate verification failed ({cause}). "
            "This Python has no CA trust store — a uv-managed interpreter looks "
            "for one in /etc/ssl, which does not exist on RHEL 8. Set "
            "SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt, or run the tool "
            "with the system Python"
        )
    return str(error)


@dataclass(frozen=True)
class _Link:
    """One release asset: a name and the web URL of a job artifact."""

    name: str
    url: str


@dataclass(frozen=True)
class _Release:
    """A GitLab release, reduced to the parts this tool needs."""

    tag: str
    links: tuple[_Link, ...]


def _parse_releases(payload: object) -> list[_Release]:
    """Pull the tags and asset links out of a ``/releases`` response.

    Anything unexpected in the payload is skipped rather than raising: a
    single malformed release should not stop the others being listed.
    """
    if not isinstance(payload, list):
        raise SourceError("Expected a list of releases")

    releases: list[_Release] = []
    for entry in cast(list[object], payload):
        if not isinstance(entry, dict):
            continue
        item = cast(dict[str, object], entry)
        tag = item.get("tag_name")
        if not isinstance(tag, str) or not tag:
            continue
        assets = item.get("assets")
        raw_links: object = None
        if isinstance(assets, dict):
            raw_links = cast(dict[str, object], assets).get("links")
        links: list[_Link] = []
        if isinstance(raw_links, list):
            for raw in cast(list[object], raw_links):
                if not isinstance(raw, dict):
                    continue
                link = cast(dict[str, object], raw)
                url, name = link.get("url"), link.get("name")
                if isinstance(url, str) and url:
                    label = name if isinstance(name, str) else url
                    links.append(_Link(name=label, url=url))
        releases.append(_Release(tag=tag, links=tuple(links)))
    return releases


def _artifact_device(url_or_path: str, device: Device) -> bool:
    """Does this asset belong to `device`?

    Asset *names* have been spelled several different ways over the releases
    ("F405 firmware BIN", "D2AFE_F405_BIN"), so the path is the reliable
    discriminator.
    """
    return f"/{device.subdir}/" in url_or_path


class LocalSource:
    """Firmware read straight off the CI build area on the filesystem."""

    def __init__(self, base: Path = FIRMWARE_BASE) -> None:
        self.base = base

    @property
    def description(self) -> str:
        return f"Filesystem: {self.base}"

    def _release_dir(self, release: str, device: Device) -> Path:
        """Where `device`'s artifacts live inside a release.

        Releases normally keep each device's files in its own subdirectory,
        but older D2AFE releases dropped them straight into ``<release>/f405``.
        """
        target = self.base / release / TARGET
        preferred = target / device.subdir
        return preferred if preferred.is_dir() else target

    def list_releases(self, device: Device) -> list[str]:
        if not self.base.is_dir():
            return []

        found: list[Path] = []
        for release in self.base.iterdir():
            target = release / TARGET
            if not target.is_dir():
                continue
            if (target / device.subdir).is_dir():
                found.append(release)
            elif device is Device.D2AFE and any(target.glob("*.bin")):
                # Legacy layout: images sat directly in the target directory.
                found.append(release)

        return [r.name for r in sorted(found, key=lambda r: r.stat().st_mtime)]

    def artifact(self, release: str, device: Device, suffix: str) -> Artifact:
        path = self._find(release, device, suffix)
        return Artifact(name=path.name, path=path, ensure=lambda _log=None: path)

    def _find(self, release: str, device: Device, suffix: str) -> Path:
        searched: list[Path] = [self._release_dir(release, device)]
        if suffix == ".py" and device is not SCRIPT_DEVICE:
            # Only the D2AFE build publishes the CLI script, but it flashes
            # both boards.
            searched.append(self._release_dir(release, SCRIPT_DEVICE))

        for directory in searched:
            if not directory.is_dir():
                continue
            matches = sorted(directory.glob(f"*{suffix}"))
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise FileNotFoundError(
                    f"Expected one *{suffix} in {directory}, found {len(matches)}"
                )
        raise FileNotFoundError(
            f"No *{suffix} for {device.value} in release {release!r} under {self.base}"
        )


class GitLabSource:
    """Firmware pulled from the GitLab releases of the firmware project.

    Release assets are links to CI job artifacts. Their stored URLs are web
    URLs that redirect to a login page, so they are rewritten to the API form,
    which serves the bytes directly. Downloads are cached on disk.
    """

    def __init__(
        self,
        url: str = GITLAB_URL,
        project: str = GITLAB_PROJECT,
        token: str | None = None,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        timeout: float = _TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/")
        self.project = project
        self.token = token or os.environ.get("GITLAB_TOKEN")
        # Tags and filenames can be identical in different repositories.
        repository_id = sha256(f"{self.url}/{self.project}".encode()).hexdigest()[:16]
        self.cache_dir = cache_dir / repository_id
        self.timeout = timeout
        self._releases: list[_Release] | None = None

    @classmethod
    def from_repository(cls, repository: str) -> GitLabSource:
        """Configure a source from an HTTP(S) GitLab project URL.

        Nested namespaces and URLs copied from the releases page are accepted.
        Credentials belong in GITLAB_TOKEN rather than in the URL.
        """
        parsed = urlsplit(repository.strip())
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Use an HTTP(S) GitLab project URL without credentials, "
                "query parameters or a fragment"
            )
        # Accessing port validates malformed port numbers before any HTTP work.
        if parsed.port == 0:
            raise ValueError("The GitLab URL port must be between 1 and 65535")
        project = unquote(parsed.path).strip("/")
        project = project.removesuffix("/-/releases").removesuffix(".git")
        parts = project.split("/")
        if len(parts) < 2 or any(p in ("", ".", "..", "-") for p in parts):
            raise ValueError("The GitLab URL must include a namespace and project")
        return cls(url=f"{parsed.scheme}://{parsed.netloc}", project=project)

    @property
    def description(self) -> str:
        return f"GitLab: {self.url}/{self.project}/-/releases"

    @property
    def api(self) -> str:
        return f"{self.url}/api/v4/projects/{quote(self.project, safe='')}"

    # ---------------- HTTP ----------------

    def _open(self, url: str) -> Any:
        request = urllib.request.Request(url)
        if self.token:
            request.add_header("PRIVATE-TOKEN", self.token)
        return urllib.request.urlopen(request, timeout=self.timeout)  # noqa: S310

    def _fetch_releases(self, refresh: bool = False) -> list[_Release]:
        if self._releases is not None and not refresh:
            return self._releases
        # The API caps a page at RELEASES_PER_PAGE, so keep asking until a short page
        # comes back; the project will have more tags than that in time.
        newest_first: list[_Release] = []
        for page in range(1, MAX_RELEASE_PAGES + 1):
            url = f"{self.api}/releases?per_page={RELEASES_PER_PAGE}&page={page}"
            try:
                with self._open(url) as response:
                    payload = cast(object, json.load(response))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise SourceError(
                    f"Could not reach {self.url}: {_network_reason(e)}"
                ) from e
            except json.JSONDecodeError as e:
                raise SourceError(f"Unexpected response from {url}: {e}") from e

            batch = _parse_releases(payload)
            newest_first += batch
            if len(batch) < RELEASES_PER_PAGE:
                break

        # The API returns newest first; everything else here works oldest first.
        self._releases = list(reversed(newest_first))
        return self._releases

    def refresh(self) -> None:
        """Forget the cached release list so the next call re-fetches it."""
        self._releases = None

    # ---------------- Source interface ----------------

    def _release(self, tag: str) -> _Release:
        for release in self._fetch_releases():
            if release.tag == tag:
                return release
        raise FileNotFoundError(f"No release tagged {tag!r} in {self.project}")

    def list_releases(self, device: Device) -> list[str]:
        # A release only counts if it can actually flash this device; some
        # tags carry firmware for one board only.
        return [
            release.tag
            for release in self._fetch_releases()
            if any(
                link.url.endswith(".bin") and _artifact_device(link.url, device)
                for link in release.links
            )
        ]

    def artifact(self, release: str, device: Device, suffix: str) -> Artifact:
        link = self._find_link(release, device, suffix)
        url = self._api_url(link.url)
        name = Path(link.url).name
        target = self.cache_dir / release / name

        def ensure(log: LogFn | None = None) -> Path:
            return self._download(url, target, log)

        return Artifact(name=name, path=target, ensure=ensure)

    def _find_link(self, release: str, device: Device, suffix: str) -> _Link:
        links = self._release(release).links
        devices = [device]
        if suffix == ".py" and device is not SCRIPT_DEVICE:
            devices.append(SCRIPT_DEVICE)

        for candidate in devices:
            matches = [
                link
                for link in links
                if link.url.endswith(suffix) and _artifact_device(link.url, candidate)
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                names = ", ".join(sorted(m.name for m in matches))
                raise FileNotFoundError(
                    f"Release {release!r} has {len(matches)} *{suffix} assets for "
                    f"{candidate.value}: {names}"
                )
        raise FileNotFoundError(
            f"Release {release!r} publishes no *{suffix} for {device.value}"
        )

    def _api_url(self, web_url: str) -> str:
        match = _JOB_ARTIFACT_RE.search(web_url)
        if not match:
            raise FileNotFoundError(f"Not a job artifact link: {web_url}")
        return f"{self.api}/jobs/{match['job']}/artifacts/{match['path']}"

    def _download(self, url: str, target: Path, log: LogFn | None) -> Path:
        if target.exists() and target.stat().st_size:
            if log:
                log(f"Using cached {target.name}")
            return target

        if log:
            log(f"Downloading {target.name} from GitLab…")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        try:
            with self._open(url) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 and target.suffix == ".py":
                # Releases built before the firmware CI kept *.py in the build
                # job's artifacts link to a script that isn't in the archive.
                raise SourceError(
                    f"{target.name} is missing from this release's artifacts. "
                    "Releases built before the CI fix don't publish it — use a "
                    "script override, or the filesystem source."
                ) from e
            raise SourceError(
                f"Download failed for {target.name}: {_network_reason(e)}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise SourceError(
                f"Download failed for {target.name}: {_network_reason(e)}"
            ) from e

        # An expired or unauthorised artifact link answers 200 with the HTML
        # login page rather than an error, so check before trusting the bytes.
        if content_type.startswith("text/html"):
            raise SourceError(
                f"{target.name} came back as a web page, not a file. The "
                "artifact may have been removed, or need an access token."
            )
        if not data:
            raise SourceError(f"{target.name} downloaded empty")

        partial.write_bytes(data)
        partial.replace(target)
        if log:
            log(f"Fetched {target.name} ({len(data)} bytes)")
        return target
