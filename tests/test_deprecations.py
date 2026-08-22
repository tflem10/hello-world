"""Deprecation guards — the tests that stop a green suite from lying to us.

This file looks like paranoia until you know the story. ``pyproject.toml`` used
to carry exactly one warning rule::

    filterwarnings = ["ignore::DeprecationWarning"]

Under it, ``swing/universe.py`` imported ``importlib.abc.Traversable``, an alias
Python deprecated in 3.12 and *removes* in 3.14. The interpreter warned on every
single import; the blanket ignore swallowed every one of them. Two thousand
green tests were sitting on a hard ``ImportError`` scheduled for the next
interpreter upgrade, and we found it by accident. Accident is not a strategy.

Two mechanisms replace the accident, because they fail in different ways:

1. **The scoped ``filterwarnings`` policy in ``pyproject.toml``.** Third-party
   deprecations stay ignored; anything attributed to a ``swing.*`` module is an
   error. The ``test_policy_*`` tests below exercise that policy
   *behaviourally*, so flattening it back to a blanket ignore turns this file
   red instead of turning the suite quiet.
2. **A cold-interpreter import scan** (``test_no_deprecated_apis_on_import``).
   A fresh subprocess imports every module under ``src/swing`` with all warnings
   enabled and reports any deprecation whose triggering file lives inside the
   package. This targets the import-time class specifically — the class that bit
   us — and, unlike mechanism 1, it does not depend on some other test happening
   to import the module first. A module nobody has written a test for is still
   checked.

The scan needs its own subprocess for a boring but fatal reason: an import-time
warning fires once per interpreter, and by the time this file runs, ``conftest``
and the other test modules have already imported most of the package. In-process
the scan would inspect an empty room and report success.

``test_scan_reports_*`` keeps mechanism 2 honest by planting deprecations in
throwaway packages and asserting the scanner catches them. A guard nobody has
ever watched fail is a guard nobody should trust.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import warnings
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

import swing

#: Resolved from the same import the rest of the suite uses, so the scan always
#: inspects the tree pytest is actually testing rather than some other checkout
#: that happens to be installed.
SWING_ROOT = Path(swing.__file__).resolve().parent

#: A cold interpreter importing pandas, matplotlib and yfinance is not instant.
SCAN_TIMEOUT_S = 300

#: Runs in a *fresh* interpreter, one ``catch_warnings`` block per module so a
#: module that fiddles with the global filter state cannot blind the scan for
#: everything imported after it. Findings go to a file rather than stdout so a
#: stray ``print`` at import time cannot corrupt the results.
_SCAN_DRIVER = """
import importlib
import json
import pathlib
import socket
import sys
import warnings

request = json.loads(pathlib.Path(sys.argv[1]).read_text())
sys.path[:0] = request["syspath"]
roots = [pathlib.Path(root) for root in request["roots"]]

# The suite guarantees it runs offline; this subprocess is outside the autouse
# fixture that enforces it, so it re-arms the block itself. Importing a module
# should never touch the network, and if one starts to, this says so loudly
# instead of quietly dialling out.
_real_connect = socket.socket.connect
_AF_UNIX = getattr(socket, "AF_UNIX", None)


def _guard_connect(self, address):
    if _AF_UNIX is not None and self.family == _AF_UNIX:
        return _real_connect(self, address)
    raise RuntimeError(f"import-time network access to {address!r}")


socket.socket.connect = _guard_connect

findings = []


def _inside_roots(filename):
    try:
        candidate = pathlib.Path(filename).resolve()
    except (OSError, ValueError):
        return False
    return any(candidate == root or root in candidate.parents for root in roots)


for name in request["modules"]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            importlib.import_module(name)
        except Exception as exc:
            findings.append(
                {
                    "kind": "import-error",
                    "module": name,
                    "label": type(exc).__name__,
                    "message": str(exc),
                    "file": "",
                    "lineno": 0,
                }
            )
            continue
    for entry in caught:
        if not issubclass(entry.category, (DeprecationWarning, PendingDeprecationWarning)):
            continue
        if not _inside_roots(entry.filename):
            continue
        findings.append(
            {
                "kind": "deprecation",
                "module": name,
                "label": entry.category.__name__,
                "message": str(entry.message),
                "file": entry.filename,
                "lineno": entry.lineno,
            }
        )

