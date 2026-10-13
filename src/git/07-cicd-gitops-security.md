# Part 7 — Git in CI/CD, GitOps and Secrets Security

For a DevOps engineer, Git is not just where code lives; it is the event source for automation. A push builds, a pull request tests and plans, a tag releases, and a merged commit in a config repository changes production. This Part covers how pipelines are triggered by Git events, how CI checks out code efficiently, how GitOps controllers reconcile clusters from Git, and how to prevent and clean up leaked secrets.

## 7.1 Git Events as Pipeline Triggers

| Git event | Typical pipeline |
|---|---|
| Push to feature branch | Lint, unit tests, build |
| Pull/merge request opened or updated | Full tests, security scans, `terraform plan`, preview environment |
| Merge to `main` | Build artifact, deploy to staging, publish `latest` |
| Push tag `v*.*.*` | Build release artifacts, sign, publish, deploy to production |
| Push to `release/*` | Build release candidates, run long regression suites |
| Changes under a path | Build only the affected service (monorepo) |
| Schedule (cron) | Nightly scans, dependency updates |

The principle: **the commit is the unit of deployment, and the tag is the unit of release.** Every artifact should be traceable to the exact commit hash it was built from.

## 7.2 GitHub Actions

### 7.2.1 Triggers by branch, tag and path

```yaml
# .github/workflows/ci.yml
name: ci

on:
  push:
    branches: [main, "release/**"]
    tags: ["v*.*.*"]
    paths-ignore: ["docs/**", "**.md"]
  pull_request:
    branches: [main]
    paths: ["services/api/**", ".github/workflows/ci.yml"]
  workflow_dispatch: {}

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make test

  release:
    if: startsWith(github.ref, 'refs/tags/v')
    needs: test
    runs-on: ubuntu-latest
    permissions:
      contents: write
      packages: write
      id-token: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # full history so changelog and describe work
      - name: Build image
        run: |
          VERSION="${GITHUB_REF_NAME#v}"
          docker build -t ghcr.io/acme/api:"$VERSION" \
            --label org.opencontainers.image.revision="$GITHUB_SHA" .
```

Key context values:

| Variable | Example | Meaning |
|---|---|---|
| `GITHUB_SHA` | `9f2c1ab...` | Commit that triggered the run |
| `GITHUB_REF` | `refs/heads/main`, `refs/tags/v1.4.0`, `refs/pull/88/merge` | Full ref |
| `GITHUB_REF_NAME` | `main`, `v1.4.0`, `88/merge` | Short name |
| `GITHUB_HEAD_REF` | `feature/pdb` | PR source branch (PR events only) |
| `GITHUB_BASE_REF` | `main` | PR target branch |

> **Note:** For `pull_request` events, `actions/checkout` checks out a **merge commit** (`refs/pull/N/merge`) of the PR into the base branch, not the PR head. CI therefore tests what main would look like after merging. `GITHUB_SHA` is that merge commit; the PR head is `github.event.pull_request.head.sha`.

> **Warning:** `pull_request_target` runs with the base repository's secrets and write token. Never check out and execute code from a fork's PR in a `pull_request_target` workflow; that is a well-known path to secret theft.

### 7.2.2 Shallow clones in CI

`actions/checkout` defaults to `fetch-depth: 1`: only the triggering commit, no history. This is fast, but breaks anything that needs history.

| Need | Setting |
|---|---|
| Build and test only | `fetch-depth: 1` (default) |
| `git describe`, changelog, semantic-release | `fetch-depth: 0` (full history and tags) |
| Diff against the PR base | `fetch-depth: 0`, or fetch the base explicitly |
| Large repo, need history but not file contents | `filter: blob:none` with `fetch-depth: 0` |
| Monorepo, only some directories | `sparse-checkout: services/api` |
| Tags only | `fetch-tags: true` |

Computing changed files in a PR with a shallow clone:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
    filter: blob:none
- name: Changed files
  run: git diff --name-only "origin/${GITHUB_BASE_REF}...HEAD"
