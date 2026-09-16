"""Tests for both firmware sources. The GitLab one never touches the network."""

import json
import os
import time
import urllib.error
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from dls_d2bpm_tools.firmware import TARGET, Device
from dls_d2bpm_tools.sources import (
    MAX_RELEASE_PAGES,
    RELEASES_PER_PAGE,
    GitLabSource,
    LocalSource,
    SourceError,
)

from .conftest import make_release

# ---------------------------------------------------------------- LocalSource


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "firmware"
    make_release(root, "1.0.0", Device.D2AFE.subdir)
    make_release(root, "1.2.0", Device.D2AFE.subdir)
    make_release(root, "1.1.0", Device.D2PTD.subdir, files=["fw.bin"])
    make_release(root, "legacy", None)  # old flat D2AFE layout
    (root / "not-a-release").mkdir()
    for i, name in enumerate(["legacy", "1.0.0", "1.1.0", "1.2.0"]):
        stamp = time.time() + i
        os.utime(root / name, (stamp, stamp))
    return root


def test_local_lists_afe_including_legacy_layout(base: Path) -> None:
    assert LocalSource(base).list_releases(Device.D2AFE) == ["legacy", "1.0.0", "1.2.0"]


def test_local_lists_ptd_only_matching_subdir(base: Path) -> None:
    assert LocalSource(base).list_releases(Device.D2PTD) == ["1.1.0"]


def test_local_missing_base_lists_nothing(tmp_path: Path) -> None:
    assert LocalSource(tmp_path / "nope").list_releases(Device.D2AFE) == []


def test_local_artifact(base: Path) -> None:
    artifact = LocalSource(base).artifact("1.2.0", Device.D2AFE, ".bin")
    assert artifact.path == base / "1.2.0" / TARGET / Device.D2AFE.subdir / "fw.bin"
    assert artifact.obtain() == artifact.path  # nothing to download


def test_local_artifact_legacy_layout(base: Path) -> None:
    artifact = LocalSource(base).artifact("legacy", Device.D2AFE, ".bin")
    assert artifact.path == base / "legacy" / TARGET / "fw.bin"


def test_local_script_falls_back_to_the_afe_build(base: Path) -> None:
    """Only D2AFE ships the CLI script, but it flashes both boards."""
    make_release(base, "1.1.0", Device.D2AFE.subdir, files=["cli.py"])
    artifact = LocalSource(base).artifact("1.1.0", Device.D2PTD, ".py")
    assert artifact.path.name == "cli.py"
    assert Device.D2AFE.subdir in str(artifact.path)


def test_local_artifact_ambiguous(base: Path) -> None:
    (base / "1.2.0" / TARGET / Device.D2AFE.subdir / "other.bin").write_text("")
    with pytest.raises(FileNotFoundError, match="found 2"):
        LocalSource(base).artifact("1.2.0", Device.D2AFE, ".bin")


def test_local_artifact_missing(base: Path) -> None:
    with pytest.raises(FileNotFoundError):
        LocalSource(base).artifact("1.1.0", Device.D2PTD, ".zip")


# --------------------------------------------------------------- GitLabSource

WEB = "https://gitlab.example/diagnostics/d2afe-firmware/-/jobs/{job}/artifacts/file/{path}"


def _link(name: str, path: str, job: int = 900) -> dict[str, str]:
    return {"name": name, "url": WEB.format(job=job, path=path)}


AFE = Device.D2AFE.subdir
PTD = Device.D2PTD.subdir

# Newest first, the way the API returns them.
RELEASES: list[dict[str, Any]] = [
    {
        "tag_name": "0.9.3b",
        "assets": {
            "links": [
                _link("D2AFE_F405_BIN", f"{AFE}/build/d2afe-0.9.3b.bin"),
                _link("D2PTD_F405_BIN", f"{PTD}/build/d2ptd-0.9.3b.bin"),
                _link("d2afe-cli.py", f"{AFE}/build/d2afe-cli.py"),
                _link("D2AFE_F405_HEX", f"{AFE}/build/d2afe-0.9.3b.hex"),
            ]
        },
    },
    {  # a one-off tag, D2PTD only
        "tag_name": "hmc1119_1",
        "assets": {"links": [_link("D2PTD_F405_BIN", f"{PTD}/build/d2ptd-hmc.bin")]},
    },
    {  # an old release, D2AFE only, with the older asset naming
        "tag_name": "0.8.5",
        "assets": {
            "links": [
                _link("F405 firmware BIN", f"{AFE}/build/d2afe-0.8.5.bin", job=800),
                _link("d2afe-cli.py", f"{AFE}/build/d2afe-cli.py", job=800),
            ]
        },
    },
]


