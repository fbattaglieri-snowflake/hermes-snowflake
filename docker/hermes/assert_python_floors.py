#!/usr/bin/env python3
"""Fail the build if an installed Python distribution is below a required floor.

The container vulnerability gate blocks on packages this repository controls,
but a pinned ``pip install`` only governs the interpreter it runs against. A
second copy of the same distribution — seeded into a virtual environment, or
pulled in as a build dependency — stays behind at a vulnerable version and is
found later by the image scan, where the report gives no hint of which of the
several interpreters in the image is at fault.

This script closes that gap at build time: it walks the filesystem, reads every
distribution's recorded version, and reports every copy that is below its floor
together with the path that holds it.

Excluded subtrees are the ones pinned to an upstream revision, whose dependency
set is resolved by the upstream lock file rather than by us. Overriding those
would replace a combination upstream tested with one nobody has. They are
excluded here for the same reason, and with the same paths, as in the scan
policy of the CI workflow.
"""

from __future__ import annotations

import argparse
import os
import re

# Never worth descending into: kernel and device trees hold no distributions.
ALWAYS_SKIP = ("/proc", "/sys", "/dev")

DISTRIBUTION_DIRECTORY = re.compile(r"^(?P<name>.+?)-(?P<version>[^-]+)\.(dist-info|egg-info)$")


def parse_version(version: str) -> tuple[int, ...]:
    """Reduce a version string to its leading numeric components.

    Comparison only has to be good enough to order a release against a security
    floor, so a pre-release or local suffix is truncated rather than modelled.
    """
    components: list[int] = []
    for part in version.split("."):
        match = re.match(r"^(\d+)", part)
        if not match:
            break
        components.append(int(match.group(1)))
    return tuple(components)


def normalize(name: str) -> str:
    """Normalize a distribution name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def find_distributions(root: str, excluded: list[str]) -> list[tuple[str, str, str]]:
    """Return ``(name, version, path)`` for every distribution under ``root``."""
    skip = tuple(ALWAYS_SKIP) + tuple(excluded)
    found: list[tuple[str, str, str]] = []
    for directory, subdirectories, _ in os.walk(root):
        if directory.startswith(skip):
            subdirectories[:] = []
            continue
        for subdirectory in subdirectories:
            match = DISTRIBUTION_DIRECTORY.match(subdirectory)
            if match:
                found.append(
                    (
                        normalize(match.group("name")),
                        match.group("version"),
                        os.path.join(directory, subdirectory),
                    )
                )
    return found


def violations(
    distributions: list[tuple[str, str, str]], floors: dict[str, str]
) -> list[tuple[str, str, str, str]]:
    """Return ``(name, version, floor, path)`` for each distribution below its floor."""
    below: list[tuple[str, str, str, str]] = []
    for name, version, path in distributions:
        floor = floors.get(name)
        if floor and parse_version(version) < parse_version(floor):
            below.append((name, version, floor, path))
    return sorted(below)


def parse_floors(specifications: list[str]) -> dict[str, str]:
    """Parse ``name=version`` arguments into a normalized floor table."""
    floors: dict[str, str] = {}
    for specification in specifications:
        name, separator, version = specification.partition("=")
        if not separator or not name or not version:
            raise SystemExit(f"invalid floor {specification!r}; expected name=version")
        floors[normalize(name)] = version
    return floors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("floors", nargs="+", metavar="NAME=VERSION")
    parser.add_argument("--root", default="/", help="filesystem root to walk")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATH",
        help="subtree to skip; repeatable",
    )
    arguments = parser.parse_args(argv)

    floors = parse_floors(arguments.floors)
    distributions = find_distributions(arguments.root, arguments.exclude)
    below = violations(distributions, floors)

    for name in sorted(floors):
        present = sorted({version for found, version, _ in distributions if found == name})
        print(f"{name}: floor {floors[name]}, found {present or ['none']}")

    if below:
        print("\nERROR: distributions below their security floor:")
        for name, version, floor, path in below:
            print(f"  {name} {version} < {floor} at {path}")
        return 1

    print("\nAll checked distributions meet their floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
