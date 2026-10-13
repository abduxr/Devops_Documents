# Part 7 — Bash Scripting: Basics to Production Quality

Shell scripts glue DevOps together: bootstrap scripts, CI steps, container entrypoints, backup jobs, health checks. Bash is everywhere, starts instantly and talks natively to every CLI tool. It is also full of sharp edges. This Part teaches the language and, more importantly, the habits that make scripts safe to run unattended.

> **Tip:** Install **ShellCheck** (`apt install shellcheck`, `dnf install ShellCheck`) and run it on every script, ideally in CI. It catches most of the bugs described in this Part.

## 7.1 The First Script

```bash
#!/usr/bin/env bash
# hello.sh - greet a user
echo "Hello, ${1:-world}! Today is $(date +%A)."
```

```bash
chmod +x hello.sh
./hello.sh Abdul        # Hello, Abdul! Today is Monday.
bash hello.sh           # runs without execute bit
bash -x hello.sh        # trace each command (debugging)
```

The **shebang** line tells the kernel which interpreter to use. `#!/usr/bin/env bash` finds bash in `PATH` (portable); `#!/bin/bash` is fine on Linux. Use `#!/bin/sh` only if you write strictly POSIX code, because on Debian/Ubuntu `/bin/sh` is dash and on Alpine it's BusyBox ash.

## 7.2 Variables and Expansion

```bash
name="web01"               # no spaces around '='
readonly REGION="eu-west-1"
count=3
echo "$name has $count cores"
echo "${name}-backup"      # braces separate the name from following text
files=$(ls /etc | wc -l)   # command substitution
today=$(date +%F)
```

Scoping: variables are global to the script unless declared `local` inside a function. `export VAR` makes it part of the environment passed to child processes.

### 7.2.1 Parameter expansion (learn these, they replace many sed/awk calls)

| Syntax | Result |
|---|---|
| `${var:-default}` | Value, or `default` if unset/empty |
| `${var:=default}` | Same, and assign it |
| `${var:?message}` | Value, or exit with error `message` if unset/empty |
| `${var:+alt}` | `alt` if var is set, else empty |
| `${#var}` | Length |
| `${var#prefix}` / `${var##prefix}` | Strip shortest/longest prefix pattern |
| `${var%suffix}` / `${var%%suffix}` | Strip shortest/longest suffix pattern |
| `${var/old/new}` / `${var//old/new}` | Replace first / all |
| `${var:offset:len}` | Substring |
| `${var^^}` / `${var,,}` | Upper / lower case |

```bash
path="/var/log/nginx/access.log.gz"
echo "${path##*/}"      # access.log.gz   (basename)
echo "${path%/*}"       # /var/log/nginx  (dirname)
echo "${path%.gz}"      # /var/log/nginx/access.log
file="report.tar.gz"; echo "${file%%.*}"   # report
tag="v1.4.2"; echo "${tag#v}"              # 1.4.2
env="Prod"; echo "${env,,}"                # prod
```

### 7.2.2 Special parameters

| Parameter | Meaning |
|---|---|
| `$0` | Script name |
| `$1`..`$9`, `${10}` | Positional arguments |
| `$#` | Number of arguments |
| `"$@"` | All arguments, each separately quoted (use this) |
| `"$*"` | All arguments as one string |
| `$?` | Exit status of last command |
| `$$` | PID of the script |
| `$!` | PID of last background job |

```bash
for arg in "$@"; do echo "arg: $arg"; done   # handles spaces correctly
```

## 7.3 Conditions

`if` runs a command and branches on its exit status. `[[ ... ]]` is bash's test command (safer than `[`: no word-splitting, supports `&&`, `||`, `=~`).

```bash
if [[ -f /etc/nginx/nginx.conf ]]; then
  echo "nginx config present"
elif [[ -d /etc/httpd ]]; then
  echo "apache"
else
  echo "no web server"
fi
```

| Test | True if |
|---|---|
| `-e f` / `-f f` / `-d f` / `-L f` | Exists / regular file / directory / symlink |
| `-r` / `-w` / `-x` | Readable / writable / executable |
| `-s f` | Size > 0 |
| `-z s` / `-n s` | String empty / non-empty |
| `a == b`, `a != b` | String compare (`==` does glob matching inside `[[ ]]`) |
| `s =~ regex` | Regex match; captures in `BASH_REMATCH` |
| `-eq -ne -lt -le -gt -ge` | Integer compare |
| `f1 -nt f2` | f1 newer than f2 |

