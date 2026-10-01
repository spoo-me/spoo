import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_versions() -> dict[str, set[str]]:
    text = (ROOT / "uv.lock").read_text()
    versions: dict[str, set[str]] = {}
    for block in text.split("[[package]]")[1:]:
        name = re.search(r'^name = "([^"]+)"', block, re.M)
        version = re.search(r'^version = "([^"]+)"', block, re.M)
        if name and version:
            versions.setdefault(_norm(name.group(1)), set()).add(version.group(1))
    return versions


def _pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s;#]+)", line)
        if m:
            pins[_norm(m.group(1))] = m.group(2)
    return pins


def test_requirements_txt_pins_match_uv_lock():
    # Railway installs from requirements.txt while the image and CI use uv.lock.
    lock = _lock_versions()
    drift = {
        name: (pinned, sorted(lock.get(name, ())))
        for name, pinned in _pins().items()
        if pinned not in lock.get(name, ())
    }
    assert not drift, f"requirements.txt disagrees with uv.lock: {drift}"
