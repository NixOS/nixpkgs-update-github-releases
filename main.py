#!/usr/bin/env python3

import datetime
import json
import logging
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from itertools import count
from json.decoder import JSONDecodeError
from pathlib import Path
from pprint import pformat
from time import sleep
from urllib.parse import urljoin, urlparse

import libversion
import pydantic
import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache


def main():
    packages = eval_packages()
    for pkg in packages:
        update = find_update(pkg)
        if update:
            print(pkg.name, pkg.version, *update, flush=True)


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
_logger = logging.getLogger(__name__)

DOT = Path(__file__).resolve().parent
LOAD_META_FROM_PATH = DOT / "loadMetaFromPath.nix"
MASTER = "https://github.com/nixos/nixpkgs/archive/master.tar.gz"

CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    / "nixpkgs-update-github-releases"
)

_logger.info("Cache dir: %s", CACHE_DIR.resolve())

# Keep stats about caching
CACHE_STATS = defaultdict(int)

sess = requests.session()

try:
    API_TOKEN_PATH = os.environ.get("API_TOKEN_FILE", DOT / "API_TOKEN")
    with open(API_TOKEN_PATH, "r") as token_file:
        API_TOKEN = token_file.read().strip()

except:
    API_TOKEN = os.environ.get("API_TOKEN")


if API_TOKEN is not None:
    username, token = API_TOKEN.split(":")
    sess.auth = (username, token)

else:
    _logger.info(
        "No API token set! You can do this by setting the environment variable "
        "API_TOKEN to `<username>:<personal access token>`"
    )

HTTP = CacheControl(sess, cache=FileCache(CACHE_DIR.resolve()))


class Package(pydantic.BaseModel):
    name: str
    version: str
    pages: list[str]


def eval_packages(url=MASTER) -> list[Package]:
    with tempfile.NamedTemporaryFile(mode="w") as f:
        subprocess.check_call(
            [
                "nix-env",
                "-f",
                "<nixpkgs>",
                "-I",
                f"nixpkgs={url}",
                "-qaP",
                "--no-name",
                "--arg",
                "config",
                'import (<nixpkgs> + "/pkgs/top-level/packages-config.nix")',
            ],
            stdout=f,
        )
        json_output = subprocess.check_output(
            [
                "nix-instantiate",
                str(LOAD_META_FROM_PATH),
                "--arg",
                "universeFile",
                f.name,
                "--arg",
                "url",
                str(url),
                "--eval",
                "--json",
                "--read-write-mode",
            ]
        )

    # seems github is flaky, reverse fetch order for better distribution
    hour = datetime.datetime.now().hour
    data = pydantic.TypeAdapter(list[Package]).validate_json(json_output)
    if hour > 11:
        data = list(reversed(data))
    return data


def find_update(pkg: Package) -> tuple[str, str] | None:
    # TODO: check if it has an updateScript
    # skip python3*, packages have an updateScript
    if pkg.name.startswith("python3"):
        return None

    # skip typstPackages*, package set
    if pkg.name.startswith("typstPackages"):
        return None

    for url in pkg.pages:
        userRepo = getUserRepoPair(url)
        if userRepo is not None:
            break
    else:
        return None

    user, repo = userRepo

    if "-unstable-" in pkg.version or pkg.version.startswith("unstable-"):
        # nixpkgs-update doesn't support updating the rev.
        _logger.debug(
            "skipping package %s because the current version %s contains unstable-",
            pkg.name,
            pkg.version,
        )
        return None

    latest_version = find_latest_version_of_repo(url)
    if latest_version is None:
        return None

    if libversion.version_compare(pkg.version, latest_version) >= 0:
        _logger.debug(
            "skipping package %s because %s is >= the current version %s according to libversion",
            pkg.name,
            pkg.version,
            latest_version,
        )
        return None

    return latest_version, f"https://github.com/{user}/{repo}/releases"


def getUserRepoPair(url):
    try:
        parsed = urlparse(url)
    except AttributeError:
        return

    if parsed.netloc != "github.com":
        return

    m = re.match(
        r"""
      ^(?:/downloads)? # Some download links use /downloads/owner/repo/version/, this filters that.
      /([^/]+) # owner
      /([^/]+) # repo
      (?:/?|/wiki/?|/.+\.tar.gz|/releases/.+|/archive/.+|/tarball/.+)$
      """,
        parsed.path,
        re.VERBOSE,
    )
    if m is None:
        _logger.info("Could not parse github url: %s", url)
        return

    user, repo = m.groups()
    return user, repo


def sleepUntil(timestamp):
    if not isinstance(timestamp, datetime.datetime):
        timestamp = datetime.datetime.fromtimestamp(timestamp)

    _logger.info("Sleeping until %s", timestamp)

    now = datetime.datetime.now()
    while now < timestamp:
        timeDiff = timestamp - datetime.datetime.now()
        _logger.info("%s left", timeDiff)
        toSleep = timeDiff / 2
        sleep(toSleep.total_seconds() + 1)
        now = datetime.datetime.now()