```bash
(( disk_pct > 90 )) && echo "disk critical"     # arithmetic context, no $ needed
[[ $version =~ ^v([0-9]+)\.([0-9]+) ]] && echo "major=${BASH_REMATCH[1]}"
if grep -q "^ok" /tmp/status; then echo healthy; fi   # any command works in if
command -v docker >/dev/null || { echo "docker missing" >&2; exit 1; }
```

### 7.3.1 case

```bash
case "$1" in
  start)   start_service ;;
  stop)    stop_service ;;
  restart) stop_service; start_service ;;
  status|st) show_status ;;
  *.tar.gz) extract "$1" ;;
  *) echo "Usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
```

## 7.4 Loops

```bash
for host in web1 web2 web3; do
  ssh "$host" uptime
done

for i in {1..5}; do echo "attempt $i"; done
for ((i = 0; i < 10; i += 2)); do echo "$i"; done

for f in /var/log/app/*.log; do
  [[ -e $f ]] || continue          # glob matched nothing
  gzip "$f"
done

# Read a file line by line (the correct way)
while IFS= read -r line; do
  echo "line: $line"
done < hosts.txt

# Read command output without a subshell losing variables
count=0
while IFS= read -r pod; do
  ((++count))
done < <(kubectl get pods -o name)
echo "$count pods"

# Retry until success
until curl -fsS http://localhost:8080/health >/dev/null; do
  echo "waiting..."; sleep 2
done
```

`break` exits a loop; `continue` skips to the next iteration. `IFS= read -r` preserves leading whitespace and backslashes.

> **Warning:** `cat file | while read ...; do count=$((count+1)); done` runs the loop in a subshell, so `count` is lost afterwards. Use redirection `< file` or process substitution `< <(cmd)`.

## 7.5 Functions

```bash
log() {
  local level=$1; shift
  printf '%s [%s] %s\n' "$(date -u +%FT%TZ)" "$level" "$*" >&2
}

is_port_open() {
  local host=$1 port=$2
  timeout 2 bash -c "</dev/tcp/$host/$port" 2>/dev/null
}

if is_port_open db.internal 5432; then
  log INFO "database reachable"
else
  log ERROR "database unreachable"
fi
```

- Functions receive arguments as `$1`, `$2`, `"$@"`.
- `return N` sets the exit status (0–255); to return *data*, print it and capture with `$(...)`.
- Always declare internal variables `local`.
- Send diagnostics to stderr (`>&2`) so stdout stays clean for data.

## 7.6 Arrays

```bash
# Indexed arrays
servers=(web1 web2 "web 3")
echo "${servers[0]}"           # web1
echo "${#servers[@]}"          # 3
servers+=(web4)
for s in "${servers[@]}"; do echo "$s"; done    # quoted: "web 3" stays one item
echo "${servers[@]:1:2}"       # slice

# Build command arguments safely
args=(--region "$REGION" --output json)
[[ -n $PROFILE ]] && args+=(--profile "$PROFILE")
aws ec2 describe-instances "${args[@]}"

# Read lines into an array
mapfile -t users < <(cut -d: -f1 /etc/passwd)

# Associative arrays (bash 4+)
declare -A port=([web]=80 [api]=8080 [db]=5432)
port[cache]=6379
for svc in "${!port[@]}"; do echo "$svc -> ${port[$svc]}"; done
```

Using an array for command arguments is the correct alternative to building a command string and running `eval`.

## 7.7 Strict Mode and Error Handling

By default bash ignores failures and keeps going, which in automation means a failed `cd` followed by `rm -rf *` in the wrong directory. Start every script with:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
```

| Option | Effect |
|---|---|
| `-e` (errexit) | Exit when a command fails |
| `-u` (nounset) | Error on unset variables (catches typos like `$DIRR`) |
| `-o pipefail` | A pipeline fails if *any* command in it fails, not just the last |
| `-E` (errtrace) | ERR traps are inherited by functions and subshells |
| `IFS=$'\n\t'` | Word-split only on newline/tab, not spaces (optional, debated) |

Why `pipefail` matters:

```bash
set -e
curl -fsS https://bad.example.com/data | jq .   # without pipefail: jq succeeds on empty input, script continues
```

Know the limits of `set -e`. It is **not** triggered for commands in `if`/`while` conditions, commands before `&&`/`||`, or (without `inherit_errexit`) inside `$(...)` assignments combined with `local`. Write explicit checks where it counts:

```bash
local out              # declare separately...
out=$(some_cmd)        # ...so the failure isn't masked by 'local'

