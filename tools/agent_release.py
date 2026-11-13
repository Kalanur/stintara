"""Build-identity, packaging and verification helpers for the Windows agent.

Used by the PowerShell build script and the GitHub workflows. Only the standard
library is required.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "agent"
CHANNELS = ("dev", "ci", "release")
SHA_RE = re.compile(r"[0-9a-f]{40}")
TAG_RE = re.compile(r"agent-v(\d+\.\d+\.\d+)")
EXE_NAME = "StintaraAgent.exe"
EXAMPLE_NAME = "stintara-agent.example.json"
README_NAME = "README.txt"
ZIP_MEMBERS = (EXE_NAME, EXAMPLE_NAME, README_NAME)
CHECKSUM_NAME = "SHA256SUMS.txt"


def agent_version() -> str:
    text = (AGENT_DIR / "version.py").read_text(encoding="utf-8")
    match = re.search(r'^AGENT_VERSION = "(\d+\.\d+\.\d+)"$', text, re.M)
    if not match:
        raise ValueError("AGENT_VERSION not found in agent/version.py")
    return match.group(1)


def zip_name(version: str) -> str:
    return f"StintaraAgent-{version}-windows-x64.zip"


def validate_identity(channel: str, revision: str) -> None:
    if channel not in CHANNELS:
        raise ValueError(f"unknown build channel: {channel!r}")
    if channel == "dev":
        if revision != "unknown" and not SHA_RE.fullmatch(revision):
            raise ValueError("revision must be 'unknown' or a full git SHA")
    elif not SHA_RE.fullmatch(revision):
        raise ValueError("ci/release builds require a full 40-character lowercase git revision")


def write_build_info(channel: str, revision: str, target: Path | None = None) -> Path:
    validate_identity(channel, revision)
    target = target or AGENT_DIR / "_build_info.py"
    target.write_text(
        "# Generated at build time; not committed.\n"
        f"BUILD_CHANNEL = {channel!r}\nBUILD_REVISION = {revision!r}\n", encoding="utf-8")
    return target


def version_file_text(version: str) -> str:
    tup = ", ".join(map(str, [int(p) for p in version.split(".")] + [0]))
    strings = {"CompanyName": "Stintara", "FileDescription": "Stintara Agent",
               "FileVersion": f"{version}.0", "InternalName": "StintaraAgent",
               "OriginalFilename": EXE_NAME, "ProductName": "Stintara Agent",
               "ProductVersion": version}
    kids = ", ".join(f"StringStruct({k!r}, {v!r})" for k, v in strings.items())
    return ("VSVersionInfo(\n"
            f"  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0,\n"
            "                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
            "  kids=[\n"
            f"    StringFileInfo([StringTable('040904B0', [{kids}])]),\n"
            "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
            "  ])\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package(dist: Path, version: str) -> tuple[Path, Path]:
    """Create the release ZIP and SHA256SUMS.txt from the dist directory."""
    sources = {EXE_NAME: dist / EXE_NAME, EXAMPLE_NAME: AGENT_DIR / EXAMPLE_NAME,
               README_NAME: AGENT_DIR / "RELEASE-README.txt"}
    for source in sources.values():
        if not source.is_file():
            raise FileNotFoundError(source)
    archive = dist / zip_name(version)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ZIP_MEMBERS:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))  # deterministic
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, sources[name].read_bytes())
    sums = dist / CHECKSUM_NAME
    sums.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")
    return archive, sums


def verify_package(archive: Path, sums: Path, version: str) -> None:
    if archive.name != zip_name(version):
        raise ValueError(f"unexpected archive name {archive.name}")
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        if sorted(names) != sorted(ZIP_MEMBERS):
            raise ValueError(f"ZIP content mismatch: {sorted(names)}")
        if b"replace-with-provisioned-token" not in zf.read(EXAMPLE_NAME):
            raise ValueError("example config is not the public template")
    if sums.read_text(encoding="utf-8").splitlines() != [f"{sha256(archive)}  {archive.name}"]:
        raise ValueError("SHA256SUMS.txt does not match the ZIP")


def expected_version_output(version: str, channel: str, revision: str) -> list[str]:
    return [f"Stintara Agent {version}", f"Build: {channel}", f"Revision: {revision}",
            "Protocol: stintara-agent-v1"]


def verify_exe(exe: Path, version: str, channel: str, revision: str) -> str:
    out = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                         timeout=60, check=True, cwd=exe.parent).stdout
    if out.strip().splitlines() != expected_version_output(version, channel, revision):
        raise ValueError(f"unexpected --version output: {out!r}")
    if "unknown" in out and channel != "dev":
        raise ValueError("binary reports unknown revision")
    return out


def check_tag(tag: str, version: str) -> None:
    match = TAG_RE.fullmatch(tag)
    if not match or match.group(1) != version:
        raise ValueError(f"tag {tag!r} does not match agent version {version} (expected agent-v{version})")


def changelog_section(version: str, text: str | None = None) -> str:
    """Return the CHANGELOG.md section for a version (heading line excluded)."""
    text = (AGENT_DIR / "CHANGELOG.md").read_text(encoding="utf-8") if text is None else text
    pattern = r"^## " + re.escape(version) + r"\b[^\n]*\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, text, re.M | re.S)
    if not match or not match.group(1).strip():
        raise ValueError(f"no CHANGELOG.md section for {version}")
    return match.group(1).strip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build-info")
    p.add_argument("--channel", required=True)
    p.add_argument("--revision", required=True)
    p = sub.add_parser("version-file")
    p.add_argument("--out", required=True)
    p = sub.add_parser("package")
    p.add_argument("--dist", default="dist")
    p = sub.add_parser("verify")
    p.add_argument("--dist", default="dist")
    p.add_argument("--channel", required=True)
    p.add_argument("--revision", required=True)
    p = sub.add_parser("notes")
    p.add_argument("--out", required=True)
    p = sub.add_parser("check-tag")
    p.add_argument("tag")
    args = parser.parse_args(argv)
    try:
        version = agent_version()
        if args.cmd == "build-info":
            write_build_info(args.channel, args.revision)
        elif args.cmd == "version-file":
            Path(args.out).write_text(version_file_text(version), encoding="utf-8")
        elif args.cmd == "package":
            archive, sums = package(Path(args.dist), version)
            verify_package(archive, sums, version)
            print(f"{archive.name}\n{sums.read_text(encoding='utf-8').strip()}")
        elif args.cmd == "verify":
            validate_identity(args.channel, args.revision)
            dist = Path(args.dist)
            print(verify_exe(dist / EXE_NAME, version, args.channel, args.revision))
            print(f"EXE SHA256: {sha256(dist / EXE_NAME)}")
            verify_package(dist / zip_name(version), dist / CHECKSUM_NAME, version)
            print("Package verified.")
        elif args.cmd == "notes":
            Path(args.out).write_text(changelog_section(version), encoding="utf-8")
        elif args.cmd == "check-tag":
            check_tag(args.tag, version)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
