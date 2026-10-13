# Part 2 — The Shell, Files and Text Processing

The shell is your primary interface to every Linux machine you'll ever operate. On a server there is no GUI; in a container there may not even be an editor. Fluency here is the single biggest productivity multiplier in this course.

## 2.1 Shell Basics

A **shell** is a program that reads commands, expands them (variables, globs, substitutions), and asks the kernel to run programs. **bash** is the default interactive shell on most distros; `/bin/sh` is often **dash** (Debian/Ubuntu) or BusyBox `ash` (Alpine), which are smaller POSIX shells. That matters for scripts: a script with `#!/bin/sh` must not use bash-only features.

A **terminal** (or terminal emulator, or SSH session) is just the window that carries text to and from the shell.

### 2.1.1 Anatomy of a command

```text
  sudo   ls    -l -a   --color=auto   /var/log   > out.txt
  |      |     |       |              |          |
 prefix command short   long option   argument   redirection
               options
```

- Short options can be combined: `ls -la` equals `ls -l -a`.
- `--` ends options: `rm -- -weird-filename`.
- The shell does the expansion *before* the program runs. `ls *.log` never sees `*`; it receives the list of matching filenames.

### 2.1.2 Navigation

```bash
pwd                 # print working directory
cd /var/log         # absolute path
cd ..               # parent
cd -                # previous directory
cd                  # home (~)
ls -lah             # long, all (hidden), human sizes
ls -lt | head       # newest first
ls -ld /etc         # the directory itself, not its contents
tree -L 2 /etc/ssh  # if installed
```

Relative paths are resolved from the current directory; absolute paths start with `/`. `~` expands to `$HOME`, `~alice` to Alice's home.

### 2.1.3 Working with files and directories

```bash
touch app.log                 # create empty / update mtime
mkdir -p project/{src,tests,docs}   # -p creates parents; braces expand
cp -a src/ backup/            # -a = archive: recursive, keep perms, times, links
mv old.txt new.txt            # rename or move
rm file; rm -r dir            # remove (no recycle bin!)
rm -rf -- "$dir"              # quote variables; never rm -rf "$UNSET/"
ln -s /opt/app/current/bin/app /usr/local/bin/app   # symlink
stat file                     # inode, perms, times
file mystery.bin              # identify content type
```

> **Warning:** There is no undo for `rm`. In scripts, guard with `${dir:?}` (fails if empty) so `rm -rf "${dir:?}/"` can never become `rm -rf /`.

**Hard links vs symlinks.** A filename is a directory entry pointing to an **inode** (the object holding metadata and data block pointers). A hard link is a second name for the same inode (same filesystem only, not for directories). A symlink is a tiny file containing a path; it can cross filesystems and can dangle if the target is removed.

```bash
ln data.txt hard.txt; ln -s data.txt soft.txt
ls -li data.txt hard.txt soft.txt
```

```text
1311 -rw-r--r-- 2 abdul abdul 12 Oct 12 10:00 data.txt
1311 -rw-r--r-- 2 abdul abdul 12 Oct 12 10:00 hard.txt
1312 lrwxrwxrwx 1 abdul abdul  8 Oct 12 10:00 soft.txt -> data.txt
```

Note the same inode number (1311) and link count 2.

### 2.1.4 Viewing files

```bash
cat file                 # whole file
less /var/log/syslog     # pager: / search, G end, g start, F follow, q quit
head -n 20 file; tail -n 50 file
tail -f /var/log/nginx/access.log   # follow appended lines
tail -F app.log          # follow by name, survives log rotation
wc -l file               # count lines
```

### 2.1.5 Globbing, quoting and expansion

| Pattern | Matches |
|---|---|
| `*` | Any string (not leading `.`) |
| `?` | Any single character |
| `[abc]`, `[0-9]` | One character from the set |
| `{a,b}` | Brace expansion (generates words, not a match) |
| `**` | Recursive (bash with `shopt -s globstar`) |

Quoting decides what the shell expands:

```bash
name="world"
echo "Hello $name"     # double quotes: variables expanded -> Hello world
echo 'Hello $name'     # single quotes: literal -> Hello $name
echo "Today is $(date +%F)"   # command substitution
echo $((3 * 4))        # arithmetic -> 12
```

> **Tip:** Quote every variable expansion (`"$file"`) unless you specifically want word-splitting. Unquoted variables containing spaces or globs are the number one source of shell bugs.

### 2.1.6 History and productivity

```bash
history | tail
!!            # repeat last command (sudo !! is classic)
!$            # last argument of previous command
Ctrl-r        # reverse search history
Ctrl-a / Ctrl-e   # start / end of line
Ctrl-w / Ctrl-u   # delete word / to start of line
Ctrl-l        # clear screen
alias ll='ls -lah'      # put in ~/.bashrc
```

Startup files: login shells read `/etc/profile` and `~/.bash_profile` (or `~/.profile`); interactive non-login shells read `~/.bashrc`. Most distros make `~/.bash_profile` source `~/.bashrc`, so put aliases and functions in `~/.bashrc`, and environment variables in `~/.profile` or `~/.bash_profile`.

### 2.1.7 Environment variables and PATH

```bash
echo "$PATH"
export EDITOR=vim          # export: visible to child processes
env | sort | head
which -a python3; command -v python3   # command -v is POSIX, use in scripts
```

`PATH` is searched left to right for commands. A process inherits a *copy* of its parent's environment; a child can never change the parent's environment, which is why `cd` must be a shell builtin and why you `source` a file (rather than execute it) to set variables in your current shell.

## 2.2 Streams, Pipes and Redirection

Every process starts with three open file descriptors:

| FD | Name | Default |
|---|---|---|
| 0 | stdin | keyboard / terminal |
| 1 | stdout | terminal |
| 2 | stderr | terminal |

```bash
cmd > out.txt          # stdout to file (truncate)
cmd >> out.txt         # append
cmd 2> err.txt         # stderr to file
cmd > all.txt 2>&1     # both to file (order matters!)
cmd &> all.txt         # bash shorthand for the same
cmd 2>/dev/null        # discard errors
cmd < input.txt        # stdin from file
cmd1 | cmd2            # stdout of cmd1 -> stdin of cmd2
cmd 2>&1 | less        # pipe both streams
cmd | tee out.txt      # write to file AND stdout
echo "x" | sudo tee /etc/protected > /dev/null   # write as root
```

Why does `sudo echo x > /etc/file` fail? The redirection is performed by *your* shell (unprivileged) before `sudo` runs. `tee` is run by sudo, so it opens the file as root.

Why does `2>&1 > file` not work? Redirections are processed left to right: `2>&1` first points stderr at the *current* stdout (the terminal), then stdout moves to the file.

**Here documents and here strings:**

```bash
cat > /etc/myapp.conf <<'EOF'
listen = 0.0.0.0:8080
log_level = info
EOF

grep -c error <<< "$log_text"
```

Quoting the delimiter (`'EOF'`) disables variable expansion inside the heredoc, which is what you usually want for config files containing `$`.

**Process substitution** (bash) lets a command's output appear as a file:

```bash
diff <(ssh web1 cat /etc/nginx/nginx.conf) <(ssh web2 cat /etc/nginx/nginx.conf)
```

**Exit codes.** Every command returns 0 for success and 1–255 for failure. `$?` holds the last one. `&&` runs the next command only on success; `||` only on failure.

```bash
mkdir -p /opt/app && cd /opt/app || exit 1
```

## 2.3 Finding Things

### 2.3.1 find

`find` walks the directory tree and evaluates tests on each entry.

```bash
find /var/log -name "*.log" -mtime -1           # modified in last 24h
find / -xdev -type f -size +500M 2>/dev/null     # big files, stay on one fs
find . -type f -name "*.tmp" -delete
find /etc -type f -newer /tmp/marker             # changed since marker
find /srv -type d -perm -o+w                     # world-writable dirs
find /home -user alice -iname "*.PDF"
find . -name "*.py" -exec grep -l "TODO" {} +    # {} + batches args (fast)
find /data -mindepth 1 -maxdepth 1 -mtime +30 -print   # preview before delete
```

