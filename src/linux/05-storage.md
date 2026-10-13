# Part 5 — Disks, Filesystems and Storage

"Disk full" is the most common production incident there is, and storage changes (growing a volume, adding a disk, fixing a bad fstab) are among the riskiest routine tasks. This Part builds the mental model from block device up to mounted filesystem.

## 5.1 The Storage Stack

```text
 Application: write("/var/lib/pg/data/file")
        |
 VFS (virtual filesystem layer)
        |
 Filesystem: ext4 / xfs / btrfs        <- mkfs, mount, fsck
        |
 [optional] LVM logical volume          <- lvcreate, lvextend
 [optional] dm-crypt (LUKS), mdraid
        |
 Partition: /dev/nvme0n1p2             <- parted, fdisk, gdisk
        |
 Block device: /dev/nvme0n1, /dev/sda, /dev/xvdf  (disk, EBS volume, virtio disk)
```

Every layer is optional except the device and the filesystem. A cloud data volume is often formatted directly (no partition, no LVM), which keeps resizing simple.

## 5.2 Discovering Block Devices

```bash
lsblk                       # tree of devices, partitions, mountpoints
lsblk -f                    # add filesystem type, label, UUID
sudo blkid                  # UUIDs and types
sudo fdisk -l /dev/sda      # partition table
cat /proc/partitions
ls -l /dev/disk/by-uuid/ /dev/disk/by-id/
```

```text
NAME        MAJ:MIN RM  SIZE RO TYPE MOUNTPOINTS
nvme0n1     259:0    0   30G  0 disk
|-nvme0n1p1 259:1    0   29G  0 part /
|-nvme0n1p14 259:2   0    4M  0 part
`-nvme0n1p15 259:3   0  106M  0 part /boot/efi
nvme1n1     259:4    0  100G  0 disk
```

Device naming:

| Name | Meaning |
|---|---|
| `/dev/sda`, `/dev/sdb` | SCSI/SATA/SAS, and USB disks |
| `/dev/vda` | virtio disk (KVM) |
| `/dev/xvda` | Xen (older EC2) |
| `/dev/nvme0n1` | NVMe controller 0, namespace 1; partitions are `p1`, `p2`; all modern EC2 EBS volumes appear as NVMe |

> **Warning:** Device names are *not stable*. `/dev/sdb` can become `/dev/sdc` after a reboot or when disks are attached in a different order. Always reference filesystems by **UUID** or label in fstab, and EBS volumes via `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol...`.

## 5.3 Partitioning

Two partition table formats:

- **MBR (msdos)**: legacy, 4 primary partitions, max 2 TiB.
- **GPT**: modern, 128 partitions, huge disks, backup header at the end of the disk. Use GPT.

```bash
# Scriptable, non-interactive: one partition spanning the disk
sudo parted -s /dev/sdb mklabel gpt
sudo parted -s /dev/sdb mkpart primary 1MiB 100%
sudo parted -s /dev/sdb set 1 lvm on        # optional: flag for LVM
lsblk /dev/sdb
```

Interactive alternatives: `fdisk` (handles GPT too), `gdisk`, `cfdisk`. After partitioning a disk that's in use, the kernel may need `sudo partprobe` to re-read the table.

## 5.4 Filesystems

| Filesystem | Strengths | Notes |
|---|---|---|
| **ext4** | Mature, default on Debian/Ubuntu; can grow and shrink (offline) | Fixed inode count at mkfs time |
| **XFS** | Default on RHEL family; great for large files and parallel I/O | Can grow, **cannot shrink**; dynamic inodes |
| **btrfs** | Snapshots, checksums, compression, subvolumes | Default on Fedora/openSUSE desktops |
| **vfat** | EFI System Partition | No Unix permissions |
| **tmpfs** | RAM-backed | `/run`, often `/tmp` |
| **overlayfs** | Union of layers | What Docker/containerd use for image layers |
| **NFS / EFS** | Network filesystem | Mount options matter (`_netdev`, `nofail`) |

```bash
sudo mkfs.ext4 -L data /dev/sdb1
sudo mkfs.xfs -L data /dev/sdb1
sudo tune2fs -l /dev/sdb1 | head -20      # ext4 details
sudo xfs_info /data                        # xfs details (mounted)
```

### 5.4.1 Inodes

Each file consumes one **inode** (metadata: owner, mode, timestamps, pointers to data blocks). Filenames live in directories, mapping names to inode numbers. On ext4 the number of inodes is fixed when you create the filesystem, so a disk can be "full" with plenty of free bytes if millions of tiny files used up all inodes.

```bash
df -i
```

```text
Filesystem      Inodes   IUsed  IFree IUse% Mounted on
/dev/nvme0n1p1 1966080 1966080      0  100% /
```

Classic culprits: PHP session files, mail queues, cache directories, a runaway job creating temp files. Find directories with the most files:

```bash
sudo find / -xdev -type f | cut -d/ -f2-3 | sort | uniq -c | sort -rn | head
```

For filesystems that will hold millions of small files, create ext4 with more inodes (`mkfs.ext4 -i 4096`) or use XFS.

## 5.5 Mounting and fstab

Mounting attaches a filesystem to a directory (the **mount point**). Anything previously in that directory is hidden (not deleted) while the mount is active, a source of confusing "missing files" moments.

```bash
sudo mkdir -p /data
sudo mount /dev/sdb1 /data
sudo mount -o remount,ro /data      # change options live
findmnt /data                       # source, fstype, options
mount | column -t | grep data
sudo umount /data                   # 'target is busy'? see below
sudo lsof +f -- /data               # who's using it
sudo fuser -vm /data
```

### 5.5.1 /etc/fstab

`/etc/fstab` declares what to mount at boot. systemd converts each line into a `.mount` unit.

```text
# <device>                                  <mountpoint> <type> <options>                 <dump> <pass>
UUID=3f1c2a9e-8d1b-4c1a-9f0e-2b7c5d1e9a01   /            ext4   defaults,discard          0      1
UUID=7B3A-11F2                              /boot/efi    vfat   umask=0077                0      1
UUID=a1b2c3d4-0000-4000-8000-123456789abc   /data        xfs    defaults,nofail,noatime   0      2
fs-0123abcd.efs.us-east-1.amazonaws.com:/   /mnt/efs     nfs4   nfsvers=4.1,_netdev,nofail 0     0
/swapfile                                   none         swap   sw                        0      0
```

Important options:

- **`nofail`**: don't fail the boot if this device is missing. Essential for secondary and cloud volumes; without it, a detached EBS volume drops the instance into emergency mode, where you can't SSH in.
- **`_netdev`**: network filesystem; wait for the network.
- **`noatime`**: don't update access time on every read (less write I/O).
- **`ro`, `noexec`, `nosuid`, `nodev`**: hardening for `/tmp`, data and removable mounts.
- **pass**: fsck order; `1` for root, `2` for others, `0` to skip.

**Always test fstab before rebooting:**

```bash
sudo findmnt --verify          # syntax and sanity check
sudo mount -a                  # mount everything not yet mounted; errors appear now, not at boot
sudo systemctl daemon-reload   # regenerate mount units
```

> **Warning:** A typo in fstab is one of the most common reasons a cloud VM stops booting. If it happens: use the serial console, boot to rescue/emergency mode, or detach the root volume and attach it to another instance to fix the file.

### 5.5.2 Swap

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
swapon --show; free -h
```