```

Common shallow-clone errors and causes:

```text
fatal: No names found, cannot describe anything.
```

No tags were fetched: use `fetch-depth: 0` or `git fetch --tags --unshallow`.

```text
fatal: origin/main...HEAD: no merge base
```

History too shallow to find the merge base: deepen with `git fetch --deepen=100 origin main` or fetch full history.

### 7.2.3 Pushing back from CI

Workflows that bump versions or update manifests need to commit:

```yaml
- name: Commit image tag bump
  run: |
    git config user.name  "github-actions[bot]"
    git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
    yq -i '.image.tag = "${{ github.sha }}"' deploy/api/values.yaml
    git commit -am "chore(deploy): api ${{ github.sha }}"
    git push
```

Pushes made with the default `GITHUB_TOKEN` do not trigger further workflows (to avoid infinite loops). Use a GitHub App token when a downstream workflow must run, and add `[skip ci]` to the message when it must not.

## 7.3 GitLab CI

### 7.3.1 Rules by branch, tag, MR and path

```yaml
# .gitlab-ci.yml
workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH && $CI_OPEN_MERGE_REQUESTS
      when: never                     # avoid duplicate branch + MR pipelines
    - if: $CI_COMMIT_BRANCH
    - if: $CI_COMMIT_TAG

variables:
  GIT_DEPTH: "20"
  GIT_STRATEGY: fetch

stages: [test, build, release]

test:
  stage: test
  script: make test
  rules:
    - changes:
        - services/api/**/*
        - .gitlab-ci.yml

plan:
  stage: test
  script: terraform -chdir=terraform plan -out=plan.tfplan
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      changes: [terraform/**/*]

build:
  stage: build
  script:
    - docker build -t "$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA" .
    - docker push "$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA"
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH

release:
  stage: release
  variables:
    GIT_DEPTH: "0"                     # full history for changelog
  script:
    - docker build -t "$CI_REGISTRY_IMAGE:$CI_COMMIT_TAG" .
    - docker push "$CI_REGISTRY_IMAGE:$CI_COMMIT_TAG"
  rules:
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+$/
```

| Variable | Meaning |
|---|---|
| `CI_COMMIT_SHA` / `CI_COMMIT_SHORT_SHA` | Commit being built |
| `CI_COMMIT_BRANCH` | Branch name (not set for tag or MR pipelines) |
| `CI_COMMIT_TAG` | Tag name (only for tag pipelines) |
| `CI_DEFAULT_BRANCH` | Usually `main` |
| `CI_PIPELINE_SOURCE` | `push`, `merge_request_event`, `schedule`, `web`... |
| `CI_MERGE_REQUEST_TARGET_BRANCH_NAME` | MR target |
| `CI_MERGE_REQUEST_DIFF_BASE_SHA` | Merge base, useful for diffs |

### 7.3.2 Git strategy and depth

| Setting | Effect |
|---|---|
| `GIT_STRATEGY: fetch` | Reuse the runner's existing working copy (faster) |
| `GIT_STRATEGY: clone` | Fresh clone every job (clean, slower) |
| `GIT_STRATEGY: none` | No checkout (jobs that only use artifacts) |
| `GIT_DEPTH: "20"` | Shallow clone depth (project default is often 20 or 50) |
| `GIT_DEPTH: "0"` | Full history |
| `GIT_SUBMODULE_STRATEGY: recursive` | Check out submodules |
| `GIT_CLEAN_FLAGS: -ffdx` | How the working copy is cleaned with `fetch` |
| `GIT_LFS_SKIP_SMUDGE: "1"` | Skip downloading LFS objects |

## 7.4 CI Checkout Best Practices

- **Pin by commit, build once.** Build an artifact once per commit, tag it with the commit SHA, and promote that same artifact across environments.
- **Label artifacts with provenance.** Add the commit SHA and repository URL as OCI labels or build metadata; sign with cosign and produce SLSA provenance where possible.
- **Use the shallowest checkout that works.** Default to depth 1, opt in to more history only for jobs that need it.
- **Fetch tags deliberately.** Release jobs need tags; most jobs do not.
- **Cache dependencies, not `.git`.** Caching Git directories across jobs is fragile; runners with `GIT_STRATEGY: fetch` already reuse them.
- **Treat CI tokens as least privilege.** Read-only by default (`permissions: contents: read`), write only in release jobs, prefer OIDC to long-lived cloud keys.
- **Mark the workspace safe in containers.** When the repository is owned by a different UID than the container user, Git refuses with "detected dubious ownership"; fix with `git config --global --add safe.directory "$PWD"` rather than disabling the check globally.

## 7.5 GitOps

GitOps applies Git workflows to operations. The OpenGitOps principles state that the desired state of a system is:

1. **Declarative**: described as data (Kubernetes manifests, Helm values, Kustomize overlays).
2. **Versioned and immutable**: stored in Git, with full history.
3. **Pulled automatically**: software agents pull the desired state from the source.
4. **Continuously reconciled**: agents observe actual state and correct drift.

```text
  Developer --PR--> app repo --CI--> image registry (api:1.4.0, digest sha256:...)
                                          |
                     CI or bot opens PR   v
  Reviewer --approve--> config repo (deploy/prod/api/values.yaml: tag 1.4.0)
                                          |
                           pull (poll or webhook)
                                          v
                      Argo CD / Flux in the cluster
                                          |
                            apply and reconcile
                                          v
                               Kubernetes cluster
