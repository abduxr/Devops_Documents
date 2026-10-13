# Part 1 — Foundations

This course takes you from "I can commit and push" to someone who can design a branching strategy for a platform team, recover a repository after a force-push accident, wire Git into CI/CD pipelines and GitOps controllers, and answer the hard questions in a senior DevOps interview. Every Part builds on the previous one and ends with a hands-on Lab and a list of key takeaways.

The commands use current Git (2.40 and later). Where older tutorials use `git checkout` for everything, this course prefers the newer, single-purpose commands `git switch` (change branches) and `git restore` (restore files), and assumes the default branch is called `main`.

## 1.1 Why Version Control Exists

A version control system (VCS) records changes to a set of files over time so you can answer four questions at any moment:

1. **What** changed?
2. **Who** changed it?
3. **When** did it change?
4. **Why** did it change?

For a DevOps engineer those questions are not academic. When production breaks at 02:00, "what changed in the Terraform repo since yesterday's deploy?" is often the fastest path to the root cause. When an auditor asks who approved a firewall rule, the answer lives in commit history and pull request reviews. When you need to roll back, version control is the undo button for your entire infrastructure.

### 1.1.1 Centralized vs distributed

| Property | Centralized (SVN, Perforce, CVS) | Distributed (Git, Mercurial) |
|---|---|---|
| Where history lives | One central server | Every clone has full history |
| Work offline | Limited (no commits, no log) | Fully: commit, branch, log, diff |
| Branching cost | Often expensive (directory copies) | Cheap (a 41-byte file pointing at a commit) |
| Single point of failure | Server | None; any clone can restore the server |
| Typical workflow | Lock / checkout / commit to server | Commit locally, then share via push/pull |

Git is distributed: when you `git clone`, you get the whole project history, not just the latest snapshot. Your laptop is a full-fledged repository. The "central" server (GitHub, GitLab, Bitbucket, Gitea) is central only by convention.

### 1.1.2 Snapshots, not diffs

Many older systems store a file and then a sequence of deltas. Git conceptually stores a **snapshot** of the entire project at each commit. If a file did not change, the new snapshot simply points to the same stored content as before. This design is why branching, switching and merging are fast: Git compares snapshots rather than replaying chains of patches.

> **Note:** Internally Git does compress similar objects into deltas inside packfiles (Part 5), but that is a storage optimization. The data model you reason about is always "each commit is a full snapshot".

### 1.1.3 Integrity by hashing

Every object Git stores is named by a cryptographic hash of its contents (SHA-1 by default, SHA-256 optionally). A commit's hash covers its tree, its parents, its author and its message. Change a single byte anywhere in history and every descendant hash changes. This gives you tamper evidence for free and is why "rewriting history" is a real, visible event in Git.

## 1.2 Installing Git

Check what you have first:

```bash
git --version
```

```text
git version 2.47.1
```

| Platform | Recommended install | Notes |
|---|---|---|
| macOS | `brew install git` | Apple's Xcode Command Line Tools ship an older Git; Homebrew keeps you current |
| Debian/Ubuntu | `sudo apt install git` | For the newest release use `sudo add-apt-repository ppa:git-core/ppa` |
| RHEL/Fedora | `sudo dnf install git` | |
| Alpine (containers) | `apk add --no-cache git` | Common in CI images |
| Windows | Git for Windows (winget install Git.Git) | Includes Git Bash and Git Credential Manager |

> **Tip:** In CI images and Dockerfiles, pin Git through the base image rather than installing "latest" at build time. Reproducible builds matter more than shiny features.

## 1.3 Configuring Git

Git configuration lives at three levels. Later levels override earlier ones.

| Level | Flag | File | Scope |
|---|---|---|---|
| System | `--system` | `/etc/gitconfig` (or `$(prefix)/etc/gitconfig`) | Every user on the machine |
| Global | `--global` | `~/.gitconfig` or `~/.config/git/config` | Your user account |
| Local | `--local` (default) | `.git/config` in the repo | One repository |
| Worktree | `--worktree` | `.git/config.worktree` | One worktree (needs `extensions.worktreeConfig`) |

