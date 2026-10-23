#!/usr/bin/env python3
"""Expose existing Xbox App installs to Xodus without copying game data."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path


DEFAULT_WINDOWS_ROOT = Path(os.environ.get(
    "XODUS_WINDOWS_ROOT",
    str(Path("/run/media") / getpass.getuser() / "Data/XboxGames"),
))
DEFAULT_XODUS_ROOT = Path(os.environ.get(
    "XODUS_IMPORT_ROOT",
    str(Path.home() / "Games/Xodus Nexus/Windows GamePass"),
))
STATE = ".xodus-windows-gamepass.json"
OWNER = "xodus-windows-gamepass-v1"
GUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
ALIGNMENT = 4096


class SetupError(RuntimeError):
    pass


def mounted(path: Path) -> bool:
    result = subprocess.run(
        ["findmnt", "--noheadings", "--raw", "--mountpoint", str(path), "--output", "TARGET"],
        text=True, capture_output=True, check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def find_games(root: Path) -> list[tuple[Path, Path, Path]]:
    if not root.is_dir():
        raise SetupError(f"Windows game directory not found: {root}. Mount the Windows drive first.")
    games = []
    for game in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        content = next((p for p in game.iterdir() if p.is_dir() and p.name.casefold() == "content"), None)
        if content is None or not any(p.name.casefold() in {"microsoftgame.config", "appxmanifest.xml"} for p in content.iterdir()):
            continue
        xvds = []
        for candidate in game.iterdir():
            if not candidate.is_file() or not GUID.match(candidate.name):
                continue
            try:
                with candidate.open("rb") as stream:
                    stream.seek(512)
                    if stream.read(8) == b"msft-xvd":
                        xvds.append(candidate)
            except OSError:
                pass
        if len(xvds) == 1:
            games.append((game, content, xvds[0]))
        else:
            print(f"Skipping {game.name}: found {len(xvds)} matching XVD containers.")
    return games


def stage_name(game: Path) -> str:
    # Keep the paths used by the three imports already known to Xodus.
    return game.name.replace("- ", " ")


def exe_files(content: Path) -> list[Path]:
    found = []
    for base, dirs, files in os.walk(content, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
        for name in files:
            path = Path(base) / name
            if name.casefold().endswith(".exe") and path.is_file() and not path.is_symlink():
                found.append(path)
    return sorted(found, key=lambda p: p.relative_to(content).as_posix().casefold())


def matches_padded(source: Path, target: Path) -> bool:
    try:
        size = source.stat().st_size
        target_size = target.stat().st_size
        if target_size < size or target_size % ALIGNMENT:
            return False
        with source.open("rb") as src, target.open("rb") as dst:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                if dst.read(len(block)) != block:
                    return False
            while block := dst.read(1024 * 1024):
                if any(block):
                    return False
        return True
    except OSError:
        return False


def copy_aligned(source: Path, target: Path) -> None:
    size = source.stat().st_size
    target_size = ((size + ALIGNMENT - 1) // ALIGNMENT) * ALIGNMENT
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".xodus-tmp")
    with source.open("rb") as src, temporary.open("wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        if target_size > size:
            dst.write(b"\0" * (target_size - size))
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    os.replace(temporary, target)


def write_state(stage: Path, game: Path, xvd: Path, executables: list[str]) -> None:
    (stage / STATE).write_text(json.dumps({
        "managed_by": OWNER,
        "source_game": str(game.resolve()),
        "xvd": str(xvd.resolve()),
        "managed_executables": executables,
    }, indent=2) + "\n", encoding="utf-8")


def clean_stale(root: Path, xodus_root: Path, present_games: set[Path], present_xvds: set[Path]) -> None:
    if not xodus_root.is_dir():
        return
    root_real = root.resolve(strict=True)
    for stage in xodus_root.iterdir():
        marker = stage / STATE
        if stage.is_symlink() or not stage.is_dir() or marker.is_symlink() or not marker.is_file():
            continue
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if data.get("managed_by") != OWNER:
                continue
            game = Path(data["source_game"]).resolve(strict=False)
            xvd = Path(data["xvd"]).resolve(strict=False)
            managed = data["managed_executables"]
            game.relative_to(root_real)
            if not isinstance(managed, list) or not all(isinstance(item, str) for item in managed):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if game in present_games or xvd in present_xvds:
            continue

        upper, work, mount = (stage / name for name in ("upper", "work", "mount"))
        if any(path.is_symlink() for path in (upper, work, mount)):
            print(f"Keeping modified staging directory untouched: {stage}")
            continue
        if mounted(mount):
            unmount = shutil.which("fusermount3") or shutil.which("fusermount")
            if not unmount or subprocess.run([unmount, "-u", str(mount)], check=False).returncode or mounted(mount):
                print(f"Could not safely unmount {mount}; keeping its sidecar files.")
                continue

        for relative in managed:
            target = (upper / relative).resolve(strict=False)
            try:
                target.relative_to(upper.resolve(strict=False))
                if target.is_file() and not target.is_symlink():
                    target.unlink()
            except (ValueError, OSError):
                pass
        link = upper / ".xodus-streaming.msixvc"
        try:
            if link.is_symlink() and os.readlink(link) == str(data["xvd"]):
                link.unlink()
        except OSError:
            pass
        if work.is_dir():
            shutil.rmtree(work, ignore_errors=True)
        for directory in (upper, mount):
            if directory.is_dir() and not mounted(directory):
                for base, dirs, _files in os.walk(directory, topdown=False):
                    for name in dirs:
                        try:
                            (Path(base) / name).rmdir()
                        except OSError:
                            pass
                try:
                    directory.rmdir()
                except OSError:
                    pass
        marker.unlink(missing_ok=True)
        try:
            stage.rmdir()
        except OSError:
            pass
        print(f"Removed stale sidecars for missing Windows game: {stage.name}")


def prepare(game: Path, content: Path, xvd: Path, xodus_root: Path) -> None:
    stage = xodus_root / stage_name(game)
    upper, work, mount = (stage / name for name in ("upper", "work", "mount"))
    for directory in (upper, work, mount):
        if directory.is_symlink():
            raise SetupError(f"Refusing to use a symlink as a managed path: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
    if mounted(mount):
        print(f"Already mounted: {game.name} -> {mount}")
        return
    if not Path("/dev/fuse").exists():
        raise SetupError("/dev/fuse is unavailable. Run the utility in a normal SteamOS/Bazzite desktop session.")

    link = upper / ".xodus-streaming.msixvc"
    if link.exists() and not link.is_symlink():
        raise SetupError(f"Refusing to overwrite a regular file: {link}")
    if link.is_symlink():
        try:
            if link.resolve(strict=True) != xvd.resolve(strict=True):
                link.unlink()
        except OSError:
            link.unlink()
    if not link.is_symlink():
        link.symlink_to(xvd.resolve())

    previous = set()
    state_path = stage / STATE
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("managed_by") == OWNER:
                previous = set(state.get("managed_executables", []))
        except (OSError, ValueError, AttributeError):
            pass

    current = []
    for source in exe_files(content):
        relative = source.relative_to(content).as_posix()
        target = upper / relative
        if target.exists() and relative not in previous and not matches_padded(source, target):
            raise SetupError(f"Refusing to overwrite an unmanaged file: {target}")
        copy_aligned(source, target)
        current.append(relative)
    for stale in previous.difference(current):
        old = upper / stale
        if old.is_file() and not old.is_symlink():
            old.unlink()
    write_state(stage, game, xvd, current)

    options = f"lowerdir={content.resolve()},upperdir={upper.resolve()},workdir={work.resolve()}"
    result = subprocess.run(["fuse-overlayfs", "-o", options, str(mount)], check=False)
    if result.returncode:
        raise SetupError(f"fuse-overlayfs exited with status {result.returncode} for {game.name}")
    print(f"Prepared: {game.name} -> {mount}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Windows Xbox App games for Xodus without copying full games.")
    parser.add_argument("command", choices=("scan", "prepare"), nargs="?", default="prepare")
    parser.add_argument("--windows-root", type=Path, default=DEFAULT_WINDOWS_ROOT)
    parser.add_argument("--xodus-root", type=Path, default=DEFAULT_XODUS_ROOT)
    args = parser.parse_args()
    try:
        games = find_games(args.windows_root)
        if args.command == "prepare":
            clean_stale(args.windows_root, args.xodus_root, {g.resolve(strict=False) for g, _c, _x in games},
                        {x.resolve(strict=False) for _g, _c, x in games})
        if not games:
            print(f"No Windows Game Pass installs found under {args.windows_root}")
            return 0
        print(f"Found {len(games)} Windows game(s):")
        for game, content, xvd in games:
            print(f"  {game.name} — XVD {xvd.name}")
        if args.command == "scan":
            return 0
        if not shutil.which("fuse-overlayfs") or not shutil.which("findmnt"):
            raise SetupError("Required commands are missing: fuse-overlayfs and findmnt.")
        for game, content, xvd in games:
            prepare(game, content, xvd, args.xodus_root)
        print("Ready. Import each game's mount directory into Xodus.")
        return 0
    except (OSError, SetupError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