Kubernetes nodes historically required swap off (newer versions support it with configuration). Database hosts often keep a small swap and set `vm.swappiness` low (Part 8).

## 5.6 LVM: Logical Volume Manager

LVM inserts a flexible layer between partitions and filesystems, so you can pool disks, resize volumes online, and take snapshots.

```text
 Physical Volumes (PV):   /dev/sdb1      /dev/sdc1
                              \             /
 Volume Group (VG):           vg_data (pool of extents)
                             /         \
 Logical Volumes (LV):  lv_app (50G)   lv_logs (20G)    <- mkfs these
                         /data/app      /var/log/app
```

```bash
sudo pvcreate /dev/sdb1 /dev/sdc1
sudo vgcreate vg_data /dev/sdb1 /dev/sdc1
sudo lvcreate -n lv_app -L 50G vg_data
sudo lvcreate -n lv_logs -l 100%FREE vg_data
sudo mkfs.xfs /dev/vg_data/lv_app
sudo pvs; sudo vgs; sudo lvs          # summaries
sudo lvdisplay /dev/vg_data/lv_app
```

LVs appear as `/dev/vg_data/lv_app` and `/dev/mapper/vg_data-lv_app`.

### 5.6.1 Growing storage (the routine task)

```bash
# Add a new disk to the VG
sudo pvcreate /dev/sdd
sudo vgextend vg_data /dev/sdd
# Grow LV and filesystem in one step (-r runs the right fs resize tool)
sudo lvextend -r -L +20G /dev/vg_data/lv_app
# or use all free space
sudo lvextend -r -l +100%FREE /dev/vg_data/lv_app
```

Without `-r`, you'd follow with `resize2fs` (ext4) or `xfs_growfs /mountpoint` (XFS). Both grow online.

### 5.6.2 Growing a cloud root disk (no LVM)

After enlarging an EBS volume in AWS (or a disk in any cloud):

