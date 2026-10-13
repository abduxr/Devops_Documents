# Part 2 — Everyday Git: Recording and Inspecting Changes

This Part covers the commands you will type dozens of times a day. The goal is not just to know them but to use them precisely: staging exactly the right lines, writing commits a reviewer can understand in ten seconds, and digging through history quickly when something breaks.

## 2.1 Creating and Obtaining Repositories

### 2.1.1 git init

```bash
mkdir platform-infra && cd platform-infra
git init
```

```text
Initialized empty Git repository in /home/dana/platform-infra/.git/
```

Useful variants:

```bash
git init -b main                 # set the initial branch explicitly
git init --bare infra.git        # a server-side repository with no working directory
git init --object-format=sha256  # experimental SHA-256 repository
```

A **bare** repository has no working directory; it contains only what would normally be inside `.git`. Servers host bare repositories because nobody edits files there, and pushing into a non-bare repository's checked-out branch would leave its working directory out of sync.

### 2.1.2 git clone

```bash
git clone git@github.com:acme/platform-infra.git
git clone https://github.com/acme/platform-infra.git infra   # into directory "infra"
```

```text
Cloning into 'platform-infra'...
remote: Enumerating objects: 4821, done.
remote: Counting objects: 100% (812/812), done.
remote: Compressing objects: 100% (301/301), done.
remote: Total 4821 (delta 560), reused 642 (delta 487), pack-reused 4009
Receiving objects: 100% (4821/4821), 1.92 MiB | 6.10 MiB/s, done.
Resolving deltas: 100% (2907/2907), done.
```

Clone does several things: creates the directory, initializes `.git`, adds a remote called `origin`, fetches all objects, creates remote-tracking branches (`origin/main`, `origin/feature-x`...), and checks out the default branch as a local branch tracking `origin/main`.

| Clone option | Use case |
|---|---|
| `--depth 1` | Shallow clone, only latest commit; fast CI checkouts |
| `--branch v2.3.0` / `-b` | Check out a specific branch or tag |
| `--single-branch` | Fetch only one branch's history |
| `--filter=blob:none` | Partial clone: download file contents on demand (Part 5) |
| `--recurse-submodules` | Also clone submodules |
| `--mirror` | Exact copy of all refs; used for migrations and backups |

## 2.2 Checking State: git status

```bash
git status
```

```text
On branch main
Your branch is ahead of 'origin/main' by 1 commit.
  (use "git push" to publish your local commits)

Changes to be committed:
  (use "git restore --staged <file>..." to unstage)
	modified:   terraform/vpc.tf

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
	modified:   terraform/variables.tf

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	terraform/outputs.tf
```

Read it top to bottom: which branch, how it relates to its upstream, what is staged, what is modified but unstaged, and what Git does not track yet. The hints in parentheses are accurate and worth reading.

For a compact view, `git status -sb` (short + branch):

```text
## main...origin/main [ahead 1]
M  terraform/vpc.tf
 M terraform/variables.tf
?? terraform/outputs.tf
```

## 2.3 Staging: git add

| Command | Stages |
|---|---|
| `git add file.tf` | One file |
| `git add terraform/` | A directory recursively |
| `git add -A` | All changes everywhere in the repo (new, modified, deleted) |
| `git add .` | All changes in the current directory and below |
| `git add -u` | Modified and deleted tracked files only, no new files |
| `git add -p` | Interactively choose hunks |
| `git add -N file` | "Intent to add": track a new file without staging content, so it shows in `git diff` |

### 2.3.1 Patch mode: the professional's add

`git add -p` walks through each changed hunk and asks what to do:

```text
diff --git a/terraform/vpc.tf b/terraform/vpc.tf
@@ -12,7 +12,7 @@ resource "aws_vpc" "main" {
-  cidr_block = "10.0.0.0/16"
+  cidr_block = var.vpc_cidr
(1/2) Stage this hunk [y,n,q,a,d,s,e,?]?
```

| Key | Action |
|---|---|
| `y` / `n` | Stage / skip this hunk |
| `s` | Split into smaller hunks |
| `e` | Edit the hunk manually |
| `q` | Quit |
| `a` / `d` | Stage / skip this and all remaining hunks in the file |

Patch mode is how you turn a messy afternoon of edits into three clean, focused commits. It also forces you to review every line before it is committed, which catches stray debug prints and accidentally pasted credentials.

> **Tip:** The same `-p` flag works with `git restore -p`, `git stash -p` and `git reset -p`.

## 2.4 Committing

```bash
git commit                       # opens editor for a message
git commit -m "fix(vpc): parameterize CIDR block"
git commit -am "docs: update runbook"   # stage tracked modified files and commit (skips new files)
git commit --amend               # replace the last commit (message and/or content)
git commit --amend --no-edit     # add staged changes to last commit, keep message
git commit --fixup=<sha>         # mark a commit to be squashed into <sha> later (Part 4)
git commit -S                    # sign the commit (Part 6)
git commit -v                    # show the diff in the editor while writing the message
```