```

Why it matters:

- **Audit trail**: every production change is a reviewed commit with an author and approver.
- **Rollback**: `git revert` the change; the controller converges back.
- **Drift correction**: manual `kubectl edit` changes are detected and reverted (or flagged).
- **Security**: CI no longer needs cluster credentials; the controller inside the cluster pulls.
- **Disaster recovery**: rebuild a cluster by pointing a controller at the repo.

### 7.5.1 Repository layout

Separate application source from deployment configuration. App code changes many times a day; config changes are deployments and deserve their own review and permissions.

```text
platform-config/
  apps/
    api/
      base/                 # common Kustomize base or Helm values
        deployment.yaml
        kustomization.yaml
      overlays/
        dev/kustomization.yaml
        staging/kustomization.yaml
        prod/kustomization.yaml
  clusters/
    prod-eu-1/
      apps.yaml             # which apps run on this cluster
    staging-eu-1/
      apps.yaml
```

> **Tip:** Model environments as directories on one `main` branch, not as branches. Promotion becomes a small, reviewable PR that copies a version from the staging overlay to the prod overlay, and `main` always describes every environment at once.

### 7.5.2 Argo CD

Argo CD runs in the cluster and syncs **Applications**, each pointing at a Git path and a target revision.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: api-prod
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/acme/platform-config.git
    targetRevision: main          # branch, tag or commit SHA
    path: apps/api/overlays/prod
  destination:
    server: https://kubernetes.default.svc
    namespace: api
  syncPolicy:
    automated:
      prune: true                  # delete resources removed from Git
      selfHeal: true               # revert manual drift
    syncOptions: [CreateNamespace=true]
```

Concepts: **sync status** (does the cluster match Git?), **health status** (are resources healthy?), **ApplicationSets** (generate many Applications from a template per cluster or directory), and the **app-of-apps** pattern.

### 7.5.3 Flux

Flux is a set of controllers. A `GitRepository` source defines what to pull; a `Kustomization` (or `HelmRelease`) defines what to apply from it.

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: platform-config
  namespace: flux-system
spec:
  interval: 1m
  url: https://github.com/acme/platform-config.git
  ref:
    branch: main
  secretRef:
    name: platform-config-auth
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: api-prod
  namespace: flux-system
spec:
  interval: 10m
  sourceRef:
    kind: GitRepository
    name: platform-config
  path: ./apps/api/overlays/prod
  prune: true
  wait: true
