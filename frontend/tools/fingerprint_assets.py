"""Publish one content-addressed asset directory in a fresh build tree."""

from hashlib import sha256
from pathlib import Path
import re
from shutil import copytree
import sys


ASSET_URL = re.compile(
    rb"""(\b(?:href|src)=["'])(/?assets/)([^"'?#]+)"""
)


def fingerprint_assets(public: Path) -> str:
    assets = public / "assets"
    fingerprint = sha256()
    for path in sorted(assets.rglob("*")):
        if path.is_file():
            fingerprint.update(path.relative_to(assets).as_posix().encode("utf-8"))
            fingerprint.update(b"\0")
            fingerprint.update(sha256(path.read_bytes()).digest())

    version = f"build-{fingerprint.hexdigest()[:20]}"
    # Copy into a sibling first, so the versioned directory cannot copy itself.
    staged = public / version
    copytree(assets, staged)
    staged.rename(assets / version)

    def version_url(match: re.Match[bytes]) -> bytes:
        if not (assets / match[3].decode("utf-8")).is_file():
            raise ValueError(f"Missing static asset: {match[3].decode('utf-8')}")
        return match[1] + match[2] + version.encode("ascii") + b"/" + match[3]

    for page in public.glob("*.html"):
        page.write_bytes(ASSET_URL.sub(version_url, page.read_bytes()))
    return version


if __name__ == "__main__":
    print(fingerprint_assets(Path(sys.argv[1])))
