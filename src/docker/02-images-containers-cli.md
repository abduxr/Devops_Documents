# Part 2 — Images, Containers and the CLI

With Docker installed, you now need a precise mental model of the two core objects, **images** and **containers**, and fluency with the CLI that manipulates them. Most day-to-day DevOps work with Docker is CLI work: inspecting, filtering, exec-ing, copying, and cleaning up.

## 2.1 Images in Depth

### 2.1.1 What an image is

An image is an immutable, content-addressed bundle made of:

- **Layers**: tar archives of filesystem changes, each identified by a SHA-256 digest.
- **A config object**: JSON describing the default command, entrypoint, environment, user, working directory, exposed ports, labels, and the ordered list of layer diff IDs.
- **A manifest**: JSON pointing to the config and the layers for one platform.
- **An index** (optional, "manifest list"): JSON pointing to several manifests, one per platform (e.g. `linux/amd64`, `linux/arm64`).

```text
 nginx:1.27-alpine  (tag, mutable pointer)
        |
        v
 Image index  sha256:ab12...   (multi-platform)
   |-- manifest linux/amd64  sha256:c3d4...
   |      |-- config  sha256:e5f6...
   |      |-- layer   sha256:1111...  (alpine base)
   |      |-- layer   sha256:2222...  (nginx binaries)
   |      `-- layer   sha256:3333...  (config files)
   `-- manifest linux/arm64  sha256:7788...
```

### 2.1.2 Image references: names, tags, digests

A full reference looks like:

```text
registry.example.com:5000/team/app:1.4.2@sha256:9f86d0...
|------ registry ------| |-repo-| |tag| |---- digest ----|
```

- If you omit the registry, Docker assumes `docker.io`.
- If you omit the namespace on Docker Hub, it assumes `library/` (official images).
- If you omit the tag, it assumes `latest`. `latest` is just a tag name with no special meaning; it is not "the newest".
- A **tag** is mutable: someone can push a new image to `app:1.4.2`.
- A **digest** is immutable: `app@sha256:...` always refers to exactly the same bytes.

> **Warning:** For production deployments, pin by digest (or use immutable tags enforced by your registry). A tag can be moved under you, silently changing what runs.

### 2.1.3 Working with images

```bash
docker pull nginx:1.27-alpine
docker pull --platform linux/arm64 nginx:1.27-alpine
docker image ls
docker image ls --digests
docker image ls --filter dangling=true
docker image inspect nginx:1.27-alpine --format '{{.Config.Cmd}} {{.Architecture}}'
docker image history nginx:1.27-alpine
docker tag nginx:1.27-alpine myregistry.local/web:v1
docker image rm myregistry.local/web:v1
docker image prune          # remove dangling images
docker image prune -a       # remove all images not used by a container
```

```text
$ docker image history nginx:1.27-alpine
IMAGE          CREATED       CREATED BY                                      SIZE
4ff1...        3 weeks ago   CMD ["nginx" "-g" "daemon off;"]                0B
<missing>      3 weeks ago   STOPSIGNAL SIGQUIT                              0B
<missing>      3 weeks ago   EXPOSE map[80/tcp:{}]                           0B
<missing>      3 weeks ago   ENTRYPOINT ["/docker-entrypoint.sh"]            0B
<missing>      3 weeks ago   RUN /bin/sh -c set -x && addgroup -g 101 ...    24.1MB
<missing>      3 weeks ago   ADD alpine-minirootfs-3.20.3-x86_64.tar.gz /    7.8MB
```

`<missing>` simply means those intermediate layers were built elsewhere and have no local image ID; it is normal.

Inspect a remote image without pulling it:

```bash
docker buildx imagetools inspect nginx:1.27-alpine
```

```text
Name:      docker.io/library/nginx:1.27-alpine
MediaType: application/vnd.oci.image.index.v1+json
Digest:    sha256:ab12...
Manifests:
  Name:      docker.io/library/nginx:1.27-alpine@sha256:c3d4...
  Platform:  linux/amd64
  Name:      docker.io/library/nginx:1.27-alpine@sha256:7788...
  Platform:  linux/arm64/v8
  ...
```

### 2.1.4 Saving, loading, exporting

```bash
docker image save -o nginx.tar nginx:1.27-alpine   # image with layers + metadata
docker image load -i nginx.tar
docker container export web -o web-fs.tar          # flattened filesystem of a container, no metadata
docker image import web-fs.tar myflat:latest        # new single-layer image
```

