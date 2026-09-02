#!/bin/sh
# Cursor sessionStart hook: check out every repository of the claim.
#
# The spawn hook already set `origin` in each worker root; that is what makes
# Cursor route the request to this worker. The primary repository lives in
# CURSOR_WORKER_WORKSPACE_DIR. A multi-repository request has one more root per
# extra repository under /home/tl-user/repos, each passed to the worker as its
# own --worker-dir. Cursor runs this hook after the claim with the sessionStart
# payload on stdin. The fetches authenticate with the short-lived GitHub token
# Cursor writes into the worker's git configuration (--mint-github-token). No
# PAT is involved.
set -eu

# Cursor discards the stderr of a failed hook; keep it next to the worker log.
exec 2>>/var/log/cursor-tl/checkout.log

workspace="${CURSOR_WORKER_WORKSPACE_DIR:?CURSOR_WORKER_WORKSPACE_DIR is required}"
repos_dir="${CURSOR_TL_REPOS_DIR:-/home/tl-user/repos}"
# No terminal here; a missing token must fail fast instead of prompting.
export GIT_TERMINAL_PROMPT=0

now() { date -u +%FT%TZ; }

# One line per root: "<directory>\t<ref>". The primary root comes first. An
# extra root is paired with the payload entry whose URL matches its `origin`;
# when the payload carries no URLs the entries are paired by position. The
# spawn hook records each extra root and its zero-based request index in
# <repos_dir>/.roots; a sandbox with no readable manifest falls back to sorted
# directory names. A root with no ref fetches the remote HEAD.
plan="$(python3 -c '
import glob, json, os, subprocess, sys, time

workspace, repos_dir = sys.argv[1], sys.argv[2]
repos = json.load(sys.stdin).get("repos") or []
# Only one repository can be the primary. Cursor could flag more than one; the
# rest must stay in `others`, because each of them has a worker root on disk.
primary = ([r for r in repos if r.get("primary")] or repos[:1])[:1]
others = [r for r in repos if all(r is not p for p in primary)]

def warn(message):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(stamp + " warning: " + message, file=sys.stderr)

def url_of(repo):
    for key in ("url", "repoUrl", "repositoryUrl", "cloneUrl", "htmlUrl", "repository"):
        value = repo.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""

def norm(url):
    url = url.strip().lower()
    for prefix in ("https://", "http://", "ssh://", "git@"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    url = url.replace(":", "/", 1) if "/" not in url.split(":", 1)[0] and ":" in url else url
    url = url.rstrip("/")
    return url[:-4] if url.endswith(".git") else url

print(workspace + "\t" + ((primary[0].get("ref") or "") if primary else ""))
roots = None
try:
    with open(os.path.join(repos_dir, ".roots")) as manifest:
        roots = []
        for fallback_position, line in enumerate(manifest):
            line = line.strip()
            if not line:
                continue
            fields = line.split("\t", 1)
            if len(fields) == 2 and fields[0].isdigit():
                request_index, root = int(fields[0]), fields[1]
            else:
                # Manifests written by older images contained only paths.
                request_index, root = fallback_position + 1, line
            roots.append((request_index, root))
except OSError:
    roots = None
if roots is None:
    roots = [
        (position + 1, root)
        for position, root in enumerate(sorted(
            path[: -len("/.git")]
            for path in glob.glob(os.path.join(repos_dir, "*", ".git"))
        ))
    ]
# Keep the request position of every root. Positional pairing reads it, so a
# root that lost its `.git` cannot shift the later roots onto another ref.
listed = [
    (request_index, root)
    for request_index, root in roots
    if os.path.isdir(os.path.join(root, ".git"))
]
positional = not any(url_of(r) for r in others)
for request_index, root in listed:
    origin = subprocess.run(
        ["git", "-C", root, "remote", "get-url", "origin"], capture_output=True, text=True
    ).stdout.strip()
    match = [r for r in others if url_of(r) and norm(url_of(r)) == norm(origin)]
    if not match and positional:
        candidate = repos[request_index:request_index + 1]
        match = [r for r in candidate if all(r is not p for p in primary)]
    if not match:
        # The root comes from the claim, so the payload should name it. Remote
        # HEAD is the only thing left to check out; say so, because it is
        # probably not the ref the request asked for.
        warn(
            "no repository in the sessionStart payload matches " + root
            + " (origin " + (origin or "unset") + "); using remote HEAD"
        )
    print(root + "\t" + ((match[0].get("ref") or "") if match else ""))
' "$workspace" "$repos_dir")"

primary_ref="$(printf '%s\n' "$plan" | head -n 1 | cut -f 2)"
if [ -z "$primary_ref" ]; then
    echo "$(now) error: sessionStart payload has no repository ref" >&2
    exit 1
fi

# Cursor writes the minted token to git config in parallel with this hook and
# kills the hook after 60 seconds, so all roots share most of that window.
deadline=$(( $(date +%s) + 50 ))

checkout_root() {
    root="$1"
    ref="$2"
    # A resumed sandbox, or a follow-up session on the same worker, already has
    # the checkout. Hibernation keeps the filesystem, so this is the common path.
    if git -C "$root" rev-parse --verify --quiet HEAD >/dev/null; then
        return 0
    fi
    attempt=0
    while :; do
        attempt=$((attempt + 1))
        if [ -n "$ref" ]; then
            if git -C "$root" fetch --quiet origin "$ref" \
                && git -C "$root" checkout --quiet -B "$ref" FETCH_HEAD; then
                echo "$(now) checked out $ref in $root after $attempt attempt(s)" >&2
                return 0
            fi
        elif git -C "$root" fetch --quiet origin HEAD \
            && git -C "$root" checkout --quiet --detach FETCH_HEAD; then
            echo "$(now) checked out remote HEAD in $root after $attempt attempt(s)" >&2
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "$(now) error: could not fetch '${ref:-HEAD}' from origin in $root; confirm GitHub token minting and repository access" >&2
            return 1
        fi
        sleep 2
    done
}

tab="$(printf '\t')"
printf '%s\n' "$plan" | while IFS="$tab" read -r root ref; do
    [ -n "$root" ] || continue
    checkout_root "$root" "$ref" || exit 1
done || exit 1

printf '{"status":"ok"}\n'