To see every value and where it comes from:

```bash
git config --list --show-origin
```

```text
file:/opt/homebrew/etc/gitconfig    credential.helper=osxkeychain
file:/Users/dev/.gitconfig          user.name=Dana Ops
file:/Users/dev/.gitconfig          user.email=dana@example.com
file:.git/config                    core.repositoryformatversion=0
file:.git/config                    remote.origin.url=git@github.com:acme/infra.git
```

### 1.3.1 Identity

Every commit embeds an author name and email. Set them once:

```bash
git config --global user.name "Dana Ops"
git config --global user.email "dana@example.com"
```

Platforms use the email to link commits to your account. If you contribute to work and personal projects from one laptop, use **conditional includes** so the right identity is picked automatically by directory:

```ini
# ~/.gitconfig
[user]
    name = Dana Ops
    email = dana@personal.example
[includeIf "gitdir:~/work/"]
    path = ~/.gitconfig-work
```

```ini
# ~/.gitconfig-work
[user]
    email = dana.ops@acme.example
    signingkey = ~/.ssh/id_ed25519_work.pub
```

> **Warning:** The trailing slash in `gitdir:~/work/` matters. It means "any repository under this directory". Without it, the pattern matches only that exact path.

### 1.3.2 Editor, default branch and sane defaults

```bash
git config --global core.editor "vim"            # or "code --wait", "nano"
git config --global init.defaultBranch main
git config --global pull.rebase true             # rebase local commits on pull instead of merge bubbles
git config --global rebase.autoStash true        # stash dirty changes around a rebase
git config --global fetch.prune true             # delete remote-tracking branches that vanished on the server
git config --global push.autoSetupRemote true    # first push creates upstream automatically
git config --global diff.algorithm histogram     # usually more readable diffs
git config --global merge.conflictStyle zdiff3   # show the common ancestor in conflicts
git config --global rerere.enabled true          # remember conflict resolutions
git config --global column.ui auto
git config --global branch.sort -committerdate   # most recent branches first
```

Why each one matters:

- `pull.rebase true` keeps your local history linear when you sync with the remote, avoiding meaningless "Merge branch 'main' of github.com:..." commits.
- `fetch.prune true` stops your `git branch -r` output from filling with branches deleted months ago.
- `push.autoSetupRemote true` (Git 2.37+) removes the classic "fatal: The current branch has no upstream branch" message on the first push.
- `merge.conflictStyle zdiff3` (Git 2.35+) adds the base version to conflict markers, which makes resolution far less guesswork (Part 3).
- `rerere` ("reuse recorded resolution") records how you resolved a conflict and replays it automatically if the same conflict reappears, which is common during long rebases.

### 1.3.3 Line endings

Mixed Windows and Unix teams fight over CRLF vs LF. Shell scripts with CRLF fail with `/bin/bash^M: bad interpreter`. The robust fix is a committed `.gitattributes` file rather than per-user settings:

```text
# .gitattributes
* text=auto eol=lf
*.ps1 text eol=crlf
*.png binary
*.jar binary
```

On Windows machines, `git config --global core.autocrlf true` converts on checkout; on macOS and Linux use `input` or leave it unset and rely on `.gitattributes`.

### 1.3.4 Aliases

Aliases save keystrokes and encode team conventions.

```bash
git config --global alias.st "status -sb"
git config --global alias.co switch
git config --global alias.br "branch -vv"
git config --global alias.last "log -1 HEAD --stat"
git config --global alias.lg "log --graph --oneline --decorate --all"
git config --global alias.unstage "restore --staged"
git config --global alias.amend "commit --amend --no-edit"
git config --global alias.undo "reset --soft HEAD~1"
```

An alias starting with `!` runs a shell command, which lets you build small tools:

```bash
git config --global alias.cleanup '!git branch --merged main | grep -vE "^\*|main" | xargs -r git branch -d'
```

> **Note:** `xargs -r` is a GNU extension. On macOS BSD xargs it is accepted and ignored in recent versions, but test before sharing aliases across platforms.

