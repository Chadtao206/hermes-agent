#!/usr/bin/env bash
# Command-allowlisted `gh` for Slack-invoked Boris review lanes.
#
# NOT a pass-through — contrast ghx.sh, which is. Deny by default: only the
# verbs below pass, and the single write verb is pinned to the PR the lane was
# dispatched for (BORIS_PR_SCOPE). `workflow` and `gist` are refused explicitly
# because the shared machine credential (spec D6) carries both scopes.
#
# Defence-in-depth, not the boundary: a command that never calls this wrapper is
# stopped by terminal_guard.py, not here.
set -uo pipefail

_scope="${BORIS_PR_SCOPE:-}"
_tokfile="${BORIS_GH_TOKEN_PATH:-/Users/ctao/.hermes/state/.gh_token}"

refuse() { echo "ghx_ro: REFUSED $*" >&2; exit 3; }

[ -n "$_scope" ] || refuse "BORIS_PR_SCOPE is unset"
_scope_repo="${_scope%%#*}"
_scope_num="${_scope##*#}"

# Reject write/blocking flags wherever they appear in argv.
for a in "$@"; do
  case "$a" in
    --approve|--request-changes|--admin|--merge|--squash|--rebase|--delete-branch)
      refuse "flag $a" ;;
  esac
done

verb="${1:-}"; sub="${2:-}"

case "$verb" in
  pr)
    case "$sub" in
      view|diff|checks|status) : ;;
      review|comment)
        _r=""; _n=""; _prev=""
        for a in "$@"; do
          case "$_prev" in -R|--repo) _r="$a" ;; esac
          case "$a" in [0-9]*) [ -z "$_n" ] && _n="$a" ;; esac
          _prev="$a"
        done
        [ "$_r" = "$_scope_repo" ] || refuse "out of scope repo '$_r' (scope $_scope)"
        [ "$_n" = "$_scope_num" ] || refuse "out of scope PR '$_n' (scope $_scope)"
        if [ "$sub" = "review" ]; then
          case " $* " in *" --comment "*) : ;; *) refuse "pr review without --comment" ;; esac
        fi ;;
      *) refuse "pr $sub" ;;
    esac ;;
  run)
    case "$sub" in view|list) : ;; *) refuse "run $sub" ;; esac ;;
  api)
    for a in "$@"; do
      case "$a" in
        -X|--method) refuse "api with explicit method" ;;
        -f|--field|--input) refuse "api with a request body" ;;
      esac
    done
    _ok=0
    for a in "$@"; do
      case "$a" in /repos/Ylopo/*) _ok=1 ;; esac
    done
    [ "$_ok" = "1" ] || refuse "api path outside /repos/Ylopo/" ;;
  *) refuse "verb '$verb'" ;;
esac

if [ "${GHX_RO_DRY_RUN:-0}" = "1" ]; then echo "ghx_ro: would run gh $*"; exit 0; fi

[ -r "$_tokfile" ] || { echo "ghx_ro: cannot read token file $_tokfile" >&2; exit 1; }
export GH_TOKEN; GH_TOKEN="$(cat "$_tokfile")"
exec gh "$@"