```bash
lsblk                                  # disk shows 50G, partition still 30G
sudo growpart /dev/nvme0n1 1           # grow partition 1 (from cloud-guest-utils / cloud-utils-growpart)
sudo resize2fs /dev/nvme0n1p1          # ext4
sudo xfs_growfs -d /                   # xfs (takes the mount point)
df -h /
```

Many cloud images run `growpart` automatically at boot through cloud-init.

### 5.6.3 Snapshots

```bash
sudo lvcreate -s -n lv_app_snap -L 5G /dev/vg_data/lv_app   # copy-on-write snapshot
sudo mount -o ro,nouuid /dev/vg_data/lv_app_snap /mnt/snap  # nouuid needed for XFS
# ... back it up, then:
sudo umount /mnt/snap && sudo lvremove -y /dev/vg_data/lv_app_snap
```

The snapshot needs enough space to hold changed blocks; if it fills, it becomes invalid.

## 5.7 RAID Basics

RAID combines disks for redundancy and/or performance. Linux software RAID uses `mdadm`.

| Level | Min disks | Usable capacity | Survives | Use |
|---|---|---|---|---|
| RAID 0 | 2 | 100% | No failures | Scratch space, speed only |
| RAID 1 | 2 | 50% | n-1 disks | Boot/OS mirrors |
| RAID 5 | 3 | (n-1)/n | 1 disk | Read-heavy; slow rebuilds on big disks |
| RAID 6 | 4 | (n-2)/n | 2 disks | Large arrays |
| RAID 10 | 4 | 50% | 1 per mirror pair | Databases: performance + redundancy |

```bash
sudo mdadm --create /dev/md0 --level=1 --raid-devices=2 /dev/sdb /dev/sdc
cat /proc/mdstat
sudo mdadm --detail /dev/md0
sudo mdadm --detail --scan | sudo tee -a /etc/mdadm/mdadm.conf   # persist (path varies: /etc/mdadm.conf on RHEL)
sudo update-initramfs -u   # or dracut -f
```

> **Note:** RAID is not a backup. It protects against disk failure, not against `rm -rf`, corruption, or ransomware. In the cloud, EBS already replicates within an AZ; people use RAID 0 across volumes only for throughput.

## 5.8 Disk Usage: df and du

```bash
df -h                       # space per mounted filesystem
df -hT /var                 # with fs type, for the fs holding /var
df -i                       # inodes
du -sh /var/log             # total of a directory
du -h --max-depth=1 /var | sort -h      # biggest subdirectories
sudo du -xh / --max-depth=2 2>/dev/null | sort -h | tail -20   # -x: stay on one fs
ncdu -x /                   # interactive explorer (install it)
```

### 5.8.1 When df and du disagree

`df` reports what the filesystem has allocated; `du` adds up files it can *see* by walking directories. If `df` says 95% full and `du` finds far less, the usual cause is a **deleted file still held open by a process**. The name is gone, but the inode and blocks remain until the last file descriptor closes.

```bash
sudo lsof +L1                    # open files with link count 0 (deleted)
sudo lsof -nP | grep '(deleted)'
```

```text
COMMAND  PID  USER   FD   TYPE DEVICE   SIZE/OFF NLINK  NODE NAME
java    2210  app    12w   REG  259,1 21474836480     0 39012 /var/log/app/app.log (deleted)
```

Fix: restart the process, or truncate the file through its descriptor without restarting:

```bash
sudo truncate -s 0 /proc/2210/fd/12
```

The other common cause is files **hidden under a mount point** (written to `/data` before the volume was mounted). Bind-mount root elsewhere to look: `sudo mount --bind / /mnt/rootview && sudo du -sh /mnt/rootview/data`.

> **Tip:** To empty a log that a process is writing, use `truncate -s 0 file` or `: > file`, not `rm`. Removing it leaves the space allocated until the process restarts.

## 5.9 Checking and Repairing Filesystems

```bash
sudo umount /dev/sdb1
sudo fsck -f /dev/sdb1          # ext4: e2fsck
sudo xfs_repair /dev/sdb1       # xfs (must be unmounted)
sudo dmesg -T | grep -iE 'I/O error|EXT4-fs error|XFS'   # look for disk problems
sudo smartctl -a /dev/sda       # SMART health (physical disks; smartmontools)
```

Never run fsck on a mounted read-write filesystem. If the kernel detects corruption, ext4 may remount itself read-only (`errors=remount-ro`), which shows up as "Read-only file system" errors in apps.

## 5.10 Lab: Build, Grow and Break Storage Safely

Goal: practise the full LVM workflow and fstab handling using loop devices, so no real disk is at risk.