`save`/`load` is how you move images into air-gapped environments. `export`/`import` loses history, CMD, ENV and so on.

## 2.2 Containers and Their Lifecycle

### 2.2.1 States

```text
             docker create
   (image) ---------------> [created]
                                | docker start
                                v
   docker restart  +-----> [running] <------+ docker unpause
         |         |        |   |   \       |
         +---------+        |   |    `---> [paused]
                            |   |  docker pause
            process exits / |   | docker stop (SIGTERM, then SIGKILL after timeout)
            docker kill     v   v
                          [exited]  ---- docker start ----> [running]
                            |
                            | docker rm
                            v
                         (removed)
```

Additional states: `restarting` (restart policy is acting), `removing`, and `dead` (removal failed, usually storage driver trouble).

### 2.2.2 PID 1 and signals

The container's main process runs as PID 1 in its PID namespace. Two consequences:

1. **Signal handling**: the kernel does not apply default signal actions to PID 1. If your app does not install a SIGTERM handler, `docker stop` will wait the full timeout (10 seconds by default) and then SIGKILL it. That is slow and skips graceful shutdown.
2. **Zombie reaping**: PID 1 must `wait()` on orphaned children. Many apps do not, leaving zombies.

Fixes:

```bash
docker run --init myapp        # injects tini as PID 1, which forwards signals and reaps zombies
docker stop -t 30 myapp        # give a longer grace period
docker run --stop-signal SIGQUIT nginx   # or STOPSIGNAL in the Dockerfile
```

Also use the **exec form** of `CMD`/`ENTRYPOINT` (JSON array) so your app is PID 1, not `/bin/sh -c` which does not forward signals. More in Part 3.

### 2.2.3 Exit codes worth memorizing

| Exit code | Meaning |
|---|---|
| 0 | Clean exit |
| 1 | Generic application error |
| 125 | The `docker run` command itself failed (bad flag, daemon error) |
| 126 | Command found but not executable (permissions, wrong arch) |
| 127 | Command not found (typo, missing binary, wrong PATH) |
| 128 + n | Killed by signal n |
| 137 | 128 + 9 = SIGKILL, often OOM-killed or `docker kill`/stop timeout |
| 139 | 128 + 11 = SIGSEGV, segmentation fault |
| 143 | 128 + 15 = SIGTERM, graceful stop |

### 2.2.4 Restart policies

| Policy | Behavior |
|---|---|
| `no` (default) | Never restart. |
| `on-failure[:N]` | Restart on non-zero exit, optionally up to N times. |
| `always` | Always restart; also starts on daemon boot, even if manually stopped before reboot. |
| `unless-stopped` | Like `always`, but stays stopped across daemon restarts if you stopped it manually. |

```bash
docker run -d --restart unless-stopped --name api myorg/api:1.2.0
docker update --restart on-failure:5 api
```

Docker applies exponential backoff between restarts (starting at 100 ms, doubling, capped at 1 minute) to avoid a tight crash loop.

## 2.3 The `docker run` Flags You Must Know

`docker run` = `create` + `start` (+ `attach` unless `-d`). Its flags are the vocabulary of container configuration; Compose and Kubernetes have equivalents for nearly all of them.

| Flag | Purpose | Example |
|---|---|---|
| `-d` | Detached (background) | `-d` |
| `-it` | Interactive + TTY | `-it alpine sh` |
| `--rm` | Remove on exit | `--rm` |
| `--name` | Fixed name | `--name api` |
| `-p host:ctr` | Publish port | `-p 127.0.0.1:8080:80` |
| `-P` | Publish all EXPOSEd ports to random host ports | `-P` |
| `-e`, `--env-file` | Environment variables | `-e LOG_LEVEL=debug` |
| `-v`, `--mount` | Volumes and bind mounts | `--mount type=volume,src=data,dst=/data` |
| `--network` | Attach to network | `--network backend` |
| `-w` | Working directory | `-w /app` |
| `-u` | User / UID:GID | `-u 1000:1000` |
| `--entrypoint` | Override ENTRYPOINT | `--entrypoint sh` |
| `--memory`, `--cpus` | Resource limits | `--memory 512m --cpus 1.5` |
| `--restart` | Restart policy | `--restart unless-stopped` |
| `--health-cmd` | Healthcheck | `--health-cmd "curl -f localhost/healthz"` |
| `--read-only` | Read-only root FS | `--read-only --tmpfs /tmp` |
| `--cap-drop/--cap-add` | Linux capabilities | `--cap-drop ALL --cap-add NET_BIND_SERVICE` |
| `--init` | tini as PID 1 | `--init` |
| `--platform` | Choose image platform | `--platform linux/amd64` |
| `--pull` | `always`, `missing`, `never` | `--pull always` |
| `-l` | Labels | `-l team=payments` |

Everything after the image name replaces `CMD`:

```bash
docker run --rm alpine:3.20 echo "hello"          # CMD replaced by: echo hello
docker run --rm --entrypoint ls alpine:3.20 -la / # ENTRYPOINT replaced by ls, args -la /
```

## 2.4 CLI Deep Dive

### 2.4.1 Management commands

Modern Docker groups commands by object: `docker container ...`, `docker image ...`, `docker volume ...`, `docker network ...`, `docker system ...`, `docker builder ...`. The short forms (`docker ps`, `docker rmi`) are aliases and remain valid.

| Short | Long form |
|---|---|
| `docker ps` | `docker container ls` |
| `docker rm` | `docker container rm` |
| `docker rmi` | `docker image rm` |
| `docker images` | `docker image ls` |
| `docker exec` | `docker container exec` |
| `docker build` | `docker buildx build` (BuildKit) |

### 2.4.2 Listing and filtering

```bash
docker ps                       # running
docker ps -a                    # all, including exited
docker ps -q                    # IDs only (for scripting)
docker ps --filter status=exited --filter label=team=payments
docker ps --filter ancestor=nginx:1.27-alpine
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker ps -s                    # include writable-layer size
```

```text
NAMES     STATUS                    PORTS
api       Up 2 hours (healthy)      0.0.0.0:8080->8080/tcp
redis     Up 2 hours                6379/tcp
worker    Exited (137) 5 min ago
```

### 2.4.3 Inspect and Go templates

`docker inspect` returns the full JSON state. Go templates (`--format`) extract fields; pipe to `jq` for anything complex.

```bash
docker inspect api --format '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}'
docker inspect api --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
docker inspect api --format '{{json .Config.Env}}' | jq
docker inspect api --format '{{.HostConfig.RestartPolicy.Name}}'
docker inspect api --format '{{json .State.Health}}' | jq '.Log[-1]'
docker inspect api | jq '.[0].Mounts'
```

### 2.4.4 Logs

```bash
docker logs api
docker logs -f --tail 100 api        # follow last 100 lines
docker logs --since 15m api
docker logs -t api                   # timestamps
docker logs api 2>&1 | grep -i error # stderr is a separate stream
```

`docker logs` only works for logging drivers that support reading (`json-file`, `local`, `journald`, and others via dual logging). Apps should log to stdout/stderr, not to files inside the container.

### 2.4.5 Exec, attach, cp

```bash
docker exec -it api sh                      # shell into running container
docker exec -u 0 -it api sh                 # as root, for debugging
docker exec api env                         # one-off command
docker exec -e DEBUG=1 -w /app api ./diag.sh
docker attach api                           # attach to PID 1 stdio (detach: Ctrl-P Ctrl-Q)
docker cp api:/app/config.yaml ./config.yaml
docker cp ./fix.conf api:/etc/nginx/conf.d/
```

> **Warning:** Ctrl-C while attached sends SIGINT to PID 1 and may stop the container. Use `docker logs -f` to watch output, and Ctrl-P Ctrl-Q to detach.

For minimal images with no shell (distroless, scratch), use `docker debug` (Docker Desktop / Pro feature) or attach a debug container sharing the target's namespaces:

```bash
docker run --rm -it --pid container:api --network container:api \
  nicolaka/netshoot
