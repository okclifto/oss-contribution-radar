"""Unit tests for configuration and CLI orchestration."""

import argparse
import json
import sys
from pathlib import Path

import pytest

from contribution_radar import cli
from contribution_radar.models import RepositoryScan, RepositoryTarget, ScanResult, ScanStatus
from contribution_radar.service import ScanObserver


def write_json(path: Path, payload: object) -> None:
    """Write a compact JSON test fixture."""

    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_targets_validates_and_normalizes_configuration(tmp_path: Path) -> None:
    """Load valid targets and apply the default popularity threshold."""

    config = tmp_path / "repositories.json"
    write_json(
        config,
        {
            "repositories": [
                {"name": "owner/one"},
                {"name": "owner/two", "minimum_stars": 42},
                {"name": "owner/three", "labels": ["contributor/wanted"]},
            ]
        },
    )

    targets = cli.load_targets(config)

    assert targets == [
        RepositoryTarget("owner/one", 1_000),
        RepositoryTarget("owner/two", 42),
        RepositoryTarget("owner/three", 1_000, ("contributor/wanted",)),
    ]


@pytest.mark.parametrize(
    "payload,error",
    [
        ({}, "repositories"),
        ({"repositories": []}, "at least one"),
        ({"repositories": ["owner/repo"]}, "must be an object"),
        ({"repositories": [{"name": ""}]}, "non-empty string"),
        ({"repositories": [{"name": "owner"}]}, "owner/repo"),
        ({"repositories": [{"name": "owner/repo/extra"}]}, "owner/repo"),
        ({"repositories": [{"name": "owner/repo", "minimum_stars": True}]}, "minimum_stars"),
        ({"repositories": [{"name": "owner/repo", "labels": "help"}]}, "labels"),
        ({"repositories": [{"name": "owner/repo", "labels": [" "]}]}, "labels"),
        (
            {"repositories": [{"name": "Owner/Repo"}, {"name": "owner/repo"}]},
            "duplicate",
        ),
    ],
)
def test_load_targets_rejects_invalid_configuration(
    tmp_path: Path,
    payload: object,
    error: str,
) -> None:
    """Reject malformed configuration before making network requests."""

    config = tmp_path / "repositories.json"
    write_json(config, payload)

    with pytest.raises(ValueError, match=error):
        cli.load_targets(config)


VALID_TARGET = {"name": "owner/repo"}
DROPPED = {"name": "owner/old", "dropped_on": "2026-11-26", "reason": "no beginner labels"}


def test_load_targets_accepts_dropped_repositories(tmp_path: Path) -> None:
    """Keep dropped repositories out of the scan while accepting the record."""

    config = tmp_path / "repositories.json"
    write_json(config, {"repositories": [VALID_TARGET], "dropped": [DROPPED]})

    assert cli.load_targets(config) == [RepositoryTarget("owner/repo")]


@pytest.mark.parametrize(
    "dropped,error",
    [
        ({"owner/old": DROPPED}, "must be a list"),
        (["owner/old"], "must be an object"),
        ([{**DROPPED, "name": ""}], "non-empty string"),
        ([{**DROPPED, "name": "old"}], "owner/repo"),
        ([{**DROPPED, "dropped_on": "26/11/2026"}], "YYYY-MM-DD"),
        ([{key: value for key, value in DROPPED.items() if key != "dropped_on"}], "YYYY-MM-DD"),
        ([{**DROPPED, "reason": " "}], "needs a reason"),
        ([{**DROPPED, "name": "Owner/Repo"}], "both a target and dropped"),
        ([DROPPED, {**DROPPED, "name": "OWNER/old"}], "duplicate dropped"),
    ],
)
def test_load_targets_rejects_invalid_dropped_entries(
    tmp_path: Path,
    dropped: object,
    error: str,
) -> None:
    """Reject a malformed dropped record and names that are also active."""

    config = tmp_path / "repositories.json"
    write_json(config, {"repositories": [VALID_TARGET], "dropped": dropped})

    with pytest.raises(ValueError, match=error):
        cli.load_targets(config)


def test_shipped_configuration_is_valid() -> None:
    """Catch a broken config/repositories.json before the daily run does."""

    config = Path(__file__).resolve().parents[1] / "config" / "repositories.json"

    targets = cli.load_targets(config)

    assert targets