if ! systemctl restart myapp; then
  log ERROR "restart failed"; exit 1
fi

value=$(grep -c foo file || true)   # explicitly allow failure (grep exits 1 on no match)
```

### 7.7.1 traps

`trap` runs code when the script exits or receives a signal. It's how you guarantee cleanup.

```bash
tmpdir=$(mktemp -d)
cleanup() {
  local rc=$?
  rm -rf -- "$tmpdir"
  (( rc != 0 )) && log ERROR "failed with exit code $rc"
  exit "$rc"
}
trap cleanup EXIT
trap 'log ERROR "error on line $LINENO: $BASH_COMMAND"' ERR
trap 'log WARN "interrupted"; exit 130' INT TERM
```

Use `mktemp` for temporary files; never hard-code `/tmp/myscript.tmp` (races, symlink attacks, collisions between concurrent runs).

### 7.7.2 Locking

Prevent two copies running at once (cron overlap, double deploys):

```bash
exec 9>/run/lock/deploy.lock
flock -n 9 || { echo "another deploy is running" >&2; exit 1; }
```

## 7.8 Argument Parsing with getopts

```bash
usage() {
  cat <<EOF
Usage: ${0##*/} [-e env] [-n] [-v] <service>
  -e ENV   target environment (default: staging)
  -n       dry run
  -v       verbose
EOF
}

env="staging" dry_run=0 verbose=0
while getopts ":e:nvh" opt; do
  case $opt in
    e) env=$OPTARG ;;
    n) dry_run=1 ;;
    v) verbose=1 ;;
    h) usage; exit 0 ;;
    :) echo "Option -$OPTARG requires an argument" >&2; usage; exit 2 ;;
    \?) echo "Unknown option -$OPTARG" >&2; usage; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
