#!/usr/bin/env bash
# Command-allowlisted `gh` for the deterministic PR-discovery cron.
#
# Sibling of ghx_ro.sh, differing in exactly one way: ghx_ro.sh pins every
# read to the single pull request a Boris review lane was dispatched for
# (BORIS_PR_SCOPE), which is precisely why it cannot enumerate. This one pins
# every read to one ORG (DISCOVERY_ORG_SCOPE) -- the shape enumeration needs
# -- and refuses outright when that variable is unset, so it can never be
# pointed at an arbitrary org by omission.
#
# WHAT THIS IS WORTH, HONESTLY. ghx_ro.sh exists because a Boris review lane
# is an LLM agent holding a terminal: its allowlist constrains what a model
# can be induced to run. `pr_discovery` is a `no_agent: true` deterministic
# Python cron whose argv are fixed constants -- there is no model here to
# constrain, and this wrapper stops no attack that is live today. Its value
# is narrower and worth stating exactly: a FUTURE EDIT to discover.py cannot
# add a write call without also editing this file, whose refusals are tested.
# It is not a sandbox. Anyone who can already run commands as this user can
# run `gh` directly and never touch this script.
#
# Deny by default. The two verb pairs below are the entire allowlist, so
# `api` (`api graphql` included), `workflow` and `gist` need no clause of
# their own -- they fall to the final refusal like everything else. They are
# named here because they are the ones worth naming: the shared machine
# credential carries the `workflow` and `gist` scopes, and `api graphql` is
# how a write reaches GitHub when nobody is watching the verb.
set -uo pipefail

_scope="${DISCOVERY_ORG_SCOPE:-}"
_tokfile="${DISCOVERY_GH_TOKEN_PATH:-/Users/ctao/.hermes/state/.gh_token}"

refuse() { echo "ghx_discovery: REFUSED $*" >&2; exit 3; }

[ -n "$_scope" ] || refuse "DISCOVERY_ORG_SCOPE is unset"

# Write and host-redirect flags are refused wherever they appear: argv
# position is never trusted, because the allowlisted verbs above accept these
# flags in any order and a future edit would not have to move them to the
# front to take effect. `--hostname` is a scope escape, not a write: it
# retargets an identically-named org on another GitHub host.
for a in "$@"; do
  case "$a" in
    --approve|--request-changes|--admin|--merge|--squash|--rebase|--delete-branch|--hostname|--hostname=*)
      refuse "flag $a" ;;
    -X*|--method=*|-f|--field|--field=*|-F|--raw-field|--raw-field=*|--input|--input=*)
      refuse "request-writing flag $a" ;;
  esac
done

verb="${1:-}"; sub="${2:-}"

# Every `--owner` given must name the scoped org, and at least one must be
# given: `gh search prs` with no owner qualifier searches all of GitHub.
require_org_owner() {
  local _seen="" _prev="" _owner="" a
  for a in "$@"; do
    _owner=""
    case "$_prev" in --owner) _owner="$a" ;; esac
    case "$a" in --owner=*) _owner="${a#--owner=}" ;; esac
    if [ -n "$_owner" ]; then
      [ "$_owner" = "$_scope" ] ||
        refuse "search owner '$_owner' is outside the scoped org '$_scope'"
      _seen=1
    fi
    _prev="$a"
  done
  [ -n "$_seen" ] || refuse "search without --owner=$_scope"
}

# Every `-R`/`--repo` given must name a repo inside the scoped org, and at
# least one must be given (without it `gh pr view` resolves the repo from the
# current directory's git remote, which is not a scope this script can see).
# A URL argument is refused outright because `gh pr view <url>` overrides -R
# entirely -- an in-scope -R next to an out-of-scope URL reads the URL.
require_org_repo() {
  local _seen="" _prev="" _repo="" _provided="" _rowner _rname a
  for a in "$@"; do
    _repo=""; _provided=""
    case "$a" in *://*) refuse "URL argument '$a' would override -R" ;; esac
    case "$_prev" in -R|--repo) _repo="$a"; _provided=1 ;; esac
    case "$a" in
      --repo=*) _repo="${a#--repo=}"; _provided=1 ;;
      -R?*) _repo="${a#-R}"; _provided=1 ;;
    esac
    if [ -n "$_provided" ]; then
      _rowner="${_repo%%/*}"
      _rname="${_repo#*/}"
      { [ "$_rowner" = "$_scope" ] && [ -n "$_rname" ] && [ "$_rname" != "$_repo" ]; } ||
        refuse "repo '$_repo' is outside the scoped org '$_scope'"
      _seen=1
    fi
    _prev="$a"
  done
  [ -n "$_seen" ] || refuse "pr $sub without -R $_scope/<repo>"
}

case "$verb" in
  search)
    case "$sub" in
      prs) require_org_owner "$@" ;;
      *) refuse "search $sub" ;;
    esac ;;
  pr)
    case "$sub" in
      view|checks) require_org_repo "$@" ;;
      *) refuse "pr $sub" ;;
    esac ;;
  *) refuse "verb '$verb'" ;;
esac

if [ "${GHX_DISCOVERY_DRY_RUN:-0}" = "1" ]; then
  echo "ghx_discovery: would run gh $*"; exit 0
fi

[ -r "$_tokfile" ] || {
  echo "ghx_discovery: cannot read token file $_tokfile" >&2; exit 1; }
GH_TOKEN="$(cat "$_tokfile")"
# An empty credential does not make `gh` fail; it makes it run
# unauthenticated, which for a search means an empty result set that looks
# exactly like "this org has no open PRs". Refuse instead.
[ -n "$GH_TOKEN" ] || {
  echo "ghx_discovery: token file $_tokfile is empty" >&2; exit 1; }
export GH_TOKEN
exec gh "$@"