pathlib.Path(request["out"]).write_text(json.dumps(findings))
"""


def discover_modules(root: Path, package: str) -> list[str]:
    """Every importable module name under ``root``, as ``package.a.b`` strings.

    Walks the filesystem rather than ``pkgutil``, because ``pkgutil`` imports
    subpackages to find their ``__path__`` and this must not import anything in
    the parent process — the whole point is that the child gets to import each
    module first.
    """
    names: list[str] = []
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(root).parts)
        if parts[-1] == "__init__.py":
            parts.pop()
        else:
            parts[-1] = parts[-1].removesuffix(".py")
        if any(not part.isidentifier() for part in parts):
            continue  # not reachable by an import statement, so not our problem
        names.append(".".join([package, *parts]))
    return names


def scan_for_deprecations(
    modules: Sequence[str],
    roots: Iterable[Path],
    syspath: Iterable[Path],
    workdir: Path,
) -> list[dict[str, Any]]:
    """Import ``modules`` in a cold interpreter; report deprecations from ``roots``.

    Args:
        modules: dotted module names to import, in order.
        roots: directories that count as "our code". A warning is reported only
            when the file that *triggered* it lives under one of these.
        syspath: entries prepended to the child's ``sys.path``.
        workdir: scratch directory for the request/response files.

    Returns:
        One dict per finding, with ``kind`` (``deprecation`` or
        ``import-error``), ``module``, ``label``, ``message``, ``file`` and
        ``lineno``.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    out_path = workdir / "findings.json"
    request_path = workdir / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "modules": list(modules),
                "roots": [str(Path(root).resolve()) for root in roots],
                "syspath": [str(Path(entry).resolve()) for entry in syspath],
                "out": str(out_path),
            }
        )
    )

    # PYTHONWARNINGS in the developer's shell must not colour the result either
    # way; the driver sets its own filters.
    env = {key: value for key, value in os.environ.items() if key != "PYTHONWARNINGS"}
    completed = subprocess.run(
        [sys.executable, "-c", _SCAN_DRIVER, str(request_path)],
        capture_output=True,
        text=True,
        timeout=SCAN_TIMEOUT_S,
        env=env,
        check=False,
    )
    if completed.returncode != 0 or not out_path.exists():
        pytest.fail(
            "the deprecation scan subprocess did not complete.\n"
            f"exit code: {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return json.loads(out_path.read_text())


def _render(findings: Sequence[dict[str, Any]]) -> str:
    """Turn findings into a failure message someone can act on without digging."""
    lines = [f"{len(findings)} deprecated API(s) used by the swing package itself:", ""]
    for finding in findings:
        where = f"{finding['file']}:{finding['lineno']}" if finding["file"] else "(import failed)"
        lines.append(f"  {finding['label']} — {where}")
        lines.append(f"      reached by importing {finding['module']}")
        lines.append(f"      {finding['message']}")
        lines.append("")
    lines.append(
        "Fix the call site. Do NOT silence this by widening the ignore in "
        "pyproject.toml:\nthat is exactly how importlib.abc.Traversable lived here "
        "undetected until an\naccident found it."
    )
    return "\n".join(lines)


def _plant_package(root: Path, name: str, modules: dict[str, str]) -> Path:
    """Write a throwaway package so the scanner has something real to find."""
    package_dir = root / name
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("")
    for module_name, body in modules.items():
        # Strip the leading newline the triple-quoted literals carry, so line
        # numbers in the planted file are the obvious ones.
        source = textwrap.dedent(body).strip() + "\n"
        (package_dir / f"{module_name}.py").write_text(source)
    return package_dir


# --- the real guard --------------------------------------------------------


def test_no_deprecated_apis_on_import(tmp_path: Path) -> None:
    """No module under ``src/swing`` may trigger a deprecation when imported."""
    modules = discover_modules(SWING_ROOT, "swing")
    assert len(modules) >= 10, (
        f"module discovery found only {modules!r} under {SWING_ROOT}; the scan is "
        "not actually looking at the package and would pass vacuously"
    )
    assert "swing.universe" in modules  # the module the original incident lived in

    findings = scan_for_deprecations(
        modules, roots=[SWING_ROOT], syspath=[SWING_ROOT.parent], workdir=tmp_path / "scan"
    )
    if findings:
        pytest.fail(_render(findings))


# --- proof the guard fires -------------------------------------------------


def test_scan_reports_a_planted_deprecation(tmp_path: Path) -> None:
    """A deprecation raised at import time, from our own file, must be reported."""
    package = _plant_package(
        tmp_path / "src",
        "planted_ok",
        {
            "stale": """
                import warnings

                warnings.warn("planted: goes away in 4.0", DeprecationWarning, stacklevel=1)
            """
        },
    )

    findings = scan_for_deprecations(
        ["planted_ok.stale"],
        roots=[package],
        syspath=[package.parent],
        workdir=tmp_path / "scan",
    )

    assert len(findings) == 1, findings
    (finding,) = findings
    assert finding["kind"] == "deprecation"
    assert finding["label"] == "DeprecationWarning"
    assert finding["message"] == "planted: goes away in 4.0"
    assert finding["file"] == str(package / "stale.py")
    assert finding["lineno"] == 3
    assert "planted: goes away in 4.0" in _render(findings)


def test_scan_reports_a_planted_pending_deprecation(tmp_path: Path) -> None:
    """PendingDeprecationWarning counts too — it is the same defect, earlier."""
    package = _plant_package(
        tmp_path / "src",
        "planted_pending",
        {
            "stale": """
                import warnings

                warnings.warn("planted: pending", PendingDeprecationWarning, stacklevel=1)
            """
        },
    )

    findings = scan_for_deprecations(
        ["planted_pending.stale"],
        roots=[package],
        syspath=[package.parent],
        workdir=tmp_path / "scan",
    )

    assert [finding["label"] for finding in findings] == ["PendingDeprecationWarning"]


def test_scan_reports_a_module_that_cannot_be_imported(tmp_path: Path) -> None:
    """The 3.14 endgame: a removed alias is an ImportError, not a warning.

    Once the interpreter drops the deprecated name there is no warning left to
    catch, so the scan has to treat an unimportable module as a finding in its
    own right.
    """
    package = _plant_package(
        tmp_path / "src",
        "planted_broken",
        {
            "gone": """
                from importlib.abc import ThisNameNeverExisted  # noqa: F401
            """
        },
    )

    findings = scan_for_deprecations(
        ["planted_broken.gone"],
        roots=[package],
        syspath=[package.parent],
        workdir=tmp_path / "scan",
    )

    assert [finding["kind"] for finding in findings] == ["import-error"]
    assert findings[0]["label"] == "ImportError"


def test_scan_ignores_deprecations_from_outside_the_package(tmp_path: Path) -> None:
    """Third-party noise must not be reported, or nobody will keep the guard.

    The planted package imports a module that lives outside the scanned root and
    warns on its own line. The deprecation is real and is triggered during our
    import, but the file that triggered it is not ours, so it is somebody else's
    release schedule and not our defect.
    """
    outsider_root = tmp_path / "vendor"
    _plant_package(
        outsider_root,
        "planted_vendor",
        {
            "old": """
                import warnings

                warnings.warn("vendor: not our problem", DeprecationWarning, stacklevel=1)
            """
        },
    )
    package = _plant_package(
        tmp_path / "src",
        "planted_clean",
        {"fine": "from planted_vendor import old  # noqa: F401"},
    )

    findings = scan_for_deprecations(
        ["planted_clean.fine"],
        roots=[package],  # deliberately excludes outsider_root
        syspath=[package.parent, outsider_root],
        workdir=tmp_path / "scan",
    )

    assert findings == []


# --- proof the pyproject.toml policy is in force ---------------------------


def _warn_from_module(module_name: str, category: type[Warning]) -> None:
    """Emit ``category`` from a frame whose module ``__name__`` is ``module_name``.

    The fourth field of a ``filterwarnings`` entry is a regex matched against
    the ``__name__`` global of the frame the warning is attributed to. Faking
    that global is enough to exercise the real, configured policy against an
    arbitrary module name without importing anything or touching ``src/``.
    """
    namespace: dict[str, Any] = {
        "__name__": module_name,
        "warnings": warnings,
        "category": category,
    }
    exec("def emit():\n    warnings.warn('policy probe', category, stacklevel=1)\n", namespace)
    namespace["emit"]()


@pytest.mark.parametrize("category", [DeprecationWarning, PendingDeprecationWarning])
def test_policy_makes_swing_deprecations_fatal(category: type[Warning]) -> None:
    """A deprecation attributed to the swing package must fail the test that hit it."""
    with pytest.raises(category):
        _warn_from_module("swing.policy_probe", category)


@pytest.mark.parametrize("category", [DeprecationWarning, PendingDeprecationWarning])
def test_policy_leaves_third_party_deprecations_alone(category: type[Warning]) -> None:
    """We cannot fix pandas on our schedule, so its deprecations must stay quiet."""
    _warn_from_module("pandas.core.policy_probe", category)


def test_policy_does_not_match_packages_merely_starting_with_swing() -> None:
    """``swing($|\\.)`` and not ``swing.*``: the boundary is a real one.

    An unanchored ``swing`` prefix would also claim ``swingfoo``, whose
    deprecations are no more ours to fix than pandas'.
    """
    _warn_from_module("swingfoo.policy_probe", DeprecationWarning)
