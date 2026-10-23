# Xodus Windows Game Pass Importer

A small Linux utility that exposes Xbox App / PC Game Pass installs already stored on a Windows NTFS partition to [Xodus Nexus](https://github.com/domsvoboda/xodus-nexus). It avoids copying the full game directory to the Linux filesystem.

The utility was developed and used on Bazzite with Xodus Nexus 0.3.8. It is intended to be portable to SteamOS, but SteamOS-specific package availability, mount permissions, and AppImage/FUSE behavior must be checked on the target installation.

## What it creates

For each detected game, the utility creates a staging directory under the Xodus install root:

```text
<xodus-root>/<game>/
├── upper/   # aligned .exe sidecars and a link to the Windows XVD
├── work/    # FUSE overlay working directory
└── mount/   # merged view to select when importing the game in Xodus
```

The Windows `Content` directory is used as the overlay's lower layer. The `upper` directory contains padded copies of `.exe` files and a `.xodus-streaming.msixvc` symlink to the Windows install's GUID-named XVD container. The game assets remain on the Windows partition. The utility only reads the Windows game directory; it writes staging files on Linux.

The merged `mount` directory presents the full game tree, so file managers may report the full game size there. To inspect Linux sidecar usage, check `upper`, for example:

```bash
du -sh "$HOME/Games/Xodus Nexus/Windows GamePass/Resonance A Plague Tale Legacy/upper"
```

Executable files are padded to 4096-byte boundaries in the sidecar. This works around the Xodus backend's alignment behavior on these Windows installs. It means some executable data is duplicated on Linux; the rest of the game is not copied.

## Detection rules

A direct child of the Windows game root is recognized when it contains:

1. A direct `Content` directory with `MicrosoftGame.config` or `appxmanifest.xml` (filename matching is case-insensitive).
2. Exactly one direct child file whose name is a GUID and whose bytes at offset 512 begin with `msft-xvd`.

The script was confirmed against these installs:

- Gears of War- Reloaded
- Resonance- A Plague Tale Legacy
- Valor Mortis

It normalizes the `- ` sequence in folder names when creating Linux staging paths, preserving the paths used by the existing Xodus imports for these titles.

## Requirements

- A Linux desktop session on Bazzite or SteamOS.
- Python 3.
- `fuse-overlayfs`.
- `findmnt` from util-linux.
- `fusermount3` (or `fusermount`) for safe stale-overlay cleanup.
- `/dev/fuse` available to the logged-in user session.
- The Windows partition mounted and readable. The script does not mount NTFS itself; systemd can automount it as described below.
- A working Xodus Nexus AppImage. The launcher defaults to `~/Applications/Xodus-Nexus-0.3.8-x86_64.AppImage`.

SteamOS is an immutable system. Keep this project and its installed scripts under the home directory; install dependencies using a SteamOS-supported method and verify they survive system updates. The utility does not install system packages or change mount configuration.

## Install the scripts

Copy the scripts into the user's local executable directory:

```bash
mkdir -p "$HOME/.local/bin"
install -m 755 scripts/xodus-windows-gamepass.py "$HOME/.local/bin/xodus-windows-gamepass"
install -m 755 scripts/xodus-nexus-gamepass-launcher "$HOME/.local/bin/xodus-nexus-gamepass-launcher"
```

By default, the scanner looks for games in:

```text
/run/media/<current-user>/Data/XboxGames
```

and creates Xodus staging folders under:

```text
~/Games/Xodus Nexus/Windows GamePass
```

Override either path with command-line options or environment variables:

```bash
XODUS_WINDOWS_ROOT="$HOME/mnt/windows/XboxGames" \
XODUS_IMPORT_ROOT="$HOME/Games/Xodus Nexus/Windows GamePass" \
"$HOME/.local/bin/xodus-windows-gamepass" scan
```

Equivalent command-line options are `--windows-root PATH` and `--xodus-root PATH`.

### Automatically mount the Windows drive on Bazzite

The game and Xodus shortcuts cannot use the imported views until the Windows partition is mounted. To have systemd mount it on first access after every boot, add an automount entry to `/etc/fstab`. This requires root access. The `nofail` option lets Bazzite boot if the drive is unavailable, and `ro` keeps Linux from changing the Windows partition. The game overlays store Linux-side changes in their staging directories.

Find the partition UUID and your numeric user and group IDs:

```bash
lsblk -f
id -u
id -g
```

Open `/etc/fstab` with `sudoedit /etc/fstab` and add one line, replacing the placeholders with the NTFS partition UUID, your Linux username, and the IDs returned above:

```text
UUID=<NTFS-UUID> /run/media/<username>/Data ntfs3 ro,nofail,x-systemd.automount,uid=<UID>,gid=<GID>,umask=022,x-systemd.device-timeout=10s 0 0
```

Then reload systemd and start the automount unit for the current session:

```bash
sudo systemctl daemon-reload
sudo systemctl start "$(systemd-escape --path --suffix=automount "/run/media/$USER/Data")"
```

Verify it by accessing the game root and checking the mounted filesystem:

```bash
ls "/run/media/$USER/Data/XboxGames"
findmnt -T "/run/media/$USER/Data/XboxGames"
```

The first access triggers the NTFS mount; subsequent launches reuse it. This Bazzite-specific setup is not automatically transferred to SteamOS, where the system partition and automount setup differ.

If the Xodus AppImage is elsewhere or has a different versioned name, set `XODUS_APPIMAGE` in the launcher environment or edit the `app=` line in `scripts/xodus-nexus-gamepass-launcher`.

## Usage

List recognized installs without changing staging files:

```bash
~/.local/bin/xodus-windows-gamepass scan
```

Create/reuse views, mount them, and remove stale sidecars for games no longer found in the Windows library:

```bash
~/.local/bin/xodus-windows-gamepass prepare
```

Import each new game's `mount` directory into Xodus once. For example:

```text
~/Games/Xodus Nexus/Windows GamePass/Resonance A Plague Tale Legacy/mount
```

Choose the `mount` directory itself; its parent does not contain the manifests at its root. Existing Xodus entries use stable staging paths and do not need to be re-imported after reboot.

## Launch from Steam / Gaming Mode

The launcher runs `prepare`, then starts Xodus:

```text
~/.local/bin/xodus-nexus-gamepass-launcher
```

Add this launcher as a non-Steam game and leave Proton compatibility disabled. If Steam's file chooser filters out shell scripts, add the Xodus AppImage first, then edit the shortcut's Target field to the launcher path above. The launcher continues to open Xodus even if preparation fails; it shows a desktop notification when available.

FUSE mounts do not survive reboot. The systemd user service described below recreates them at login; the launcher also prepares them when it starts Xodus. The Windows drive must be available at the configured root, either through the systemd automount below or by mounting it manually.

### Prepare game views for individual Steam shortcuts

Xodus-generated per-game shortcuts launch the AppImage directly. They do not run `xodus-nexus-gamepass-launcher`, so the FUSE views must be prepared at login. Use a systemd user service; this also keeps Xodus's own `shortcuts/launch-game` file intact. Do not replace that Xodus-managed file: Xodus checks its profile marker when adding shortcuts.

Install the supplied user service:

```bash
mkdir -p "$HOME/.config/systemd/user"
install -m 644 systemd/xodus-windows-gamepass-prepare.service \
  "$HOME/.config/systemd/user/xodus-windows-gamepass-prepare.service"
systemctl --user daemon-reload
systemctl --user enable --now xodus-windows-gamepass-prepare.service
```

The service starts at user login, accesses the Windows game root (triggering the systemd NTFS automount if configured), and prepares all imported views before the Steam shortcuts are launched. Check its result with `systemctl --user status xodus-windows-gamepass-prepare.service`.

## Deleted games and cleanup

Each managed staging directory contains `.xodus-windows-gamepass.json`. On `prepare`, the utility compares those markers with detected Windows installs. For a missing game it tries to unmount the FUSE view, then removes only the managed executable copies, XVD link, and empty staging directories. If unmounting fails or a managed directory was replaced with a symlink, it leaves the sidecar untouched.

The utility does not delete Windows game files and does not remove entries from the Xodus library. Remove a stale game entry from Xodus separately. The read-only `scan` command never performs cleanup.

To remove a staging setup manually, close Xodus and the game first, unmount that game's `mount` path with `fusermount3 -u PATH`, then remove its staging directory. Do not remove the Windows game's folder to clean up Linux sidecars.

## Limitations

- The scanner expects a direct `Content` directory and one matching XVD file per game folder.
- Some Xbox App titles may use a different package layout or container format and will be skipped.
- Preparing files does not guarantee that a title will launch. Xodus/Wine compatibility, licensing, DRM, and anti-cheat support vary by game.
- Mounting and preparation require the Windows drive to be available. The script does not configure system-level NTFS automount; follow the Bazzite instructions above if needed.
- No game content, AppImage binaries, or user-specific paths are included in this repository.