> **Warning:** `--amend` creates a new commit with a new hash. If you have already pushed the original, amending rewrites shared history and you will need a force push. Amend freely on local, unpushed work.

### 2.4.1 Writing good commit messages

A commit message is documentation that is permanently attached to the exact change it describes. Six months from now, `git blame` will lead someone to your commit, and the message is all they will have.

The widely accepted format:

```text
Short summary in imperative mood, about 50 chars

Body wrapped at 72 characters explaining WHY the change was made,
what problem it solves, and any trade-offs or side effects. The
diff already shows WHAT changed; the message must explain why.

Refs: OPS-1423
```

Rules of thumb:

1. Subject in the imperative: "Add", "Fix", "Remove", not "Added" or "Fixes". It should complete the sentence "If applied, this commit will ...".
2. Keep the subject around 50 characters, hard limit about 72, no trailing period.
3. Blank line between subject and body; many tools depend on it.
4. Explain the motivation, the alternatives rejected, and the impact.
5. One logical change per commit. If the message needs "and", consider splitting.

### 2.4.2 Conventional Commits

Conventional Commits is a lightweight specification that makes commit messages machine-readable. It powers automated changelogs and semantic version bumps (Part 4).

```text
<type>(<optional scope>): <description>

[optional body]

[optional footer(s)]
```

| Type | Meaning | SemVer effect (typical) |
|---|---|---|
| `feat` | New feature | Minor |
| `fix` | Bug fix | Patch |
| `docs` | Documentation only | None |
| `refactor` | Code change that neither fixes a bug nor adds a feature | None |
| `perf` | Performance improvement | Patch |
| `test` | Adding or fixing tests | None |
| `build` | Build system or dependencies | None |
| `ci` | CI configuration | None |
| `chore` | Maintenance tasks | None |
| `revert` | Reverts a previous commit | Depends |

A breaking change is marked with `!` after the type or a `BREAKING CHANGE:` footer, and triggers a major bump:

```text
feat(api)!: drop support for v1 auth tokens

BREAKING CHANGE: clients must use OAuth2 bearer tokens. The legacy
X-Auth-Token header is no longer accepted.
```

Examples from a DevOps repository:

```text
feat(helm): add PodDisruptionBudget to ingress chart
fix(terraform): pin aws provider to ~> 5.40 to avoid S3 ACL regression
ci: cache Go modules between pipeline runs
chore(deps): bump alpine base image to 3.20
```

> **Tip:** Enforce the format with a `commit-msg` hook (Part 6) using a tool like commitlint, and enforce it again in CI on the pull request title if you squash-merge.

### 2.4.3 Atomic commits

An atomic commit contains one complete, self-consistent change: the code, its tests and its docs together, and nothing unrelated. Atomic commits pay off later:

- `git revert` of one commit removes one feature or fix cleanly.
- `git bisect` (Part 5) pinpoints a single culprit instead of a 2,000-line grab bag.
- `git cherry-pick` of a fix to a release branch does not drag in unrelated work.
- Reviewers can reason about the change.

## 2.5 Ignoring Files: .gitignore

`.gitignore` tells Git which untracked files to leave alone. It does not affect files that are already tracked.

```text
# .gitignore for an infrastructure repo

# Terraform
.terraform/
*.tfstate
*.tfstate.*
crash.log
*.tfvars
!example.tfvars

# Python
__pycache__/
*.pyc
.venv/

# Node
node_modules/
dist/

# Secrets and local config
.env
.env.*
!.env.example
*.pem
*.key

# Kubernetes local
kubeconfig*
```

### 2.5.1 Pattern syntax

| Pattern | Matches |
|---|---|
| `*.log` | Any `.log` file in any directory |
| `/build` | `build` only at the repo root |
| `build/` | Any directory named `build` (not a file) |
| `docs/**/*.pdf` | PDFs at any depth under `docs/` |
| `!keep.log` | Re-include a file excluded by an earlier pattern |
| `#` | Comment |

> **Warning:** You cannot re-include a file if its parent directory is excluded. `logs/` followed by `!logs/keep.txt` does not work; use `logs/*` then `!logs/keep.txt`.

### 2.5.2 Where ignore rules live

| Location | Shared with team? | Use for |
|---|---|---|
| `.gitignore` (any directory) | Yes, committed | Project build outputs, dependencies |
| `.git/info/exclude` | No | Personal, repo-specific ignores |
| `core.excludesFile` (global) | No | Editor and OS files everywhere |

### 2.5.3 Debugging and fixing ignore rules

Why is a file ignored?

