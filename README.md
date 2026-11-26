# OSS Contribution Radar

Forked from [Sun-Wukhan/oss-contribution-radar](https://github.com/Sun-Wukhan/oss-contribution-radar).

[![Daily radar](https://github.com/okclifto/oss-contribution-radar/actions/workflows/daily-radar.yml/badge.svg)](https://github.com/okclifto/oss-contribution-radar/actions/workflows/daily-radar.yml)
[![Tests](https://github.com/okclifto/oss-contribution-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/okclifto/oss-contribution-radar/actions/workflows/ci.yml)

OSS Contribution Radar publishes a daily, ranked shortlist of genuine contribution
opportunities from established infrastructure projects.

The generated [`REPORT.md`](REPORT.md) is a discovery aid, not an autonomous contribution
bot. It never comments on third-party issues, claims work, opens pull requests, or writes to
external repositories. A human must review each issue, follow the target project's contribution
guide, implement and test a substantive change, and decide whether to submit it.

## How selection works

Candidates must satisfy every eligibility rule:

- The repository is active and above its configured star threshold.
- The issue is open and unassigned.
- Maintainers labeled it `good first issue`, `help wanted`, or an equivalent invitation.
  Matching ignores case, hyphens, underscores, emoji and a `prefix/`, so `good-first-issue`,
  `kind/help-wanted` and `good first issue :baby_chick:` all count.
- No currently open pull request declares that it closes the issue.

Ranking favors explicit beginner-friendly labels, bugs and documentation work, active discussions,
recent updates, and established repositories. The policy is intentionally conservative: an empty
report is better than a low-quality recommendation.

## Daily automation

The `Daily contribution radar` workflow runs at 13:00 UTC every day and can also be started
manually. It:

1. Uses GitHub's read-only public API to collect repository and issue metadata.
2. Generates `REPORT.md`.
3. Publishes the report in the workflow summary.
4. Opens a pull request with the changed report as `github-actions[bot]`, which merges once checks pass.

The workflow can write to this repository and open pull requests in it. It cannot modify
third-party repositories.

## Local usage

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are recommended.

```bash
uv sync
GITHUB_TOKEN="$(gh auth token)" uv run contribution-radar
```

The token is optional for small scans, but authenticated requests receive a higher GitHub API rate
limit. Use a short-lived token and never commit it.

The radar scans 8 repositories at a time. Use `--workers 1` to scan one at a time, or up to
`--workers 16`; higher values risk GitHub's secondary rate limits.

Customize repository targets and minimum popularity thresholds in
[`config/repositories.json`](config/repositories.json). A target can also list `labels` for a
project's own invitation labels that the defaults don't cover:

```json
{ "name": "traefik/traefik", "minimum_stars": 40000, "labels": ["contributor/wanted"] }
```

Repositories that were checked and deliberately left out go in the `dropped` list with the
date and the reason, so the decision isn't lost:

```json
"dropped": [
  { "name": "hashicorp/nomad", "dropped_on": "2026-11-26", "reason": "No contributor labels." }
]
```

The radar never scans dropped repositories, and a name can't be both a target and dropped.

The radar reads each repository's labels, then fetches the 100 most recently updated open
issues under each matching label.

## Development

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

Test coverage must remain at or above 90%.

## Architecture

- `github_client.py` is the read-only GitHub data adapter.
- `service.py` contains eligibility and ranking business rules.
- `report.py` renders the public Markdown artifact.
- `cli.py` validates inputs and coordinates the application.

This separation keeps external API access out of the ranking logic and makes the policy
deterministically testable.

## Contributing

Please open an issue before making a substantial change. Pull requests should include tests,
type annotations, docstrings, and a clear explanation of the ranking behavior they change.

## License

MIT