```

Flux can also verify commit signatures on the source (`spec.verify`) and has image automation controllers that commit new image tags back to Git.

| Aspect | Argo CD | Flux |
|---|---|---|
| UI | Rich built-in web UI | CLI-first; UIs via third parties |
| Model | Application CRDs, projects, RBAC | Composable controllers (source, kustomize, helm, notification, image) |
| Multi-cluster | Central Argo manages many clusters | Typically one Flux per cluster, bootstrapped from Git |
| Image updates | Argo CD Image Updater (separate) | Built-in image automation controllers |

### 7.5.4 Git practices for GitOps repos

- Protect `main` heavily; it is effectively production access.
- Require CODEOWNERS approval for `overlays/prod/`.
- Pin images by digest or immutable tag, never `latest`.
- Never put plain secrets in the repo; use Sealed Secrets, SOPS with KMS/age, or External Secrets Operator referencing a vault.
- Keep bot commits (image bumps) identifiable and signed.
- Roll back with `git revert`, not by editing the cluster.

## 7.6 Secrets: Prevention

Once a secret is pushed, assume it is compromised. Public GitHub repositories are scanned by attackers within minutes, and private repositories get cloned to laptops, CI caches and forks. Prevention is far cheaper than cleanup.

Layered defence:

| Layer | Control |
|---|---|
| Design | Secrets in a vault or cloud secret manager; workloads use OIDC/IAM roles, not static keys |
| Repo hygiene | `.gitignore` for `.env`, `*.pem`, `*.tfvars`, kubeconfigs; commit `.env.example` only |
| Developer machine | pre-commit with gitleaks or detect-secrets |
| Push time | Platform push protection (GitHub secret scanning push protection, GitLab secret push protection) |
| CI | gitleaks or trufflehog on every PR and on full history periodically |
| Monitoring | Platform secret scanning alerts, partner programs that auto-revoke known token formats |

### 7.6.1 gitleaks

```bash
gitleaks git -v                       # scan full history of the repo in the current dir
gitleaks git --log-opts="origin/main..HEAD"   # only new commits (PR scan)
gitleaks dir .                        # scan files on disk, not history
gitleaks git --report-format sarif --report-path gitleaks.sarif
```

> **Note:** Older gitleaks versions (before 8.19) used `gitleaks detect` for history and `gitleaks protect --staged` for staged changes. Both still appear in many tutorials and CI templates.

```text
Finding:     aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
Secret:      wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
RuleID:      aws-secret-access-key
Entropy:     4.7
File:        terraform/provider.tf
Line:        7
Commit:      b13a9e5c2f...
Author:      Lee Chen
Date:        2025-05-19T16:02:51Z
```

Allowlist false positives in `.gitleaks.toml` or with a `gitleaks:allow` comment, and keep a baseline for legacy findings so CI fails only on new leaks.

In GitHub Actions:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- uses: gitleaks/gitleaks-action@v2
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

## 7.7 Secrets: Response and History Cleanup

If a secret reaches a remote, follow this order. The order matters.

1. **Revoke and rotate the secret immediately.** This is the only step that actually removes the risk. Cleaning history does not un-leak a credential.
2. **Investigate use.** Check cloud audit logs (CloudTrail, Azure Activity Log, GCP Audit Logs) for activity with that credential since it was pushed.
3. **Remove it from the current code** with a normal commit, and move it into a secret manager.
4. **Decide whether to rewrite history.** Required for compliance or when the secret cannot be rotated (rare). Rewriting is disruptive: every clone, fork and open PR is affected.
5. **Rewrite, force push, and coordinate** with everyone who has a clone.
6. **Purge caches** on the platform (GitHub support can remove cached views and PR refs; GitLab offers repository cleanup and housekeeping).

### 7.7.1 git filter-repo

`git filter-repo` is the recommended tool for history rewriting; `git filter-branch` is deprecated and slow. Install with `pip install git-filter-repo` or `brew install git-filter-repo`.

It insists on a fresh clone so you cannot destroy your only copy:

```bash
git clone --mirror git@github.com:acme/platform-infra.git
cd platform-infra.git
```

Remove a file from all history:

```bash
git filter-repo --invert-paths --path terraform/prod.tfvars
```

Remove all files matching a glob:

```bash
git filter-repo --invert-paths --path-glob '*.pem'
```

Replace secret strings everywhere in history:

```bash
cat > ../replacements.txt <<'EOF'
wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY==>***REMOVED***
regex:AKIA[0-9A-Z]{16}==>AKIA_REMOVED
EOF
git filter-repo --replace-text ../replacements.txt
```

Remove large blobs:

```bash
git filter-repo --strip-blobs-bigger-than 50M
```

Then push the rewritten history:

```bash
git remote add origin git@github.com:acme/platform-infra.git   # filter-repo removes origin as a safety measure
git push --force --mirror origin
```

> **Warning:** `--mirror` force-pushes every ref, including deleting branches that do not exist locally. Make sure branch protection is temporarily relaxed only for the person doing the cleanup, and that everyone has been told to stop pushing. Re-enable protection immediately afterwards.

Afterwards, every collaborator must discard their old clone and re-clone (or hard reset every branch). An old clone pushed later will reintroduce the secret. Open PRs that reference old commits must be recreated.

| Tool | Notes |
|---|---|
| `git filter-repo` | Recommended, fast, flexible, maintained |
| BFG Repo-Cleaner | Java, simple for deleting files and replacing text; protects HEAD by default |
| `git filter-branch` | Deprecated; slow and easy to misuse |

## 7.8 Lab: Pipeline, GitOps and a Leaked Key

**Goal:** wire Git events to CI, observe shallow clone limitations, model a GitOps repo, and run the full secret-leak response.

1. **Shallow clone limits.** Using a repo with tags (Lab 4 or any public project):

   ```bash
   git clone --depth 1 file:///tmp/lab4 /tmp/lab7-shallow
   cd /tmp/lab7-shallow
   git describe --tags              # fails
   git fetch --unshallow --tags
   git describe --tags              # works
   ```

   Note: `file://` is required; a plain path ignores `--depth`.