def test_write_report_creates_parent_and_replaces_content(tmp_path: Path) -> None:
    """Write complete reports atomically into a new directory."""

    output = tmp_path / "nested" / "REPORT.md"

    cli.write_report(output, "first")
    cli.write_report(output, "second")

    assert output.read_text(encoding="utf-8") == "second"


def test_parse_args_applies_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose predictable defaults for local and scheduled runs."""

    monkeypatch.setattr(sys, "argv", ["contribution-radar"])

    args = cli.parse_args()

    assert args.config == Path("config/repositories.json")
    assert args.output == Path("REPORT.md")
    assert args.limit == 10
    assert args.workers == 8


@pytest.mark.parametrize("workers", ["0", "17"])
def test_parse_args_rejects_out_of_range_workers(
    monkeypatch: pytest.MonkeyPatch, workers: str
) -> None:
    """Keep concurrency within what GitHub tolerates."""

    monkeypatch.setattr(sys, "argv", ["contribution-radar", "--workers", workers])

    with pytest.raises(SystemExit):
        cli.parse_args()


def install_fake_radar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scans: list[RepositoryScan],
) -> Path:
    """Point the CLI at temp files and a radar that reports the given outcomes."""

    config = tmp_path / "repositories.json"
    output = tmp_path / "REPORT.md"
    write_json(config, {"repositories": [{"name": scan.name} for scan in scans]})
    monkeypatch.setattr(
        cli,
        "parse_args",
        lambda: argparse.Namespace(config=config, output=output, limit=3, workers=4),
    )

    class FakeRadar:
        """Deterministic replacement for the ranking service."""

        def __init__(self, gateway: object) -> None:
            """Accept the controller's gateway dependency."""

            assert gateway is not None

        def scan(
            self,
            targets: list[RepositoryTarget],
            limit: int,
            now: object,
            on_repository: ScanObserver,
            max_workers: int,
        ) -> ScanResult:
            """Validate controller inputs and replay canned outcomes."""

            assert [target.name for target in targets] == [scan.name for scan in scans]
            assert limit == 3
            assert max_workers == 4
            assert now is not None
            for scan in scans:
                on_repository(scan)
            return ScanResult(candidates=(), repositories=tuple(scans))

    monkeypatch.setattr(cli, "ContributionRadar", FakeRadar)
    return output


def test_main_orchestrates_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Connect configuration, scanning, rendering, and persistence with progress output."""

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    output = install_fake_radar(
        monkeypatch,
        tmp_path,
        [
            RepositoryScan("owner/repo", ScanStatus.SCANNED, "0 open items checked, 0 eligible"),
            RepositoryScan("owner/down", ScanStatus.FAILED, "HTTP Error 502"),
        ],
    )

    cli.main()

    out = capsys.readouterr().out
    assert "No unassigned" in output.read_text(encoding="utf-8")
    assert "Scanning 2 repositories" in out
    assert "(4 at a time)" in out
    assert "✓ owner/repo: scanned" in out
    assert "✗ owner/down: failed (HTTP Error 502)" in out
    assert "Scanned 1/2 repositories (0 skipped, 1 failed)" in out
    assert "::error" not in out


def test_main_fails_when_every_repository_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fail the run on a total outage, after still writing the diagnostic report."""

    output = install_fake_radar(
        monkeypatch,
        tmp_path,
        [RepositoryScan("owner/down", ScanStatus.FAILED, "network unreachable")],
    )

    with pytest.raises(SystemExit, match="Every repository scan failed"):
        cli.main()

    assert "❌ failed" in output.read_text(encoding="utf-8")


def test_report_scan_emits_github_actions_annotations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Annotate failures as errors and skips as warnings, escaping multi-line details."""

    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    cli.report_scan(RepositoryScan("owner/a", ScanStatus.FAILED, "100% broken\nretry"))
    cli.report_scan(RepositoryScan("owner/b", ScanStatus.SKIPPED, "archived"))
    cli.report_scan(RepositoryScan("owner/c", ScanStatus.SCANNED, "fine"))

    lines = capsys.readouterr().out.splitlines()
    assert "::error title=Repository scan failed::owner/a: 100%25 broken%0Aretry" in lines
    assert "::warning title=Repository skipped::owner/b: archived" in lines
    assert not any(line.startswith("::") and "owner/c" in line for line in lines)