### 1.3.5 Global gitignore

Some files should never be committed in any repository: editor swap files, OS metadata, local env files. Put them in a global excludes file instead of polluting every project's `.gitignore`:

```bash
git config --global core.excludesFile ~/.config/git/ignore
```

```text
# ~/.config/git/ignore
.DS_Store
Thumbs.db
*.swp
.idea/
.vscode/
*.log
.env
.direnv/
```

Project-specific rules (build outputs, `node_modules/`, `.terraform/`) still belong in the project's own `.gitignore` so every contributor gets them.

### 1.3.6 Authentication: SSH and credential helpers

Remotes are reached over HTTPS or SSH.

**SSH** is the usual choice for engineers:

```bash
ssh-keygen -t ed25519 -C "dana@example.com"
cat ~/.ssh/id_ed25519.pub      # paste into GitHub/GitLab settings
ssh -T git@github.com
```

```text
Hi dana-ops! You've successfully authenticated, but GitHub does not provide shell access.
```

**HTTPS** needs a token (passwords are no longer accepted by the major platforms). A credential helper stores it so you are not prompted every time:

| Helper | Platform | Storage |
|---|---|---|
| `osxkeychain` | macOS | Keychain (encrypted) |
| `manager` (Git Credential Manager) | Windows, macOS, Linux | OS secure store, supports OAuth and MFA |
| `libsecret` | Linux desktop | GNOME Keyring / KWallet |
| `cache --timeout=3600` | Any | In memory, expires |
| `store` | Any | **Plain text** in `~/.git-credentials` |

```bash
git config --global credential.helper osxkeychain
```

> **Warning:** Avoid `credential.helper store` on shared machines and CI runners. It writes your token unencrypted to disk. In CI, inject short-lived tokens through environment variables or the platform's built-in token instead.

## 1.4 The Three Trees

The single most important mental model in Git is that your files live in three places at once.

```text
  Working Directory          Index (Staging Area)          HEAD (last commit)
  -----------------          --------------------          ------------------
  files you edit     --add-->  proposed next commit  --commit-->  committed snapshot
                     <-restore--                     <-restore --staged--
```

| Tree | What it is | Stored in |
|---|---|---|
| Working directory | Real files on disk you open in an editor | Your project folder |
| Index (staging area) | A binary file listing exactly what the next commit will contain | `.git/index` |
| HEAD | A pointer to the current branch, which points to the latest commit | `.git/HEAD` and `.git/refs/heads/*` |

### 1.4.1 Why a staging area?

Beginners often ask why Git doesn't just commit everything that changed. The index exists so you can craft commits deliberately. Suppose you fixed a bug and, while there, reformatted an unrelated file. With staging you can commit the bug fix alone, with a clear message, and the formatting separately. That makes the history reviewable, revertable and bisectable.

### 1.4.2 Watching the trees move

```bash
echo "v1" > app.conf
git add app.conf          # working dir -> index
git commit -m "Add app.conf"   # index -> new commit, HEAD moves
echo "v2" > app.conf      # working dir differs from index
git status -s
```

```text
 M app.conf
```

The two-column short status tells you which tree differs:

| Column 1 (index vs HEAD) | Column 2 (working dir vs index) | Meaning |
|---|---|---|
| ` ` | `M` | Modified, not staged |
| `M` | ` ` | Modified and staged |
| `M` | `M` | Staged, then modified again |
| `A` | ` ` | New file staged |
| `?` | `?` | Untracked |
| `D` | ` ` | Deletion staged |

```bash
git add app.conf
echo "v3" > app.conf
git status -s
```

```text
MM app.conf
```

Now the index holds `v2`, the working directory holds `v3`, and HEAD holds `v1`. Three different versions of one file, which is exactly what the "three trees" mean. `git diff` shows working dir vs index (v2 to v3); `git diff --staged` shows index vs HEAD (v1 to v2); `git diff HEAD` shows working dir vs HEAD (v1 to v3).

### 1.4.3 The commands that move content between trees

