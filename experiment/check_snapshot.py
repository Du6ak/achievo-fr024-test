"""Intentionally snapshot-based check: an experiment, not a merge guard."""

import json
import os
import re
import sys
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MAX_PAGES = 100


def key(title):
    # Test protocol: PR title supplies the synthetic guide's identity.
    parts = [part.strip() for part in title.split("|")]
    if len(parts) != 5 or parts[0] != "SYNTH" or not all(parts[1:]):
        raise ValueError("each open test PR must use the SYNTH title protocol")
    if not parts[1].isdigit():
        raise ValueError("invalid synthetic app id")
    return (parts[1], parts[2], parts[3].lower(), " ".join(parts[4].split()).casefold())


def conflict_numbers(prs, current_number, base):
    candidates = [p for p in prs if p["base"]["ref"] == base]
    current = [p for p in candidates if p["number"] == current_number]
    if len(current) != 1:
        raise ValueError("current PR is not present in the complete open snapshot")
    # Unknown title is a failure, not proof of uniqueness.
    keys = [(p["number"], key(p["title"])) for p in candidates]
    current_key = key(current[0]["title"])
    return [number for number, candidate_key in keys if number != current_number and candidate_key == current_key]


def get_open_prs(api_url, repository, token):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid repository")
    if api_url != "https://api.github.com":
        raise ValueError("this experiment targets github.com only")
    result = []
    for page in range(1, MAX_PAGES + 1):
        query = urlencode({"state": "open", "per_page": 100, "page": page})
        request = Request(
            f"{api_url}/repos/{repository}/pulls?{query}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "fr024-experiment"},
        )
        with urlopen(request, timeout=10) as response:
            batch = json.load(response)
        if not isinstance(batch, list):
            raise ValueError("invalid response")
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise ValueError("pagination limit reached: snapshot may be incomplete")


def main():
    if os.environ.get("EXPERIMENT_FORCE_UNAVAILABLE") == "1":
        raise ValueError("simulated check unavailability")
    pause = int(os.environ.get("EXPERIMENT_PAUSE_SECONDS") or "0")
    if not 0 <= pause <= 120:
        raise ValueError("pause must be between 0 and 120 seconds")
    token = os.environ["GH_TOKEN"]
    if not token:
        raise ValueError("missing token")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as event_file:
        event = json.load(event_file)
    pr = event["pull_request"]
    number = pr["number"]
    base = pr["base"]["ref"]
    prs = get_open_prs(os.environ.get("GITHUB_API_URL", ""), os.environ["GITHUB_REPOSITORY"], token)
    conflicts = conflict_numbers(prs, number, base)
    print(f"SNAPSHOT_TAKEN pr={number} open_count={len(prs)} conflicts={conflicts} pause={pause}", flush=True)
    # Deliberately no re-read: provides a reproducible stale-success race.
    time.sleep(pause)
    if conflicts:
        print("CONFLICT: snapshot found another open PR with the same synthetic key", flush=True)
        return 1
    print("SNAPSHOT_SUCCESS: no conflict in the earlier snapshot; this does NOT prove current safety", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, URLError, json.JSONDecodeError) as exc:
        # No raw HTTP response, token, body, author, or title in logs.
        print(f"CHECK_UNAVAILABLE: {type(exc).__name__}", file=sys.stderr, flush=True)
        sys.exit(1)