class FakeResponse:
    def __init__(self, body: bytes, content_type: str) -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeGitLab(GitLabSource):
    """A GitLabSource whose only HTTP call is replaced by a canned table."""

    def __init__(self, cache_dir: Path, **kwargs: Any) -> None:
        super().__init__(
            url="https://gitlab.example",
            project="diagnostics/d2afe-firmware",
            cache_dir=cache_dir,
            **kwargs,
        )
        self.requests: list[str] = []
        self.releases_payload: Any = RELEASES
        #: Optional per-page override, for testing pagination.
        self.releases_by_page: Any = None
        self.artifact_body = b"\x00firmware\xff"
        self.artifact_type = "application/octet-stream"
        self.fail: Exception | None = None

    def _open(self, url: str) -> Any:  # noqa: SLF001
        self.requests.append(url)
        if self.fail is not None:
            raise self.fail
        if "/releases" in url:
            payload = (
                self.releases_by_page(url)
                if self.releases_by_page
                else self.releases_payload
            )
            return FakeResponse(json.dumps(payload).encode(), "application/json")
        return FakeResponse(self.artifact_body, self.artifact_type)


@pytest.fixture
def gitlab(tmp_path: Path) -> Iterator[FakeGitLab]:
    yield FakeGitLab(cache_dir=tmp_path / "cache")


def test_lists_releases_oldest_first_per_device(gitlab: FakeGitLab) -> None:
    assert gitlab.list_releases(Device.D2AFE) == ["0.8.5", "0.9.3b"]
    assert gitlab.list_releases(Device.D2PTD) == ["hmc1119_1", "0.9.3b"]


def test_release_list_is_fetched_once(gitlab: FakeGitLab) -> None:
    gitlab.list_releases(Device.D2AFE)
    gitlab.list_releases(Device.D2PTD)
    assert len(gitlab.requests) == 1
    gitlab.refresh()
    gitlab.list_releases(Device.D2AFE)
    assert len(gitlab.requests) == 2


def test_web_url_is_rewritten_to_the_api(gitlab: FakeGitLab) -> None:
    """The stored URL redirects to a login page; the API serves the file."""
    gitlab.artifact("0.9.3b", Device.D2AFE, ".bin").obtain()
    download = gitlab.requests[-1]
    assert download == (
        "https://gitlab.example/api/v4/projects/diagnostics%2Fd2afe-firmware"
        f"/jobs/900/artifacts/{AFE}/build/d2afe-0.9.3b.bin"
    )
    assert "/-/jobs/" not in download


def test_artifact_is_selected_by_path_not_name(gitlab: FakeGitLab) -> None:
    """Asset names vary between releases; the path is the reliable key."""
    assert gitlab.artifact("0.9.3b", Device.D2AFE, ".bin").name == "d2afe-0.9.3b.bin"
    assert gitlab.artifact("0.9.3b", Device.D2PTD, ".bin").name == "d2ptd-0.9.3b.bin"
    assert gitlab.artifact("0.8.5", Device.D2AFE, ".bin").name == "d2afe-0.8.5.bin"


def test_script_falls_back_to_the_afe_build(gitlab: FakeGitLab) -> None:
    artifact = gitlab.artifact("0.9.3b", Device.D2PTD, ".py")
    assert artifact.name == "d2afe-cli.py"


def test_download_is_cached(gitlab: FakeGitLab) -> None:
    artifact = gitlab.artifact("0.9.3b", Device.D2AFE, ".bin")
    path = artifact.obtain()
    assert path.read_bytes() == gitlab.artifact_body
    before = len(gitlab.requests)
    assert artifact.obtain() == path
    assert len(gitlab.requests) == before, "should not re-download"