```bash
git check-ignore -v terraform/prod.tfvars
```

```text
.gitignore:7:*.tfvars	terraform/prod.tfvars
```

Stop tracking a file that was committed before the ignore rule existed:

```bash
git rm --cached terraform/terraform.tfstate
git commit -m "chore: stop tracking terraform state"
```

`--cached` removes it from the index but leaves the file on disk. Note that the file is still present in history; if it held secrets, see Part 6.

## 2.6 Inspecting Changes: git diff

| Command | Compares |
|---|---|
| `git diff` | Working dir vs index |
| `git diff --staged` (alias `--cached`) | Index vs HEAD |
| `git diff HEAD` | Working dir vs HEAD |
| `git diff main feature` | Tip of main vs tip of feature |
| `git diff main...feature` | Merge base of the two vs feature (what the PR introduces) |
| `git diff HEAD~3 -- path/` | Limit to a path |
| `git diff --stat` | Summary of files and line counts |
| `git diff --name-only` | Just file names (great in CI scripts) |
| `git diff --word-diff` | Word-level changes, useful for docs and YAML |
| `git diff --color-moved` | Highlight moved blocks differently from edits |

```bash
git diff --stat main...feature/pdb
```

```text
 charts/ingress/templates/pdb.yaml | 14 ++++++++++++++
 charts/ingress/values.yaml        |  4 ++++
 2 files changed, 18 insertions(+)
```

> **Interview:** "What's the difference between `git diff A..B` and `git diff A...B`?" Two dots compares the two tips directly. Three dots compares the merge base of A and B against B, i.e. only the changes made on B since it diverged. Three dots is what a pull request shows.

In CI, `git diff --name-only` drives selective builds:

```bash
CHANGED=$(git diff --name-only origin/main...HEAD)
if echo "$CHANGED" | grep -q '^terraform/'; then
  terraform -chdir=terraform plan
fi
```

## 2.7 Reading History: git log

### 2.7.1 Formats

```bash
git log --oneline --graph --decorate -n 10
```

```text
* 9f2c1ab (HEAD -> main, origin/main) feat(helm): add PDB to ingress chart
*   4e7d0c2 Merge pull request #88 from acme/fix/tf-provider
|\
| * b13a9e5 fix(terraform): pin aws provider to ~> 5.40
|/
* 70aa3d1 ci: cache Go modules
* 1c5e8f0 docs: add on-call runbook
```

```bash
git log --pretty=format:"%h %an %ar %s" -n 3
```

```text
9f2c1ab Dana Ops 2 hours ago feat(helm): add PDB to ingress chart
4e7d0c2 Lee Chen 1 day ago Merge pull request #88 from acme/fix/tf-provider
b13a9e5 Lee Chen 1 day ago fix(terraform): pin aws provider to ~> 5.40
```

Common placeholders: `%H` full hash, `%h` short hash, `%an` author name, `%ae` author email, `%ad` author date, `%ar` relative date, `%cn` committer name, `%s` subject, `%b` body, `%d` ref names.

### 2.7.2 Filtering

| Command | Shows |
|---|---|
| `git log -n 5` | Last 5 commits |
| `git log --since="2 weeks ago" --until=yesterday` | Date range |
| `git log --author="Lee"` | By author (regex) |
| `git log --grep="OPS-1423"` | Commit message contains text |
| `git log -S "db_password"` | "Pickaxe": commits that add or remove that string |
| `git log -G "timeout\s*=\s*\d+"` | Commits whose diff matches a regex |
| `git log -- terraform/vpc.tf` | Commits touching a path |
| `git log --follow -- old/name.tf` | Follow a file across renames |
| `git log -p -- file` | Include the patch |
| `git log --stat` | Files changed per commit |
| `git log main..feature` | Commits on feature not yet in main |
| `git log --merges` / `--no-merges` | Only / exclude merge commits |
| `git log --first-parent main` | Mainline history only (one entry per merged PR) |
| `git log -L 20,40:app.py` | History of a line range |
| `git log -L :handler:app.py` | History of a function |

The pickaxe (`-S`) deserves special mention for DevOps. "When was this config key introduced?" and "Did anyone ever commit an AWS key?" are both one command:

```bash
git log -S "AKIA" --all --oneline
```

### 2.7.3 Revision syntax

Git lets you name commits in many ways. Learn these and every command becomes more powerful.

| Syntax | Meaning |
|---|---|
| `HEAD` | Current commit |
| `HEAD~1`, `HEAD~3` | First parent, three generations back |
| `HEAD^2` | Second parent of a merge commit |
| `main@{1}` | Where main pointed one move ago (reflog) |
| `main@{yesterday}` | Where main pointed yesterday (local reflog) |
| `@{u}` / `@{upstream}` | The upstream of the current branch |
| `v1.2.0^{commit}` | The commit a tag points to |
| `:/fix login` | Youngest commit whose message matches "fix login" |
| `A..B` | Commits reachable from B but not A |
| `A...B` | Commits reachable from either but not both (symmetric difference) |