`-exec cmd {} \;` runs one process per file; `-exec cmd {} +` passes many files per invocation, like xargs.

### 2.3.2 xargs

`xargs` turns stdin into arguments. Always pair `find -print0` with `xargs -0` so filenames with spaces or newlines work.

```bash
find . -name "*.log" -print0 | xargs -0 gzip
cat hosts.txt | xargs -I{} -P 8 ssh {} uptime     # 8 in parallel
kubectl get pods -o name | xargs -n1 kubectl delete   # one arg per run
```

### 2.3.3 locate, which, whereis

`locate` (from `plocate`/`mlocate`) queries a prebuilt database: fast but may be stale (`sudo updatedb`). `which` and `command -v` find executables in PATH.

## 2.4 Text Processing: The DevOps Toolkit

Logs, configs, CSV exports, command output: it's all text. These tools compose through pipes.

### 2.4.1 grep

```bash
grep "ERROR" app.log
grep -i "timeout" app.log          # case-insensitive
grep -v "healthcheck" access.log   # invert
grep -n -C 3 "panic" app.log       # line numbers, 3 lines context
grep -r "db_host" /etc/myapp/      # recursive
grep -rl "password" .              # list matching files only
grep -c " 500 " access.log         # count matching lines
grep -E "5[0-9]{2} " access.log    # extended regex
grep -o -E "[0-9]{1,3}(\.[0-9]{1,3}){3}" access.log | sort -u   # extract IPs
grep -F "a.b[c]" file              # fixed string, no regex
grep -q "^ok$" status && echo healthy   # quiet: only exit code
```

`ripgrep` (`rg`) is a faster, gitignore-aware alternative worth installing on workstations.

### 2.4.2 cut, sort, uniq, wc, tr

```bash
cut -d: -f1,7 /etc/passwd          # fields 1 and 7, ':' delimiter
cut -c1-10 file                    # characters
sort -n                            # numeric
sort -k3,3nr                       # by 3rd field, numeric, reverse
sort -t, -k2 data.csv              # comma-separated
sort -u                            # sort + unique
uniq -c                            # count adjacent duplicates (sort first!)
tr 'a-z' 'A-Z'                     # translate characters
tr -d '\r'                         # strip Windows CR
tr -s ' '                          # squeeze repeated spaces
paste -sd+ nums.txt | bc           # sum a column
column -t                          # align into a table
```

The canonical "top N" pipeline:

```bash
# Top 10 client IPs in an nginx access log
awk '{print $1}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head
```

```text
   4821 203.0.113.42
   1210 198.51.100.7
    388 192.0.2.15
```

### 2.4.3 sed

`sed` is a stream editor: it applies edits to each line.

```bash
sed 's/foo/bar/' file              # first match per line
sed 's/foo/bar/g' file             # all matches
sed -n '10,20p' file               # print lines 10-20
sed '/^#/d; /^$/d' nginx.conf      # drop comments and blank lines
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i.bak 's|/old/path|/new/path|g' app.conf   # keep a backup; '|' delimiter
sed -E 's/([0-9]+)ms/\1 ms/g' file  # extended regex with capture group
```

> **Warning:** `sed -i` differs between GNU (Linux) and BSD (macOS). On macOS, `sed -i '' 's/a/b/' file`. On Linux, `sed -i 's/a/b/' file`. Using `-i.bak` works on both.

### 2.4.4 awk

`awk` is a small language for field-based text. It splits each line into `$1`, `$2`, ... (`$0` is the whole line, `NF` the number of fields, `NR` the line number).