```

### 2.4.6 Stats, top, events, diff

```bash
docker stats                        # live CPU/mem/net/IO per container
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'
docker top api                      # processes in the container
docker events --since 30m --filter type=container --filter event=die
docker diff api                     # A=added, C=changed, D=deleted in writable layer
docker port api
```

```text
$ docker diff api
C /tmp
A /tmp/cache.db
C /etc/nginx/conf.d
```

`docker diff` is a quick way to discover that an app is writing to its container filesystem when it should be writing to a volume.

### 2.4.7 Updating, renaming, waiting

```bash
docker update --memory 1g --cpus 2 api   # change limits live
docker rename api api-old
docker wait worker                       # block until exit, print exit code
docker container prune                   # remove all stopped containers
```

### 2.4.8 Bulk operations and scripting

```bash
docker stop $(docker ps -q)                         # stop everything running
docker rm $(docker ps -aq --filter status=exited)   # remove exited
docker ps -aq --filter label=env=test | xargs -r docker rm -f
```

Use `xargs -r` so nothing runs when the list is empty.

### 2.4.9 docker system

```bash
docker system df          # disk usage summary
docker system df -v       # per image/container/volume
docker system info
docker system prune       # stopped containers, unused networks, dangling images, build cache
docker system prune -a --volumes   # aggressive: also unused images and volumes
```

```text
TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
Images          23        6         8.42GB    6.10GB (72%)
Containers      9         4         120MB     88MB (73%)
Local Volumes   11        5         2.3GB     1.1GB (47%)
Build Cache     154       0         3.9GB     3.9GB
```

> **Warning:** `--volumes` deletes data in volumes not attached to a container, including stopped databases' data if their container was removed. Never run it blindly on a server.

## 2.5 Environment Variables and Configuration

Twelve-factor apps take configuration from the environment.

```bash
docker run -e DB_HOST=db -e DB_PORT=5432 myapp
docker run -e DB_PASSWORD myapp          # passes the value from the current shell
docker run --env-file ./app.env myapp
```

```ini
# app.env  (KEY=VALUE, no quotes processing, no export keyword)
DB_HOST=db
DB_PORT=5432
LOG_LEVEL=info
```

> **Warning:** Environment variables are visible in `docker inspect`, in `/proc/<pid>/environ`, and often in crash dumps. For real secrets prefer mounted files (Compose/Swarm secrets, Kubernetes Secrets as volumes, or a secrets manager). See Part 8.

## 2.6 Lab: Lifecycle and CLI Fluency

**Goal:** observe every lifecycle state, exit codes, signal handling, and practice inspection.

1. Create without starting, then start:

```bash
docker create --name lc alpine:3.20 sh -c 'echo started; sleep 300'
docker ps -a --filter name=lc --format '{{.Status}}'   # Created
docker start lc
docker pause lc && docker ps --filter name=lc --format '{{.Status}}'  # (Paused)
docker unpause lc
```

2. Observe the stop timeout problem. `sh` as PID 1 ignores SIGTERM:

```bash
time docker stop lc
docker inspect lc --format '{{.State.ExitCode}}'
```

Expect about 10 seconds and exit code 137 (SIGKILL after timeout).

3. Repeat with `--init`:

```bash
docker run -d --init --name lc2 alpine:3.20 sleep 300
time docker stop lc2
docker inspect lc2 --format '{{.State.ExitCode}}'
```

Expect a near-instant stop and exit code 143.

4. Produce exit codes 127 and 126:

```bash
docker run --rm alpine:3.20 nosuchcmd; echo "exit=$?"
docker run --rm alpine:3.20 /etc/passwd; echo "exit=$?"
```

5. Restart policy with backoff:

```bash
docker run -d --name crashy --restart on-failure:3 alpine:3.20 sh -c 'exit 1'
sleep 5; docker inspect crashy --format '{{.RestartCount}} {{.State.Status}}'
```

6. Inspection drill: start `nginx:1.27-alpine` with `-p 8080:80`, then use templates to print its IP, port bindings, and the image's default CMD. Use `docker diff` after `curl localhost:8080` to see what nginx writes.

7. Clean up:

```bash
docker rm -f lc lc2 crashy $(docker ps -aq --filter ancestor=nginx:1.27-alpine)
docker system df
```

## Key takeaways

- Images are immutable stacks of content-addressed layers plus config; tags are mutable pointers, digests are immutable.
- `latest` means nothing special. Pin versions, and pin digests in production.
- PID 1 does not get default signal handling: use exec-form commands, handle SIGTERM, or use `--init`.
- Learn the exit codes: 125/126/127 for startup problems, 137 for SIGKILL/OOM, 143 for SIGTERM.
- `docker inspect` with `--format` or `jq` answers almost any "what is this container doing" question.
- `docker system df` and targeted `prune` commands keep hosts healthy; be careful with `--volumes`.