1. Create three 1 GiB "disks" backed by files:

   ```bash
   sudo mkdir -p /lab5
   for i in 1 2 3; do sudo truncate -s 1G /lab5/disk$i.img; done
   for i in 1 2 3; do sudo losetup -fP --show /lab5/disk$i.img; done
   lsblk | grep loop
   ```

   Note the device names printed (e.g. `/dev/loop10`, `/dev/loop11`, `/dev/loop12`). Below we call them `$D1 $D2 $D3`:

   ```bash
   D1=/dev/loop10; D2=/dev/loop11; D3=/dev/loop12   # adjust!
   ```

2. Build LVM on two of them:

   ```bash
   sudo pvcreate $D1 $D2
   sudo vgcreate vg_lab $D1 $D2
   sudo lvcreate -n lv_data -L 1G vg_lab
   sudo mkfs.ext4 -L labdata /dev/vg_lab/lv_data
   sudo mkdir -p /mnt/labdata
   sudo mount /dev/vg_lab/lv_data /mnt/labdata
   df -h /mnt/labdata
   ```

3. Persist it in fstab with UUID and `nofail`, then verify without rebooting:

   ```bash
   UUID=$(sudo blkid -s UUID -o value /dev/vg_lab/lv_data)
   echo "UUID=$UUID /mnt/labdata ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
   sudo umount /mnt/labdata
   sudo findmnt --verify && sudo mount -a && findmnt /mnt/labdata
   ```

4. Grow online: add the third disk and extend the LV and filesystem together:

   ```bash
   sudo pvcreate $D3 && sudo vgextend vg_lab $D3
   sudo lvextend -r -l +100%FREE /dev/vg_lab/lv_data
   df -h /mnt/labdata
   ```

5. Reproduce "df vs du" disagreement:

   ```bash
   sudo dd if=/dev/zero of=/mnt/labdata/big.log bs=1M count=500 status=none
   sudo bash -c 'exec 3>>/mnt/labdata/big.log; sleep 600' &
   sleep 1; sudo rm /mnt/labdata/big.log
   df -h /mnt/labdata; sudo du -sh /mnt/labdata
   sudo lsof +L1 | grep big.log
   ```

   Then kill the `sleep` process and watch `df` drop.

6. Exhaust inodes on a tiny filesystem:

   ```bash
   sudo truncate -s 20M /lab5/tiny.img
   sudo mkfs.ext4 -q -N 128 /lab5/tiny.img
   sudo mkdir -p /mnt/tiny && sudo mount -o loop /lab5/tiny.img /mnt/tiny
   sudo bash -c 'for i in $(seq 1 5000); do touch /mnt/tiny/f$i || break; done'
   df -h /mnt/tiny; df -i /mnt/tiny     # space free, inodes 100%
   ```

   (ext4 rounds the inode count up to fill whole inode-table blocks, so you'll get a few hundred inodes rather than exactly 128; the loop stops at the first "No space left on device".)

7. Take an LVM snapshot. The VG is full after step 4, so add a fourth disk first:

   ```bash
   sudo truncate -s 1G /lab5/disk4.img
   D4=$(sudo losetup -fP --show /lab5/disk4.img)
   sudo pvcreate $D4 && sudo vgextend vg_lab $D4
   echo "before" | sudo tee /mnt/labdata/state.txt
   sudo lvcreate -s -n lv_snap -L 500M vg_lab/lv_data
   echo "after" | sudo tee /mnt/labdata/state.txt
   sudo mkdir -p /mnt/snap && sudo mount -o ro /dev/vg_lab/lv_snap /mnt/snap
   cat /mnt/snap/state.txt        # still "before": the snapshot froze that moment
   sudo lvs vg_lab                # Data% shows how full the COW area is
   sudo umount /mnt/snap && sudo lvremove -y vg_lab/lv_snap
   ```

8. Clean up carefully, in reverse order:

   ```bash
   sudo umount /mnt/tiny /mnt/labdata
   sudo sed -i '\|/mnt/labdata|d' /etc/fstab
   sudo lvremove -y vg_lab/lv_data && sudo vgremove vg_lab && sudo pvremove $D1 $D2 $D3 $D4
   sudo losetup -d $D1 $D2 $D3 $D4
   sudo rm -rf /lab5
   ```

## Key takeaways

- The stack is device → (partition) → (LVM/RAID/crypt) → filesystem → mount point. Know which layer you're changing.
- Device names change; reference filesystems by UUID, and use `nofail` for non-root volumes, especially in the cloud.
- Always run `findmnt --verify` and `mount -a` after editing fstab, before rebooting.
- LVM makes online growth routine: `vgextend` then `lvextend -r`. XFS grows but never shrinks.
- "Disk full" can mean bytes (`df -h`) or inodes (`df -i`).
- When df and du disagree, look for deleted-but-open files (`lsof +L1`) or files hidden under a mount point.
- RAID handles disk failure, not data loss; you still need backups.