```bash
awk '{print $1, $9}' access.log                 # IP and status code
awk -F: '$3 >= 1000 {print $1}' /etc/passwd     # human users
awk '$9 >= 500' access.log                      # filter: 5xx responses
awk '{sum += $10} END {print sum/1024/1024 " MB"}' access.log   # total bytes
awk '{c[$9]++} END {for (s in c) print s, c[s]}' access.log | sort  # count by status
df -h | awk 'NR>1 && $5+0 > 80 {print $6, $5}'  # filesystems over 80%
ps aux | awk '{mem[$1]+=$6} END {for (u in mem) print u, mem[u]/1024 " MB"}'
```

```text
200 48211
301 1022
404 377
502 41
```

Rule of thumb: `grep` to filter lines, `cut` for simple delimiters, `awk` for fields with logic or aggregation, `sed` for substitutions. Past ~10 lines of awk, switch to Python.

### 2.4.5 jq: JSON on the command line

Cloud CLIs, Kubernetes and APIs speak JSON. `jq` is mandatory.

```bash
echo '{"name":"web","ports":[80,443],"tags":{"env":"prod"}}' | jq .
jq -r '.name'                     # raw string, no quotes -> web
jq '.ports[0]'                    # -> 80
jq '.tags.env'
jq -r '.items[].metadata.name'    # iterate arrays
jq '.items | length'
jq '.[] | select(.status == "running") | .id'
jq -r '.[] | [.name, .ip] | @tsv' # tab-separated output
jq '{name, env: .tags.env}'       # build a new object
jq --arg v "$VERSION" '.version = $v' package.json > tmp && mv tmp package.json
```

Real examples:

```bash
# All running EC2 instance IDs and private IPs
aws ec2 describe-instances \
  --filters Name=instance-state-name,Values=running \
  | jq -r '.Reservations[].Instances[] | [.InstanceId, .PrivateIpAddress] | @tsv'

# Pods not in Running phase
kubectl get pods -A -o json \
  | jq -r '.items[] | select(.status.phase != "Running")
           | "\(.metadata.namespace)/\(.metadata.name) \(.status.phase)"'
```

For YAML, `yq` (the Go version by Mike Farah) uses nearly identical syntax.

### 2.4.6 Other tools worth knowing

```bash
diff -u old.conf new.conf          # unified diff
comm -23 <(sort a) <(sort b)       # lines only in a
tar -czf backup.tgz /etc           # create gzip tarball
tar -tzf backup.tgz | head         # list
tar -xzf backup.tgz -C /restore    # extract into dir
tar --zstd -cf backup.tar.zst dir  # zstd: faster, better ratio
sha256sum file; sha256sum -c SHA256SUMS   # verify downloads
base64 -d <<< "aGVsbG8="            # decode -> hello
watch -n 2 'ss -s'                 # rerun every 2s
```

## 2.5 Editors: vim and nano

You will edit files over SSH on boxes with nothing but `vi`. Learn enough vim to survive; use nano when it's there and you're in a hurry.

### 2.5.1 nano

Commands are shown on screen: `Ctrl-O` save, `Ctrl-X` exit, `Ctrl-W` search, `Alt-U` undo.

### 2.5.2 vim survival guide

vim is **modal**. Normal mode is for moving and commanding; Insert mode is for typing.

| Action | Keys |
|---|---|
| Enter insert mode | `i` (before cursor), `a` (after), `o` (new line below) |
| Back to normal mode | `Esc` |
| Save / quit / both / quit without saving | `:w` / `:q` / `:wq` or `ZZ` / `:q!` |
| Move | `h j k l`, `w`/`b` words, `0`/`$` line start/end |
| Top / bottom / line 42 | `gg` / `G` / `42G` or `:42` |
| Delete line / word / char | `dd` / `dw` / `x` |
| Copy (yank) line / paste | `yy` / `p` |
| Undo / redo | `u` / `Ctrl-r` |
| Search / next | `/pattern` / `n` |
| Replace in file | `:%s/old/new/g` |
| Visual select | `v` (chars), `V` (lines) |
| Show line numbers | `:set number` |
| Paste without auto-indent mess | `:set paste` |