def getEndpoint(endpoint, base="https://api.github.com/", max_retries=10):
    url = urljoin(base, endpoint)
    error_sleep = 1
    for _ in range(max_retries):
        resp = HTTP.get(url)
        from_cache: bool = getattr(resp, "from_cache")
        status = resp.status_code

        # Save cache stats:
        CACHE_STATS[from_cache] += 1

        if status == 500:
            _logger.info("Host is having trouble. Let's give them some time.")
            sleep(error_sleep)
            error_sleep *= 2
            continue

        if status == 451:
            _logger.info("Endpoint %s Unavailable For Legal Reasons", endpoint)
            return

        if status == 404:
            _logger.info("Endpoint %s not found", endpoint)
            return

        if status == 403:
            message = resp.json().get("message", "")
            if message:
                _logger.info("%s", message)

            if "exceeded" in message:
                # Fall through to rateRemaining logic
                pass
            elif "abuse" in message:
                sleep(10)
                continue
            elif "blocked" in message:
                _logger.info("Endpoint %s blocked", endpoint)
                return
            else:
                raise Exception("Got 403, but we can't tell why.", message)

        rateRemaining = resp.headers.get("X-RateLimit-Remaining")
        if rateRemaining is None:
            _logger.info("Host did not send X-RateLimit-Remaining header.")
            _logger.info("Status code: %s", resp.status_code)

            sleep(1)
            continue

        rateRemaining = int(rateRemaining)

        if not from_cache and rateRemaining % 100 == 0:
            _logger.info("%s requests remaining this hour!", rateRemaining)

        if rateRemaining == 0:
            _logger.info("No rate :(")
            _logger.info("%s", pformat(dict(resp.headers)))
            sleepUntil(int(resp.headers["X-Ratelimit-Reset"]))
            sleep(5)  # in case of clock disagreement, add a little buffer
            continue

        try:
            return resp.json()
        except JSONDecodeError:
            # GitHub returns empty JSON sometimes? Very flaky, but anoying if it happens...
            # https://github.com/nix-community/nixpkgs-update-github-releases/issues/8
            sleep(error_sleep)
            error_sleep *= 2
            continue
    else:
        raise Exception(f"No good response after {max_retries} tries")


def iterReleases(user, repo):
    """
    See also:

    "GET /repos/:owner/:repo/releases"
    https://developer.github.com/v3/repos/releases/#get-the-latest-release
    """

    for page in count(1):
        if page > 1:
            _logger.info("Fetching page %s for %s/%s", page, user, repo)

        result = getEndpoint(f"/repos/{user}/{repo}/releases?page={page}")

        if result is None:
            return

        yield from result

        # 30 seems to be the maximum number of releases the API is willing to
        # return on a single page. The API is perfectly willing to return an
        # empty page if we request out-of-bounds pages, but this saves requests.
        if len(result) < 30:
            return


def latestRelease(user, repo):
    releases = iterReleases(user, repo)

    verboseMatch = False
    for tag in releases:
        release = tag.get("tag_name")
        if tag.get("prerelease"):
            continue
        if skipPrerelease(release):
            _logger.info("Skipping non-tagged prerelease %s", release)
            verboseMatch = True
            continue
        if verboseMatch:
            _logger.info("Rescued it with %s :)", release)
        break
    else:
        # No matching releases
        return

    return release


def removePrefix(prefix, string):
    if not string.startswith(prefix):
        return string

    return string[len(prefix) :]


def stripRelease(repo, release):
    rawPrefixes = [*"v r version release stable".split(), repo]
    joiners = [*"- _ . /".split(), ""]
    modifiers = [str.lower, str.upper, str.title, lambda x: x]
    prefixes = [
        modifier(raw) + joiner
        for modifier in modifiers
        for joiner in joiners
        for raw in rawPrefixes
    ]

    for prefix in prefixes:
        release = removePrefix(prefix, release)
    return release


# Filter out pre-releases that weren't marked on GitHub as such
def skipPrerelease(release):
    release = release.lower()
    markers = [
        "nightly",
        "develop",
        "rc",
        "alpha",
        "beta",
        "snapshot",
        "testing",
    ]

    return any(marker in release for marker in markers)


def find_latest_version_of_repo(homepage: str) -> str | None:
    userRepo = getUserRepoPair(homepage)

    if userRepo is None:
        return

    nextVersion = latestRelease(*userRepo)

    if nextVersion is None:
        return

    if skipPrerelease(nextVersion):
        return

    nextVersion = stripRelease(userRepo[1], nextVersion)

    return nextVersion


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _logger.info("Shutting down...")
    finally:
        _logger.info("Cached stats:")
        _logger.info("%s", pformat(dict(CACHE_STATS)))
