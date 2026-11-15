# Shared by deploy.sh and test-remote.sh: which machine to reach and how to ship the repo.
#
# The target is SCRAPE_SSH, an ssh command line, set in the environment or in the
# untracked file .deploy.env at the repo root, e.g.:
#   SCRAPE_SSH="ssh -p 2222 user@host"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
[[ -f "$REPO_ROOT/.deploy.env" ]] && source "$REPO_ROOT/.deploy.env"
: "${SCRAPE_SSH:?set SCRAPE_SSH, e.g. in .deploy.env: SCRAPE_SSH=\"ssh -p 22 user@host\"}"

q() { printf '%q' "$1"; }

# Run one command line on the target in a login shell (PATH, TMPDIR as for a person).
remote() { $SCRAPE_SSH "bash -lc $(q "$1")"; }

# The arguments quoted for the remote shell; nothing at all when there are none.
quote_args() { [[ $# -eq 0 ]] || printf '%q ' "$@"; }

# Remote paths are relative to the remote home unless absolute; refuse anything odd
# before it reaches an rm -rf.
check_path() {
  [[ "$1" =~ ^[A-Za-z0-9._/-]+$ && "$1" != */ && "$1" != *..* && "$1" != "/" && "$1" != "." ]] ||
    { echo "refusing remote path: '$1'" >&2; exit 2; }
}

# ship <remote dir>: replace it with the tracked and new (not ignored) files of this repo.
ship() {
  check_path "$1"
  local tar_flags=()
  # macOS bsdtar would add Apple xattrs that GNU tar warns about.
  tar --version 2>/dev/null | grep -q bsdtar && tar_flags=(--no-mac-metadata)
  (cd "$REPO_ROOT" && git ls-files -z --cached --others --exclude-standard |
    COPYFILE_DISABLE=1 tar "${tar_flags[@]}" --null -T - -czf -) |
    remote "rm -rf $(q "$1") && mkdir -p $(q "$1") && tar --warning=no-unknown-keyword -xzf - -C $(q "$1")"
}