service=${1:?$(usage)}
(( verbose )) && set -x
```

`getopts` handles short options only. For long options (`--env`), write a `while (( $# )); do case $1 in --env) env=$2; shift 2;; ... esac; done` loop.

## 7.9 Useful Idioms

```bash
# Run a command with retries and backoff
retry() {
  local -i max=$1 n=1; shift
  until "$@"; do
    (( n >= max )) && { log ERROR "failed after $n attempts: $*"; return 1; }
    log WARN "attempt $n failed, retrying in $((2 ** n))s"
    sleep $((2 ** n)); ((n++))
  done
}
retry 5 curl -fsS https://api.example.com/health

# Require root
(( EUID == 0 )) || { echo "run as root" >&2; exit 1; }

# Require commands
for c in curl jq aws; do command -v "$c" >/dev/null || { echo "missing $c" >&2; exit 1; }; done

# Script's own directory (works regardless of cwd)
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# Confirm dangerous actions
read -rp "Delete $target? [y/N] " ans
[[ $ans == [yY] ]] || exit 1

# Default config file, override with env var
CONFIG=${CONFIG:-/etc/myapp/config.env}
[[ -r $CONFIG ]] && source "$CONFIG"

# printf over echo for anything non-trivial (portable, no -e surprises)
printf '%-20s %5d\n' "$name" "$count"

# Timing
start=$SECONDS; do_work; echo "took $((SECONDS - start))s"

# Parallel work with a limit
xargs -P 4 -I{} ./process.sh {} < items.txt
```

## 7.10 Real DevOps Scripts

### 7.10.1 Disk usage alert

```bash
#!/usr/bin/env bash
# disk-alert.sh - warn when any filesystem exceeds a threshold
set -Eeuo pipefail

THRESHOLD=${THRESHOLD:-85}
WEBHOOK=${WEBHOOK:-}           # e.g. Slack/Teams incoming webhook URL
host=$(hostname -f 2>/dev/null || hostname)

alerts=()
while read -r target pcent; do
  pct=${pcent%\%}
  if (( pct >= THRESHOLD )); then
    alerts+=("$target at ${pct}%")
  fi
done < <(df --output=target,pcent -x tmpfs -x devtmpfs -x overlay -x squashfs | tail -n +2)

(( ${#alerts[@]} == 0 )) && exit 0

msg="Disk alert on $host: $(printf '%s; ' "${alerts[@]}")"
logger -t disk-alert "$msg"
echo "$msg" >&2

if [[ -n $WEBHOOK ]]; then
  curl -fsS -m 10 -H 'Content-Type: application/json' \
       -d "$(jq -n --arg t "$msg" '{text: $t}')" "$WEBHOOK" >/dev/null
fi
exit 1
```

Note the use of `jq -n --arg` to build JSON: string concatenation breaks as soon as the message contains a quote.

### 7.10.2 Backup with rotation

```bash
#!/usr/bin/env bash
# backup.sh - archive a directory, keep N days, upload to S3
set -Eeuo pipefail

SRC=${1:?usage: backup.sh <dir> [bucket]}
BUCKET=${2:-}
DEST=/var/backups/$(basename "$SRC")
KEEP_DAYS=${KEEP_DAYS:-7}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$DEST/$(basename "$SRC")-$stamp.tar.zst"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

exec 9>"/run/lock/backup-$(basename "$SRC").lock"
flock -n 9 || { log "backup already running"; exit 0; }

mkdir -p "$DEST"
tmp=$(mktemp "$DEST/.partial.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT

log "archiving $SRC"
tar --zstd -cf "$tmp" -C "$(dirname "$SRC")" "$(basename "$SRC")"
mv -- "$tmp" "$archive"                         # atomic: never leave half-written backups
sha256sum "$archive" > "$archive.sha256"
log "created $archive ($(du -h "$archive" | cut -f1))"

if [[ -n $BUCKET ]]; then
  aws s3 cp --only-show-errors "$archive" "s3://$BUCKET/$(hostname)/"
  aws s3 cp --only-show-errors "$archive.sha256" "s3://$BUCKET/$(hostname)/"
  log "uploaded to s3://$BUCKET"
fi

find "$DEST" -name '*.tar.zst*' -mtime +"$KEEP_DAYS" -print -delete | sed 's/^/pruned: /' >&2
```

### 7.10.3 Rolling restart across hosts

```bash
#!/usr/bin/env bash
# rolling-restart.sh - restart a service one host at a time, stop on first failure
set -Eeuo pipefail

service=${1:?service name}; shift
hosts=("$@")
(( ${#hosts[@]} )) || { echo "usage: $0 <service> host1 [host2...]" >&2; exit 2; }

for h in "${hosts[@]}"; do
  echo ">>> $h: restarting $service"
  ssh -o BatchMode=yes "$h" "sudo systemctl restart '$service'"
  for i in {1..30}; do
    if ssh -o BatchMode=yes "$h" "systemctl is-active --quiet '$service'" &&
       ssh -o BatchMode=yes "$h" "curl -fsS -m 2 http://localhost:8080/health >/dev/null"; then
      echo ">>> $h healthy"; break
    fi
    (( i == 30 )) && { echo "!!! $h failed health check, aborting" >&2; exit 1; }
    sleep 2
  done
done
echo "all hosts restarted"
```

At this scale, you'd normally reach for Ansible (`serial: 1`), covered in Part 9, but understanding the logic helps you design any rollout.

### 7.10.4 Container entrypoint

```bash
#!/bin/sh
# docker-entrypoint.sh - POSIX sh because Alpine/distroless-like images may lack bash
set -eu

: "${APP_PORT:=8080}"
if [ -n "${DB_HOST:-}" ]; then
  echo "waiting for $DB_HOST:${DB_PORT:-5432}"
  i=0
  until nc -z "$DB_HOST" "${DB_PORT:-5432}"; do
    i=$((i + 1)); [ "$i" -ge 30 ] && { echo "db not reachable" >&2; exit 1; }
    sleep 1
  done
fi

# exec replaces the shell so the app becomes PID 1 and receives SIGTERM directly
exec "$@"
```

The `exec "$@"` line is essential: without it, the shell stays PID 1, may not forward SIGTERM, and `docker stop` waits for the timeout and then SIGKILLs your app.

## 7.11 When Not to Use Bash

Switch to Python (or Go) when you need: data structures beyond flat arrays, JSON manipulation beyond a few `jq` calls, unit tests, complex error handling, HTTP APIs with pagination/retries, or the script passes ~200 lines. Bash is best as glue between existing tools.

## 7.12 Lab: Write a Production-Grade Health Check Script

Goal: build `svc-check.sh` that checks a list of services and endpoints, supports options, logs properly, cleans up, and passes ShellCheck.

1. Create a config file listing checks:

   ```bash
   mkdir -p ~/lab7 && cd ~/lab7
   cat > checks.conf <<'EOF'
   # type   target
   unit     ssh
   unit     cron
   tcp      127.0.0.1:22
   http     https://example.com
   EOF
   ```

2. Write the script skeleton with strict mode, logging and a trap:

   ```bash
   cat > svc-check.sh <<'EOF'
   #!/usr/bin/env bash
   set -Eeuo pipefail

   CONF="./checks.conf"; TIMEOUT=3; QUIET=0
   log() { (( QUIET )) || printf '%s %-5s %s\n' "$(date +%T)" "$1" "${*:2}" >&2; }
   trap 'log ERROR "line $LINENO: $BASH_COMMAND"' ERR

   usage() { echo "Usage: ${0##*/} [-c conf] [-t timeout] [-q]"; }
   while getopts ":c:t:qh" o; do
     case $o in
       c) CONF=$OPTARG ;; t) TIMEOUT=$OPTARG ;; q) QUIET=1 ;;
       h) usage; exit 0 ;; *) usage >&2; exit 2 ;;
     esac
   done
   [[ -r $CONF ]] || { log ERROR "cannot read $CONF"; exit 2; }

   check_unit() { systemctl is-active --quiet "$1"; }
   check_tcp()  { local h=${1%:*} p=${1##*:}; timeout "$TIMEOUT" bash -c "</dev/tcp/$h/$p" 2>/dev/null; }
   check_http() { curl -fsS -o /dev/null -m "$TIMEOUT" "$1"; }

   declare -A results=()
   failed=0
   while read -r type target; do
     [[ -z ${type:-} || $type == \#* ]] && continue
     if "check_$type" "$target"; then
       results["$type:$target"]=OK; log INFO "OK   $type $target"
     else
       results["$type:$target"]=FAIL; log WARN "FAIL $type $target"; ((++failed))
     fi
   done < "$CONF"

   printf '%-40s %s\n' CHECK RESULT
   for k in "${!results[@]}"; do printf '%-40s %s\n' "$k" "${results[$k]}"; done | sort
   exit $(( failed > 0 ))
   EOF
   chmod +x svc-check.sh
   ```

3. Run it, then break something and run again:

   ```bash
   ./svc-check.sh; echo "exit=$?"
   echo "tcp 127.0.0.1:9" >> checks.conf
   ./svc-check.sh -t 1; echo "exit=$?"
   ./svc-check.sh -q -c /nonexistent; echo "exit=$?"
   ```

4. Why `((++failed))` and not `((failed++))`? Under `set -e`, `((failed++))` evaluates to 0 the first time (post-increment returns the old value), which counts as failure and would exit the script. Test it to see for yourself.

5. Run ShellCheck and fix anything it reports: `shellcheck svc-check.sh`.

6. Add a `-j` option that prints results as JSON using `jq -n` (hint: build with `--arg` per item, or print `key<TAB>value` lines and pipe to `jq -R -n '[inputs | split("\t") | {check: .[0], result: .[1]}]'`).

7. Schedule it every 5 minutes with a systemd timer (reuse the pattern from Part 4) and view results with `journalctl -u svc-check`.

## Key takeaways

- Start scripts with `#!/usr/bin/env bash` and `set -Eeuo pipefail`; know where `set -e` doesn't apply.
- Quote every expansion; use `"$@"` and arrays for argument lists instead of strings and `eval`.
- Parameter expansion (`${var:-x}`, `${var##*/}`, `${var:?}`) replaces many external commands and protects against empty variables.
- Use `[[ ]]` for tests, `(( ))` for arithmetic, `case` for dispatch, `while IFS= read -r` for lines.
- `trap ... EXIT` plus `mktemp` guarantees cleanup; `flock` prevents concurrent runs; write to a temp file then `mv` for atomic output.
- Logs go to stderr, data to stdout; exit codes are your API.
- In container entrypoints, finish with `exec "$@"` so the app gets signals.
- Lint with ShellCheck; move to Python when the script outgrows glue.