@pytest.mark.parametrize("different_host", [False, True])
def test_repositories_do_not_share_cached_firmware(
    tmp_path: Path, different_host: bool
) -> None:
    first = FakeGitLab(cache_dir=tmp_path)
    first.artifact_body = b"original firmware"
    original_path = first.artifact("0.9.3b", Device.D2AFE, ".bin").obtain()

    other = FakeGitLab(cache_dir=tmp_path)
    # Reconfigure the same fake transport with a different repository identity.
    GitLabSource.__init__(
        other,
        url="https://other.example" if different_host else first.url,
        project=first.project if different_host else "other/firmware",
        cache_dir=tmp_path,
    )
    other.artifact_body = b"fork firmware"
    other_path = other.artifact("0.9.3b", Device.D2AFE, ".bin").obtain()
    assert original_path != other_path
    assert original_path.read_bytes() == b"original firmware"
    assert other_path.read_bytes() == b"fork firmware"


@pytest.mark.parametrize("suffix", ["", "/", ".git", "/-/releases", "/-/releases/"])
def test_repository_url_configures_host_and_nested_project(suffix: str) -> None:
    source = GitLabSource.from_repository(
        f"https://gitlab.example:8443/team/subgroup/firmware{suffix}"
    )
    assert source.url == "https://gitlab.example:8443"
    assert source.project == "team/subgroup/firmware"
    assert source.api == (
        "https://gitlab.example:8443/api/v4/projects/team%2Fsubgroup%2Ffirmware"
    )
    assert "team/subgroup/firmware/-/releases" in source.description
    canonical = GitLabSource.from_repository(
        "https://gitlab.example:8443/team/subgroup/firmware"
    )
    assert source.cache_dir == canonical.cache_dir


@pytest.mark.parametrize(
    "repository",
    [
        "gitlab.example/team/firmware",
        "git@gitlab.example:team/firmware.git",
        "https://gitlab.example",
        "https://gitlab.example/project",
        "https://user:password@gitlab.example/team/firmware",
        "https://gitlab.example/team/firmware?token=secret",
        "https://gitlab.example/team/firmware#readme",
        "https://gitlab.example/team/../firmware",
        "https://gitlab.example:wrong/team/firmware",
        "https://gitlab.example:0/team/firmware",
    ],
)
def test_invalid_repository_url_is_rejected(repository: str) -> None:
    with pytest.raises(ValueError):
        GitLabSource.from_repository(repository)


def test_download_reports_progress(gitlab: FakeGitLab) -> None:
    lines: list[str] = []
    gitlab.artifact("0.9.3b", Device.D2AFE, ".bin").obtain(lines.append)
    assert any("Downloading" in line for line in lines)
    gitlab.artifact("0.9.3b", Device.D2AFE, ".bin").obtain(lines.append)
    assert any("cached" in line for line in lines)


def test_login_page_is_not_mistaken_for_firmware(gitlab: FakeGitLab) -> None:
    """An unauthorised artifact answers 200 with HTML, not an error."""
    gitlab.artifact_body = b"<!DOCTYPE html><html>Sign in</html>"
    gitlab.artifact_type = "text/html; charset=utf-8"
    with pytest.raises(SourceError, match="web page"):
        gitlab.artifact("0.9.3b", Device.D2AFE, ".bin").obtain()


def test_empty_download_is_rejected(gitlab: FakeGitLab) -> None:
    gitlab.artifact_body = b""
    with pytest.raises(SourceError, match="empty"):
        gitlab.artifact("0.9.3b", Device.D2AFE, ".bin").obtain()


def test_nothing_is_cached_when_a_download_fails(gitlab: FakeGitLab) -> None:
    artifact = gitlab.artifact("0.9.3b", Device.D2AFE, ".bin")
    gitlab.artifact_body = b""
    with pytest.raises(SourceError):
        artifact.obtain()
    assert not artifact.path.exists()


def test_missing_script_explains_the_ci_gap(gitlab: FakeGitLab) -> None:
    """Releases built before the CI fix link to a script that isn't there."""
    gitlab.list_releases(Device.D2AFE)  # prime the release list
    # Closed explicitly: an HTTPError is a file-like object wrapping a
    # tempfile, and leaving it to the garbage collector makes its finalizer
    # fire mid-test, which `filterwarnings = "error"` turns into a failure of
    # whichever unlucky test is running at the time.
    with urllib.error.HTTPError("url", 404, "Not Found", Message(), None) as error:
        gitlab.fail = error
        with pytest.raises(SourceError, match="before the CI fix"):
            gitlab.artifact("0.9.3b", Device.D2AFE, ".py").obtain()
    gitlab.fail = None


