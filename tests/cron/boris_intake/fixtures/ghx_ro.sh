#!/usr/bin/env bash
# Command-allowlisted `gh` for Slack-invoked Boris review lanes.
#
# NOT a pass-through — contrast ghx.sh, which is. Deny by default: only the
# verbs below pass and every read is pinned to the PR the lane was dispatched
# for (BORIS_PR_SCOPE). `workflow` and `gist` are refused explicitly
# because the shared machine credential (spec D6) carries both scopes.
#
# Defence-in-depth, not the boundary: a command that never calls this wrapper is
# stopped by terminal_guard.py, not here.
set -uo pipefail

_scope="${BORIS_PR_SCOPE:-}"
_head="${BORIS_PR_HEAD_SHA:-}"
_tokfile="${BORIS_GH_TOKEN_PATH:-/Users/ctao/.hermes/state/.gh_token}"

refuse() { echo "ghx_ro: REFUSED $*" >&2; exit 3; }

[ -n "$_scope" ] || refuse "BORIS_PR_SCOPE is unset"
[[ "$_head" =~ ^[0-9a-f]{40}$ ]] || refuse "BORIS_PR_HEAD_SHA is invalid"
_scope_repo="${_scope%%#*}"
_scope_num="${_scope##*#}"

# Reject write/blocking flags wherever they appear in argv.
for a in "$@"; do
  case "$a" in
    --approve|--request-changes|--admin|--merge|--squash|--rebase|--delete-branch|--hostname)
      refuse "flag $a" ;;
    -X*|--method=*|-f|--field|--field=*|-F|--raw-field|--raw-field=*|--input|--input=*)
      refuse "request-writing flag $a" ;;
  esac
done

verb="${1:-}"; sub="${2:-}"

require_pr_scope() {
  local _r="" _n="" _prev="" a
  for a in "$@"; do
    case "$_prev" in -R|--repo) _r="$a" ;; esac
    case "$a" in [0-9]*) [ -z "$_n" ] && _n="$a" ;; esac
    _prev="$a"
  done
  [ "$_r" = "$_scope_repo" ] || refuse "out of scope repo '$_r' (scope $_scope)"
  [ "$_n" = "$_scope_num" ] || refuse "out of scope PR '$_n' (scope $_scope)"
}

case "$verb" in
  pr)
    case "$sub" in
      view|diff|checks|status) require_pr_scope "$@" ;;
      review|comment) refuse "pr $sub is not allowed" ;;
      *) refuse "pr $sub" ;;
    esac ;;
  api)
    for a in "$@"; do
      case "$a" in
        -X|--method) refuse "api with explicit method" ;;
        -f|--field|--input) refuse "api with a request body" ;;
      esac
    done
    _api_path=""
    for a in "$@"; do
      case "$a" in /repos/*) _api_path="$a" ;; esac
    done
    case "$_api_path" in
      *"/../"*|*"/./"*|*"%2e"*|*"%2E"*) refuse "api path traversal" ;;
    esac
    [[ "$_api_path" == "/repos/${_scope_repo}/contents/"*"?ref=${_head}" ]] ||
      refuse "api path is outside the pinned repository head" ;;
  *) refuse "verb '$verb'" ;;
esac

if [ "${GHX_RO_DRY_RUN:-0}" = "1" ]; then echo "ghx_ro: would run gh $*"; exit 0; fi

[ -r "$_tokfile" ] || { echo "ghx_ro: cannot read token file $_tokfile" >&2; exit 1; }
export GH_TOKEN; GH_TOKEN="$(cat "$_tokfile")"
exec gh "$@"