> **Tip:** `sudoedit /etc/file` (or `sudo -e`) edits a temporary copy with your own editor settings and writes it back as root. It's safer than running a whole editor as root.

Set your default editor for `crontab -e`, `visudo`, `git commit`, and `systemctl edit`:

```bash
echo 'export EDITOR=vim VISUAL=vim' >> ~/.bashrc
sudo update-alternatives --config editor   # Debian/Ubuntu system default
```

## 2.6 Lab: Log Analysis with Pipelines

Goal: answer real operational questions about a web server log using only shell tools.

1. Generate a sample access log (works anywhere with bash):

   ```bash
   mkdir -p ~/lab2 && cd ~/lab2
   for i in $(seq 1 5000); do
     ip="10.0.$((RANDOM % 5)).$((RANDOM % 20))"
     codes=(200 200 200 200 301 404 500 502)
     code=${codes[$((RANDOM % 8))]}
     paths=(/ /api/users /api/orders /login /health)
     path=${paths[$((RANDOM % 5))]}
     bytes=$((RANDOM % 5000))
     ms=$((RANDOM % 900))
     echo "$ip - - [12/Oct/2026:10:$((RANDOM%60)):00 +0000] \"GET $path HTTP/1.1\" $code $bytes ${ms}ms"
   done > access.log
   wc -l access.log
   ```

2. Top 5 client IPs:

   ```bash
   awk '{print $1}' access.log | sort | uniq -c | sort -rn | head -5
   ```

3. Count requests per status code, as a table:

   ```bash
   awk '{print $9}' access.log | sort | uniq -c | sort -rn | column -t
   ```

4. Error rate (percentage of 5xx):

   ```bash
   awk '$9 >= 500 {e++} END {printf "%.2f%%\n", e/NR*100}' access.log
   ```

5. Which path produces most 5xx errors?

   ```bash
   awk '$9 >= 500 {print $7}' access.log | sort | uniq -c | sort -rn
   ```

6. Average latency per path (strip the `ms` suffix):

   ```bash
   awk '{gsub("ms","",$11); s[$7]+=$11; n[$7]++}
        END {for (p in s) printf "%-14s %6.1f ms\n", p, s[p]/n[p]}' access.log | sort -k2 -n
   ```

7. Exclude health checks, and save slow requests (>800 ms) to a file while also viewing them:

   ```bash
   grep -v "/health" access.log | awk '{v=$11; sub("ms","",v); if (v+0 > 800) print}' | tee slow.log | wc -l
   ```

8. Use sed to anonymize the last octet of every IP, writing a new file:

   ```bash
   sed -E 's/^([0-9]+\.[0-9]+\.[0-9]+)\.[0-9]+/\1.xxx/' access.log > anon.log
   head -3 anon.log
   ```

9. jq practice: convert the status counts to JSON and query it:

   ```bash
   awk '{c[$9]++} END {printf "{"; for (k in c) printf "%s\"%s\":%d", (n++?",":""), k, c[k]; print "}"}' access.log > counts.json
   jq . counts.json
   jq 'to_entries | map(select(.key|tonumber >= 500)) | map(.value) | add' counts.json
   ```

10. Find and compress every `.log` file in `~/lab2` safely, keeping the originals (in real life you would add `-mtime +7` to target only old logs):

    ```bash
    find ~/lab2 -name "*.log" -print0 | xargs -0 -r gzip -k
    ls -lh ~/lab2
    ```

## Key takeaways

- The shell expands globs, variables and substitutions *before* the program runs; quote variables to control that.
- stdin/stdout/stderr are file descriptors 0/1/2; redirections are processed left to right, and `sudo` does not apply to your shell's redirections (use `tee`).
- `find ... -print0 | xargs -0` is the safe way to act on many files.
- grep filters, cut slices, sort/uniq count, sed substitutes, awk computes, jq handles JSON. Chaining them answers most operational questions in one line.
- Know enough vim to edit, save and quit on any server; use `sudoedit` for root-owned files.