| Command | Effect |
|---|---|
| `git add <file>` | Copy working dir version into the index |
| `git restore <file>` | Copy index version over working dir (discard unstaged edits) |
| `git restore --staged <file>` | Copy HEAD version into the index (unstage) |
| `git restore --source=HEAD~2 <file>` | Copy a file from any commit into the working dir |
| `git commit` | Turn the index into a new commit and advance the branch |
| `git reset --soft/--mixed/--hard <commit>` | Move the branch pointer and optionally reset index and working dir (Part 4) |

> **Interview:** "What is the difference between `git diff` and `git diff --staged`?" The first compares the working directory to the index (what you have not staged yet). The second compares the index to HEAD (what will go into the next commit).

## 1.5 What Lives in .git

Run `git init` and look inside:

```bash
mkdir demo && cd demo && git init
ls -1 .git
```

```text
HEAD
config
description
hooks
info
objects
refs
```

| Path | Purpose |
|---|---|
| `HEAD` | Which branch you are on, e.g. `ref: refs/heads/main` |
| `config` | Repository-local configuration |
| `objects/` | The content-addressed database: blobs, trees, commits, tags |
| `refs/heads/` | Local branches (each a file containing a commit hash) |
| `refs/tags/` | Tags |
| `refs/remotes/` | Remote-tracking branches like `origin/main` |
| `hooks/` | Sample client-side hook scripts (Part 6) |
| `index` | The staging area (appears after first `git add`) |
| `logs/` | Reflogs: history of where each ref pointed (Part 4) |

Deleting `.git` deletes the repository; the remaining files become ordinary files. Copying the project folder including `.git` copies the whole repository.

## 1.6 Lab: Set Up a Professional Git Environment

**Goal:** configure Git the way you would on a new work laptop and observe the three trees.

1. Verify your version is 2.40 or newer:

   ```bash
   git --version
   ```

2. Set identity, editor and defaults:

   ```bash
   git config --global user.name "Your Name"
   git config --global user.email "you@example.com"
   git config --global init.defaultBranch main
   git config --global core.editor "code --wait"
   git config --global pull.rebase true
   git config --global fetch.prune true
   git config --global push.autoSetupRemote true
   git config --global merge.conflictStyle zdiff3
   git config --global rerere.enabled true
   ```

3. Create a global ignore file and register it:

   ```bash
   mkdir -p ~/.config/git
   printf ".DS_Store\n*.swp\n.env\n.idea/\n.vscode/\n" > ~/.config/git/ignore
   git config --global core.excludesFile ~/.config/git/ignore
   ```

4. Add the `lg` and `st` aliases from section 1.3.4.

5. Create an SSH key, add it to your Git hosting account and test with `ssh -T git@github.com` (or `git@gitlab.com`).

6. Create a sandbox repository and walk the three trees:

   ```bash
   mkdir -p /tmp/three-trees && cd /tmp/three-trees
   git init
   echo "v1" > app.conf && git add app.conf && git commit -m "Add app.conf"
   echo "v2" > app.conf && git add app.conf
   echo "v3" > app.conf
   git status -s
   git diff
   git diff --staged
   git diff HEAD
   ```

   Confirm you can explain each diff output.

7. Unstage and discard:

   ```bash
   git restore --staged app.conf   # index back to v1
   git restore app.conf            # working dir back to v1
   cat app.conf
   ```

8. Run `git config --list --show-origin` and verify every setting comes from where you expect.

## 1.7 Key Takeaways

- Git is distributed: every clone holds full history, and the server is central only by agreement.
- Git models each commit as a full snapshot, identified by a content hash that also protects integrity.
- Configuration has system, global and local levels; use `includeIf` to keep work and personal identities separate.
- A handful of global settings (`pull.rebase`, `fetch.prune`, `push.autoSetupRemote`, `zdiff3`, `rerere`) prevent common daily friction.
- Use a global excludes file for personal noise and `.gitattributes` for line-ending policy.
- Prefer SSH keys or a secure credential helper; never store tokens in plain text on shared machines.
- Master the three trees: working directory, index, HEAD. Almost every Git command is a way of moving content between them.
