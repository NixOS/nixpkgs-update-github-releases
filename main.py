#!/usr/bin/env python3

import subprocess
import json
from json.decoder import JSONDecodeError
import re
import requests
import datetime
import dateutil.parser
from urllib.parse import urlparse, urljoin
import libversion
import tempfile

from cachecontrol import CacheControl
from cachecontrol.caches import FileCache
from pathlib import Path
from pprint import pprint
from time import sleep
from functools import partial
from collections import defaultdict
from itertools import count

import os
import sys

log = partial(print, file=sys.stderr)
plog = partial(pprint, stream=sys.stderr)

DOT = Path(__file__).resolve().parent
LOAD_META_FROM_PATH = DOT / "loadMetaFromPath.nix"
MASTER = "https://github.com/nixos/nixpkgs/archive/master.tar.gz"

CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    / "nixpkgs-update-github-releases"
)

log("Cache dir:", CACHE_DIR.resolve())

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
    sess.auth = tuple(API_TOKEN.split(":"))

else:
    log(
        "No API token set! You can do this by setting the environment variable "
        "API_TOKEN to `<username>:<personal access token>`"
    )

HTTP = CacheControl(sess, cache=FileCache(CACHE_DIR.resolve()))


def loadVersions(url=MASTER):
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
    data = json.loads(json_output)
    if hour > 11:
        data = {key: data[key] for key in reversed(data)}
    return data


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
        log(f"Could not parse github url: {url}")
        return

    user, repo = m.groups()
    return user, repo


def sleepUntil(timestamp):
    if not isinstance(timestamp, datetime.datetime):
        timestamp = datetime.datetime.fromtimestamp(timestamp)

    log("Sleeping until", timestamp)

    now = datetime.datetime.now()
    while now < timestamp:
        timeDiff = timestamp - datetime.datetime.now()
        log(timeDiff, "left")
        toSleep = timeDiff / 2
        sleep(toSleep.total_seconds() + 1)
        now = datetime.datetime.now()


def getEndpoint(endpoint, base="https://api.github.com/", max_retries=10):
    url = urljoin(base, endpoint)
    error_sleep = 1
    for _ in range(max_retries):
        resp = HTTP.get(url)
        status = resp.status_code

        # Save cache stats:
        CACHE_STATS[resp.from_cache] += 1

        if status == 500:
            log("Host is having trouble. Let's give them some time.")
            sleep(error_sleep)
            error_sleep *= 2
            continue

        if status == 451:
            log("Endpoint", endpoint, "Unavailable For Legal Reasons")
            return

        if status == 404:
            log("Endpoint", endpoint, "not found")
            return

        if status == 403:
            message = resp.json().get("message", "")
            if message:
                log(message)

            if "exceeded" in message:
                # Fall through to rateRemaining logic
                pass
            elif "abuse" in message:
                sleep(10)
                continue
            elif "blocked" in message:
                log("Endpoint", endpoint, "blocked")
                return
            else:
                raise Exception("Got 403, but we can't tell why.", message)

        rateRemaining = resp.headers.get("X-RateLimit-Remaining")
        if rateRemaining is None:
            log("Host did not send X-RateLimit-Remaining header.")
            log("Status code:", resp.status_code)

            sleep(1)
            continue

        rateRemaining = int(rateRemaining)

        if not resp.from_cache and rateRemaining % 100 == 0:
            log(rateRemaining, "requests remaining this hour!")

        if rateRemaining == 0:
            log("No rate :(")
            plog(dict(resp.headers))
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
            log("Fetching page", page, f"for {user}/{repo}")

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
            log("Skipping non-tagged prerelease", release)
            verboseMatch = True
            continue
        if verboseMatch:
            log("Rescued it with", release, ":)")
        break
    else:
        # No matching releases
        return

    date = dateutil.parser.parse(tag.get("created_at"))

    return release, date


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


# Returns either a date object or none.
def parseUnstable(release):
    unstable = "unstable-"

    shouldParse = release.startswith(unstable)

    release_ = removePrefix("unstable-", release)

    try:
        date_obj = datetime.datetime.strptime(release_, "%Y-%m-%d")
    except ValueError:
        if shouldParse:
            log(
                f"Could not parse unstable date {release}! This should probably "
                "be fixed, either in nixpkgs or in this script."
            )
        return

    return date_obj


def getNextVersion(version, homepage):
    userRepo = getUserRepoPair(homepage)

    if userRepo is None:
        return

    nextVersionDate = latestRelease(*userRepo)

    if nextVersionDate is None:
        return

    nextVersion, nextDate = nextVersionDate

    currDate = parseUnstable(version)

    if currDate is not None and nextDate.date() <= currDate.date():
        log(
            f"Discarding unfit version {nextVersion} ({nextDate}), because it "
            f"is older than our current version {version}."
        )
        return

    if skipPrerelease(nextVersion):
        return

    nextVersion = stripRelease(userRepo[1], nextVersion)

    if libversion.version_compare(version, nextVersion) >= 0:
        return

    return nextVersion


def updateLines(meta):
    for name, values in meta.items():
        # skip python3*, packages have an updateScript
        if name.startswith("python3"):
            continue

        # skip typstPackages*, package set
        if name.startswith("typstPackages"):
            continue

        version = values["version"]

        for page in values["pages"]:
            userRepo = getUserRepoPair(page)
            if userRepo is not None:
                break
        else:
            continue

        user, repo = userRepo

        nextVersion = getNextVersion(version, page)
        if nextVersion is None:
            continue

        url = f"https://github.com/{user}/{repo}/releases"

        yield name, version, nextVersion, url


def main():
    try:
        meta = loadVersions()
        for line in updateLines(meta):
            print(*line, flush=True)
    except KeyboardInterrupt:
        log(" Shutting down...")
    finally:
        log("Cached stats:")
        plog(dict(CACHE_STATS))


if __name__ == "__main__":
    main()