```text
         HEAD~2  HEAD~1  HEAD
   o-------o-------o-------o   main
                    \
                     o--o      feature  (main..feature = these two)
```

### 2.7.4 git show

`git show` displays one object, typically a commit with its diff:

```bash
git show 9f2c1ab
git show HEAD~2:terraform/vpc.tf     # file content at that commit
git show v1.4.0 --stat
```

### 2.7.5 git shortlog

```bash
git shortlog -sn --since="3 months ago"
```

```text
    42  Dana Ops
    31  Lee Chen
     9  dependabot[bot]
```

Good for release notes and understanding who knows a codebase.

## 2.8 Moving and Removing Files

```bash
git mv old.tf new.tf     # rename and stage
git rm obsolete.tf       # delete and stage
git rm --cached secret.env   # untrack but keep on disk
git rm -r --cached .terraform/
```

Git does not store renames explicitly. It detects them by comparing content similarity at diff time, which is why `git log --follow` and `git diff -M` work even if you used plain `mv` followed by `git add -A`.

## 2.9 Discarding and Undoing Small Things

| Situation | Command |
|---|---|
| Throw away unstaged edits to a file | `git restore file` |
| Unstage a file but keep edits | `git restore --staged file` |
| Unstage and discard | `git restore --staged --worktree file` |
| Get a file back as it was 3 commits ago | `git restore --source=HEAD~3 file` |
| Fix the last commit message | `git commit --amend` |
| Forgot to add a file to last commit | `git add file && git commit --amend --no-edit` |
| Delete untracked files (preview first) | `git clean -n` then `git clean -fd` |
| Also delete ignored files | `git clean -fdx` |

> **Warning:** `git restore file` and `git clean -f` destroy uncommitted work that Git has never stored. There is no reflog for it. Preview with `git clean -n` and consider `git stash -u` if unsure.

## 2.10 Lab: Build a Clean History

**Goal:** practise patch staging, Conventional Commits, ignore rules and history search.

1. Create a repository:

   ```bash
   mkdir -p /tmp/lab2 && cd /tmp/lab2 && git init
   ```

2. Add a Terraform-like file and an ignore file:

   ```bash
   cat > main.tf <<'EOF'
   resource "aws_vpc" "main" {
     cidr_block = "10.0.0.0/16"
   }
   EOF
   printf ".terraform/\n*.tfstate\n*.tfvars\n!example.tfvars\n" > .gitignore
   git add .gitignore main.tf
   git commit -m "feat(vpc): add base VPC"
   ```

3. Make two unrelated edits in one file: change the CIDR to `var.vpc_cidr`, and add a comment block at the bottom. Then stage only the CIDR change:

   ```bash
   git add -p main.tf      # answer y for the CIDR hunk, n for the comment hunk
   git diff --staged
   git commit -m "refactor(vpc): parameterize CIDR block"
   git add main.tf
   git commit -m "docs(vpc): explain VPC sizing"
   ```

   If both edits end up in one hunk, use `s` to split or `e` to edit.

4. Prove the ignore rules work:

   ```bash
   touch prod.tfvars example.tfvars terraform.tfstate
   git status -s
   git check-ignore -v prod.tfvars
   ```

   Only `example.tfvars` should appear as untracked.

5. Simulate an accidental commit and untrack it:

   ```bash
   git add -f terraform.tfstate && git commit -m "oops: add state"
   git rm --cached terraform.tfstate
   git commit -m "chore: stop tracking terraform state"
   ```

6. Search history:

   ```bash
   git log --oneline --graph
   git log -S "vpc_cidr" --oneline
   git log --stat -- terraform.tfstate
   git show HEAD~2:main.tf
   ```

7. Write a commit with a body explaining why, using `git commit -v`, and view it with `git log -1`.

## 2.11 Key Takeaways

- `git clone` creates `origin`, remote-tracking branches and a local tracking branch; options like `--depth` and `--filter` control how much you download.
- Read `git status` carefully; `git status -sb` gives a dense summary.
- `git add -p` is the tool for building focused, reviewed commits.
- Write commit messages in the imperative, explain why, and adopt Conventional Commits to enable automation.
- `.gitignore` only affects untracked files; use `git rm --cached` to untrack and `git check-ignore -v` to debug.
- `git diff A...B` shows what a branch introduces; `git diff --name-only` drives selective CI.
- `git log` with `-S`, `--grep`, `--follow`, `-L` and revision ranges answers almost any "when and why did this change" question.
- `git restore` and `git clean` destroy uncommitted work irreversibly; preview first.