2. **CI workflow.** Write a GitHub Actions or GitLab CI file that runs tests on PRs, builds on `main`, and releases only on tags matching `v*.*.*`, with `fetch-depth: 0` (or `GIT_DEPTH: "0"`) only in the release job. If you have a sandbox repo, push a branch, open a PR, then push a tag and confirm the right jobs run.

3. **GitOps layout.** Create a `platform-config` repository with `apps/api/base` and `apps/api/overlays/{staging,prod}`. Write an Argo CD `Application` or Flux `Kustomization` for prod. Simulate a promotion as a PR that changes the image tag in `overlays/prod`, then simulate a rollback with `git revert`.

4. **Leak and detect:**

   ```bash
   cd /tmp && rm -rf lab7 && mkdir lab7 && cd lab7 && git init
   echo 'provider "aws" { region = "eu-west-1" }' > main.tf
   git add . && git commit -m "feat: provider"
   echo 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"' > creds.tf
   git add . && git commit -m "feat: creds"
   git rm creds.tf && git commit -m "fix: remove creds"
   git log --oneline -S "wJalrX" --all       # still in history
   gitleaks git -v                            # if gitleaks is installed
   ```

5. **Clean history** (pretend you have already rotated the key):

   ```bash
   cd /tmp && git clone --no-local lab7 lab7-clean && cd lab7-clean
   git filter-repo --invert-paths --path creds.tf
   git log --oneline --all -S "wJalrX"        # nothing
   git log --oneline                          # note the changed hashes
   ```

6. Write a short incident note: when the key was pushed, when it was rotated, what audit logs showed, and who must re-clone.

## 7.9 Key Takeaways

- Branch pushes, PRs, merges to main and tag pushes map naturally to test, plan, deploy and release pipelines.
- Build once per commit, label artifacts with the commit SHA, and promote the same artifact through environments.
- CI defaults to shallow clones; request full history (`fetch-depth: 0`, `GIT_DEPTH: "0"`) only for jobs that need tags or merge bases.
- PR pipelines on GitHub test a merge commit of the PR into the base branch; never run untrusted fork code with secrets.
- GitOps: declarative, versioned, pulled and continuously reconciled; Argo CD and Flux sync clusters from Git, and `git revert` is your rollback.
- Model environments as directories in a config repo, not branches.
- Prevent secret leaks in layers: design, ignore files, pre-commit, push protection, CI scanning.
- If a secret leaks: rotate first, investigate, then optionally rewrite history with `git filter-repo`, force push and have everyone re-clone.