def test_unreachable_gitlab_is_reported(gitlab: FakeGitLab) -> None:
    gitlab.fail = urllib.error.URLError("no route to host")
    with pytest.raises(SourceError, match="Could not reach"):
        gitlab.list_releases(Device.D2AFE)


def test_unknown_release(gitlab: FakeGitLab) -> None:
    with pytest.raises(FileNotFoundError, match="No release tagged"):
        gitlab.artifact("9.9.9", Device.D2AFE, ".bin")


def test_release_without_the_requested_file(gitlab: FakeGitLab) -> None:
    with pytest.raises(FileNotFoundError, match="publishes no"):
        gitlab.artifact("hmc1119_1", Device.D2AFE, ".bin")


def test_ambiguous_assets_are_refused(gitlab: FakeGitLab) -> None:
    gitlab.releases_payload = [
        {
            "tag_name": "1.0.0",
            "assets": {
                "links": [
                    _link("one", f"{AFE}/build/a.bin"),
                    _link("two", f"{AFE}/build/b.bin"),
                ]
            },
        }
    ]
    with pytest.raises(FileNotFoundError, match="2 \\*.bin assets"):
        gitlab.artifact("1.0.0", Device.D2AFE, ".bin")


def test_malformed_releases_are_skipped_not_fatal(gitlab: FakeGitLab) -> None:
    gitlab.releases_payload = [
        "nonsense",
        {"no_tag": True},
        {"tag_name": "1.0.0", "assets": {"links": [_link("b", f"{AFE}/build/a.bin")]}},
        {"tag_name": "2.0.0", "assets": None},
    ]
    assert gitlab.list_releases(Device.D2AFE) == ["1.0.0"]


def test_unexpected_payload_is_reported(gitlab: FakeGitLab) -> None:
    gitlab.releases_payload = {"message": "404 Project Not Found"}
    with pytest.raises(SourceError, match="list of releases"):
        gitlab.list_releases(Device.D2AFE)


def test_token_is_sent_when_configured(tmp_path: Path) -> None:
    source = GitLabSource(token="abc123", cache_dir=tmp_path)
    assert source.token == "abc123"


def test_token_is_read_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "from-env")
    assert GitLabSource(cache_dir=tmp_path).token == "from-env"


def test_releases_are_paginated(gitlab: FakeGitLab) -> None:
    """More than one page of releases must all be listed, not just the first."""
    pages = {
        1: [
            {
                "tag_name": f"1.0.{n}",
                "assets": {"links": [_link("bin", f"{AFE}/build/a{n}.bin")]},
            }
            for n in range(RELEASES_PER_PAGE)
        ],
        2: [
            {
                "tag_name": "0.9.0",
                "assets": {"links": [_link("bin", f"{AFE}/build/old.bin")]},
            }
        ],
    }

    def by_page(url: str) -> Any:
        page = int(url.rsplit("page=", 1)[1])
        return pages.get(page, [])

    gitlab.releases_by_page = by_page
    releases = gitlab.list_releases(Device.D2AFE)

    assert len(releases) == RELEASES_PER_PAGE + 1
    # Oldest first: the second page holds the older tags.
    assert releases[0] == "0.9.0"
    assert releases[-1] == "1.0.0"


def test_pagination_stops_on_a_short_page(gitlab: FakeGitLab) -> None:
    """A page shorter than the limit is the last one; don't keep asking."""
    gitlab.list_releases(Device.D2AFE)
    assert len([r for r in gitlab.requests if "/releases" in r]) == 1


def test_pagination_is_bounded(gitlab: FakeGitLab) -> None:
    """A source that always returns a full page must not loop forever."""
    full_page = [
        {
            "tag_name": f"1.0.{n}",
            "assets": {"links": [_link("bin", f"{AFE}/build/a{n}.bin")]},
        }
        for n in range(RELEASES_PER_PAGE)
    ]

    def always_full(_url: str) -> Any:
        return full_page

    gitlab.releases_by_page = always_full
    gitlab.list_releases(Device.D2AFE)
    assert len([r for r in gitlab.requests if "/releases" in r]) == MAX_RELEASE_PAGES
