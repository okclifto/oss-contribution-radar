"""Command-line controller for daily report generation."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from contribution_radar.github_client import GitHubClient
from contribution_radar.models import RepositoryScan, RepositoryTarget, ScanStatus
from contribution_radar.report import render_report
from contribution_radar.service import ContributionRadar


def parse_args() -> argparse.Namespace:
    """Parse and validate command-line options."""

    parser = argparse.ArgumentParser(
        description="Find review-ready issues in established public repositories."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/repositories.json"),
        help="JSON repository target configuration",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("REPORT.md"),
        help="generated Markdown report path",
    )
    parser.add_argument("--limit", type=int, default=10, choices=range(1, 51))
    return parser.parse_args()


def load_targets(path: Path) -> list[RepositoryTarget]:
    """Load validated repository targets from JSON."""

    with path.open(encoding="utf-8") as config_file:
        payload = cast(object, json.load(config_file))

    if not isinstance(payload, dict) or not isinstance(payload.get("repositories"), list):
        raise ValueError("config must contain a 'repositories' list")

    targets: list[RepositoryTarget] = []
    seen: set[str] = set()
    for raw_target in cast(list[Any], payload["repositories"]):
        if not isinstance(raw_target, dict):
            raise ValueError("each repository target must be an object")
        name = raw_target.get("name")
        minimum_stars = raw_target.get("minimum_stars", 1_000)
        if not isinstance(name, str) or not name:
            raise ValueError("repository target name must be a non-empty string")
        if (
            not isinstance(minimum_stars, int)
            or isinstance(minimum_stars, bool)
            or minimum_stars < 0
        ):
            raise ValueError(f"invalid minimum_stars for {name!r}")
        labels = raw_target.get("labels", [])
        if not isinstance(labels, list) or not all(
            isinstance(label, str) and label.strip() for label in labels
        ):
            raise ValueError(f"labels for {name!r} must be a list of non-empty strings")
        if name.casefold() in seen:
            raise ValueError(f"duplicate repository target: {name}")
        seen.add(name.casefold())
        targets.append(
            RepositoryTarget(
                name=name,
                minimum_stars=minimum_stars,
                labels=tuple(cast(list[str], labels)),
            )
        )

    if not targets:
        raise ValueError("at least one repository target is required")
    return targets


def write_report(path: Path, content: str) -> None:
    """Atomically write the generated report."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as temporary_file:
        temporary_file.write(content)
        temporary_path = Path(temporary_file.name)
    temporary_path.replace(path)


def _escape_workflow_data(value: str) -> str:
    """Escape text for a GitHub Actions workflow command message."""

    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def report_scan(scan: RepositoryScan) -> None:
    """Print one repository's outcome, with an Actions annotation when relevant."""

    symbol = {ScanStatus.SCANNED: "✓", ScanStatus.SKIPPED: "-", ScanStatus.FAILED: "✗"}
    print(f"{symbol[scan.status]} {scan.name}: {scan.status} ({scan.detail})", flush=True)

    if os.getenv("GITHUB_ACTIONS") != "true" or scan.status is ScanStatus.SCANNED:
        return
    level = "error" if scan.status is ScanStatus.FAILED else "warning"
    title = "Repository scan failed" if level == "error" else "Repository skipped"
    message = _escape_workflow_data(f"{scan.name}: {scan.detail}")
    print(f"::{level} title={title}::{message}", flush=True)


def main() -> None:
    """Generate the daily contribution report.

    Exits with status 1 only when every repository failed, so a partial outage still
    publishes a report while a total outage fails the workflow visibly.
    """

    args = parse_args()
    targets = load_targets(args.config)
    generated_at = datetime.now(UTC).replace(microsecond=0)
    radar = ContributionRadar(GitHubClient(token=os.getenv("GITHUB_TOKEN")))

    print(f"Scanning {len(targets)} repositories from {args.config}", flush=True)
    result = radar.scan(targets, limit=args.limit, now=generated_at, on_repository=report_scan)
    report = render_report(result.candidates, generated_at, result.repositories)
    write_report(args.output, report)

    failed = result.count(ScanStatus.FAILED)
    print(
        f"Scanned {result.count(ScanStatus.SCANNED)}/{len(targets)} repositories "
        f"({result.count(ScanStatus.SKIPPED)} skipped, {failed} failed). "
        f"Wrote {len(result.candidates)} opportunities to {args.output}"
    )
    if failed == len(targets):
        raise SystemExit("Every repository scan failed; see the errors above.")


if __name__ == "__main__":
    main()
