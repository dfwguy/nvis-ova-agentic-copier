#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
set -Eeuo pipefail

stop() {
  printf 'stopped: %s\n' "$*" >&2
  exit 2
}

if [[ "${AGENT_WORK_ENV_VERSION:-}" != "1" ]]; then
  stop 'AGENT_WORK_ENV_VERSION=1 is required'
fi

require_path() {
  local value="${!1:-}"
  if [[ "$value" != /* || ! -x "$value" ]]; then
    stop "$1 must be an absolute executable path"
  fi
}

housekeeping() {
  local status
  set +e
  "$AGENT_WORK_TIMEOUT" --signal=TERM --kill-after=30s 120s "$@"
  status=$?
  set -e
  if [[ "$status" == 124 || "$status" == 137 ]]; then
    stop 'housekeeping command timed out'
  fi
  return "$status"
}

normal_url() {
  printf '%s\n' "${1%.git}"
}

host="${GITLAB_HOST:-}"
case "$host" in
  ''|*://*|*/*)
    stop 'GITLAB_HOST must be a bare hostname'
    ;;
esac
project_id="${GITLAB_PROJECT_ID:-}"
factory_username="${AGENT_WORK_FACTORY_USERNAME:-}"
case "$project_id" in
  ''|0|00*|*[!0-9]*)
    stop 'GITLAB_PROJECT_ID must be a positive integer'
    ;;
esac
if [[ ! "$factory_username" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  stop 'AGENT_WORK_FACTORY_USERNAME must be a safe GitLab username'
fi
if [[ "${AGENT_WORK_CHECKOUT:-}" != /* || ! -d "$AGENT_WORK_CHECKOUT" ]]; then
  stop 'AGENT_WORK_CHECKOUT must be an absolute directory'
fi
if [[ ! -f "$AGENT_WORK_CHECKOUT/.envrc" || -L "$AGENT_WORK_CHECKOUT/.envrc" ]]; then
  stop 'AGENT_WORK_DIRENV requires a regular AGENT_WORK_CHECKOUT/.envrc'
fi
for name in AGENT_WORK_GIT AGENT_WORK_GLAB AGENT_WORK_JQ AGENT_WORK_FLOCK AGENT_WORK_UV; do
  require_path "$name"
done
require_path AGENT_WORK_TIMEOUT
if [[ -z "${AGENT_WORK_AGENT_PATH:-}" ]]; then
  stop 'AGENT_WORK_AGENT_PATH is not set'
fi
if [[ ! -x "$AGENT_WORK_AGENT_PATH" ]]; then
  stop 'AGENT_WORK_AGENT_PATH is not executable'
fi
if [[ "$AGENT_WORK_AGENT_PATH" != /* ]]; then
  stop 'AGENT_WORK_AGENT_PATH must be an absolute executable path'
fi
base="${AGENT_WORK_BASE_BRANCH:-}"
if [[ "$base" != agent/* || "$base" == agent/ ]]; then
  stop 'AGENT_WORK_BASE_BRANCH must be a reserved agent/* branch'
fi
agent_name="${AGENT_WORK_AGENT:-codex}"
case "$agent_name" in
  codex|claude|pi)
    ;;
  *)
    stop 'AGENT_WORK_AGENT must be one of: codex, claude, pi'
    ;;
esac
require_path AGENT_WORK_DIRENV

git=("$AGENT_WORK_GIT" -c core.hooksPath=/dev/null)
checkout="$(cd -P "$AGENT_WORK_CHECKOUT" && pwd)"
credential_helper="$checkout/src/agent-work-git-credential.sh"
if [[ ! -f "$credential_helper" || -L "$credential_helper" || ! -x "$credential_helper" ]]; then
  stop 'repository Git credential helper must be an executable regular file'
fi
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never
export GIT_CONFIG_COUNT=4 GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false
export GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0=
export GIT_CONFIG_KEY_1=credential.helper
export GIT_CONFIG_VALUE_1="!exec \"\$AGENT_WORK_GIT_CREDENTIAL_HELPER\""
export AGENT_WORK_GIT_CREDENTIAL_HELPER="$credential_helper"
export GIT_CONFIG_KEY_2=credential.useHttpPath GIT_CONFIG_VALUE_2=true
export GIT_CONFIG_KEY_3=core.askPass GIT_CONFIG_VALUE_3=/bin/false
unset GIT_CONFIG_PARAMETERS
authenticated() {
  local bounded=false
  if [[ "${1:-}" == --housekeeping ]]; then
    bounded=true
    shift
  fi
  local command=("$AGENT_WORK_DIRENV" exec "$checkout" /usr/bin/env -u GIT_CONFIG_PARAMETERS
    "GITLAB_HOST=$host" "GITLAB_PROJECT_ID=$project_id" \
    "AGENT_WORK_FACTORY_USERNAME=$factory_username" \
    "AGENT_WORK_GIT=$AGENT_WORK_GIT" "AGENT_WORK_GLAB=$AGENT_WORK_GLAB" \
    "GIT_TERMINAL_PROMPT=0" "GCM_INTERACTIVE=never" \
    "GIT_ASKPASS=/bin/false" "SSH_ASKPASS=/bin/false" \
    "GIT_CONFIG_COUNT=4" "GIT_CONFIG_KEY_0=credential.helper" \
    "GIT_CONFIG_VALUE_0=" "GIT_CONFIG_KEY_1=credential.helper" \
    "GIT_CONFIG_VALUE_1=!exec \"\$AGENT_WORK_GIT_CREDENTIAL_HELPER\"" \
    "AGENT_WORK_GIT_CREDENTIAL_HELPER=$credential_helper" \
    "GIT_CONFIG_KEY_2=credential.useHttpPath" "GIT_CONFIG_VALUE_2=true" \
    "GIT_CONFIG_KEY_3=core.askPass" "GIT_CONFIG_VALUE_3=/bin/false")
  if "$bounded"; then
    housekeeping "${command[@]}" "$@"
  else
    "${command[@]}" "$@"
  fi
}
if ! top="$(housekeeping "${git[@]}" -C "$checkout" rev-parse --show-toplevel)"; then
  stop 'AGENT_WORK_CHECKOUT is not a Git checkout'
fi
if [[ "$top" != "$checkout" ]]; then
  stop 'AGENT_WORK_CHECKOUT must resolve to the Git top-level'
fi
if ! housekeeping "${git[@]}" -C "$checkout" check-ref-format --branch "$base" >/dev/null; then
  stop 'AGENT_WORK_BASE_BRANCH is not valid'
fi
if ! local_config="$(housekeeping "${git[@]}" -C "$checkout" config --local --name-only --list)"; then
  stop 'cannot inspect local checkout Git configuration'
fi
while IFS= read -r config_name; do
  case "$config_name" in
    core.repositoryformatversion|core.filemode|core.bare|core.logallrefupdates|core.ignorecase|core.precomposeunicode|user.name|user.email|remote.origin.url|remote.origin.fetch|branch.*.remote|branch.*.merge)
      ;;
    *)
      stop 'local checkout Git configuration is not allowed for refresh operations'
      ;;
  esac
done <<<"$local_config"
if ! git_dir="$(housekeeping "${git[@]}" -C "$checkout" rev-parse --path-format=absolute --git-dir)"; then
  stop 'cannot resolve checkout Git directory'
fi
if [[ "$git_dir" != /* ]]; then
  stop 'checkout Git directory is not absolute'
fi
if ! attributes_file="$(housekeeping "${git[@]}" -C "$checkout" rev-parse --path-format=absolute --git-path info/attributes)"; then
  stop 'cannot resolve checkout attributes path'
fi
if [[ -s "$attributes_file" ]]; then
  stop 'checkout local Git attributes are not allowed for refresh operations'
fi
exec 9>"$git_dir/agent-work-systemd.lock"
if ! "$AGENT_WORK_FLOCK" -n 9; then
  stop 'another wrapper invocation holds the checkout refresh lock'
fi
clean() {
  local status_output
  if ! status_output="$(housekeeping "${git[@]}" -C "$checkout" status --porcelain --untracked-files=all)"; then
    stop 'cannot inspect checkout cleanliness'
  fi
  if [[ -n "$status_output" ]]; then
    stop 'checkout is not clean'
  fi
  if [[ "$(housekeeping "${git[@]}" -C "$checkout" symbolic-ref --quiet --short HEAD)" != "$base" ]]; then
    stop 'checkout is not on AGENT_WORK_BASE_BRANCH'
  fi
}
clean
if ! origin_urls="$(housekeeping "${git[@]}" -C "$checkout" config --get-all remote.origin.url)"; then
  stop 'checkout has no origin remote'
fi
if [[ "${origin_urls%%$'\n'*}" != "$origin_urls" ]]; then
  stop 'checkout origin has ambiguous URLs'
fi
origin="$origin_urls"
default=""
remote_ref=""
verify_project() {
  local actual_id http json
  local project_command=("$AGENT_WORK_GLAB" api --hostname "$host" "projects/$project_id")
  if ! json="$(authenticated --housekeeping "${project_command[@]}")"; then
    stop 'cannot read configured GitLab project'
  fi
  if ! actual_id="$(housekeeping "$AGENT_WORK_JQ" -er '.id | if type == "number" then tostring else empty end' <<<"$json")"; then
    stop 'GitLab project response has no numeric id'
  fi
  if [[ "$actual_id" != "$project_id" ]]; then
    stop 'GitLab project id does not match configured project'
  fi
  if ! default="$(housekeeping "$AGENT_WORK_JQ" -er '.default_branch | strings | select(length > 0)' <<<"$json")"; then
    stop 'GitLab project response has no default branch'
  fi
  if ! housekeeping "${git[@]}" -C "$checkout" check-ref-format --branch "$default" >/dev/null; then
    stop 'GitLab default branch is not valid'
  fi
  if ! http="$(housekeeping "$AGENT_WORK_JQ" -er '.http_url_to_repo | strings | select(length > 0)' <<<"$json")"; then
    stop 'GitLab project response has no HTTP origin'
  fi
  case "$http" in https://*) ;; *) stop 'GitLab project HTTP origin must use credential-free HTTPS' ;; esac
  if [[ "$http" == https://*@* || "$(normal_url "$origin")" != "$(normal_url "$http")" ]]; then
    stop 'origin must match configured GitLab HTTPS project URL'
  fi
  if [[ "$(housekeeping "${git[@]}" -C "$checkout" config --get "branch.$base.remote")" != origin || "$(housekeeping "${git[@]}" -C "$checkout" config --get "branch.$base.merge")" != "refs/heads/$default" ]]; then
    stop 'checkout upstream is not origin default branch'
  fi
  remote_ref="refs/remotes/origin/$default"
}

refresh_base() {
  local grep_status
  verify_project
  clean
  if ! authenticated --housekeeping "${git[@]}" -C "$checkout" fetch --no-tags --no-recurse-submodules origin "refs/heads/$default:$remote_ref"; then
    stop 'cannot fetch verified default branch'
  fi
  if housekeeping "${git[@]}" -C "$checkout" grep -q -E '(^|[[:space:]])filter(=|[[:space:]]|$)' "$remote_ref" -- .gitattributes '*/.gitattributes'; then
    stop 'target branch contains filter attributes'
  else
    grep_status="$?"
    if [[ "$grep_status" != 1 ]]; then stop 'cannot inspect target Git attributes'; fi
  fi
  if ! housekeeping "${git[@]}" -C "$checkout" merge --ff-only "$remote_ref"; then stop 'cannot fast-forward checkout to verified default branch'; fi
  clean
  if [[ "$(housekeeping "${git[@]}" -C "$checkout" rev-parse HEAD)" != "$(housekeeping "${git[@]}" -C "$checkout" rev-parse "$remote_ref")" ]]; then stop 'checkout HEAD is not the refreshed origin default branch'; fi
}

if [[ "${1:-}" != --apply ]]; then
  verify_project
  if ! lines="$(authenticated --housekeeping "${git[@]}" -C "$checkout" ls-remote --exit-code origin "refs/heads/$default")"; then
    stop 'cannot read default branch from origin'
  fi
  if ! [[ "$lines" =~ ^[0-9a-f]{40}[[:space:]]refs/heads/ ]] || [[ "${lines%%$'\n'*}" != "$lines" ]]; then
    stop 'origin default branch did not return one valid SHA'
  fi
  if [[ "${lines%%[[:space:]]*}" != "$(housekeeping "${git[@]}" -C "$checkout" rev-parse HEAD)" ]]; then
    stop 'preview checkout is not current with origin default branch'
  fi
  if [[ "${1:-}" == --validate-service-environment ]]; then
    exit 0
  fi
else
  refresh_base
fi
if ! authenticated --housekeeping "$AGENT_WORK_GIT" -c core.hooksPath=/dev/null -C "$checkout" \
  push --dry-run origin "HEAD:refs/heads/agent/readiness" >/dev/null 2>&1; then
  stop 'Git credential-helper publication readiness check failed'
fi
cd "$checkout"
if [[ "${1:-}" != --apply ]]; then
  authenticated --housekeeping "$AGENT_WORK_UV" run agent-work loop run --json
  exit $?
fi

validate_applied_report() {
  local report="$1"
  housekeeping "$AGENT_WORK_JQ" -e -s '
    length == 1 and (.[0] | (
      .schema_version == 1 and .command == "loop.run" and .applied == true and
      ((.stop_reason == "idle" and .outcome == "idle") or
       (.stop_reason == "prerequisite_labels_reconciled" and
        .outcome == "prerequisite_labels_reconciled") or
       (.stop_reason == "awaiting_human" and .outcome == "activation_complete") or
       (.stop_reason == "blocked" and .outcome == "activation_complete") or
       (.stop_reason == "failed" and .outcome == "activation_complete") or
       (.stop_reason == "complete" and .outcome == "activation_complete") or
       (.stop_reason == "pipeline_wait" and
        (.outcome == "pipeline_wait" or .outcome == "activation_complete")) or
       (.stop_reason == "pipeline_blocked" and
        (.outcome == "pipeline_blocked" or .outcome == "activation_complete")) or
       (.stop_reason == "pipeline_inventory_blocked" and
        .outcome == "pipeline_inventory_blocked") or
       (.stop_reason == "retry_wait" and .outcome == "activation_complete") or
       (.stop_reason == "repair" and .outcome == "activation_complete") or
       (.stop_reason == "time_limit" and .outcome == "activation_complete") or
       (.stop_reason == "segment_limit" and .outcome == "activation_complete"))
    ))
  ' <<<"$report" >/dev/null
}

preview_outcome() {
  local report="$1"
  if ! housekeeping "$AGENT_WORK_JQ" -e -s '
    length == 1 and (.[0] | .schema_version == 1 and .command == "loop.run" and
    .applied == false and (.outcome == "eligible" or .outcome == "idle" or
    .outcome == "pipeline_wait" or .outcome == "pipeline_blocked" or
    .outcome == "pipeline_inventory_blocked"))
  ' <<<"$report" >/dev/null; then
    return 1
  fi
  housekeeping "$AGENT_WORK_JQ" -er '.[0].outcome' <<<"[$report]"
}

while :; do
  if ! applied_report="$(authenticated "$AGENT_WORK_UV" run agent-work loop run --apply --json)"; then
    stop 'agent-work applied activation failed'
  fi
  if ! validate_applied_report "$applied_report"; then
    stop 'agent-work applied activation returned an invalid report'
  fi
  printf '%s\n' "$applied_report"
  if ! stop_reason="$(housekeeping "$AGENT_WORK_JQ" -er '.[0].stop_reason' <<<"[$applied_report]")"; then
    stop 'agent-work applied activation report has no stop reason'
  fi
  case "$stop_reason" in
    awaiting_human|blocked|failed|complete)
      ;;
    *)
      exit 0
      ;;
  esac
  if ! preview_report="$(authenticated --housekeeping "$AGENT_WORK_UV" run agent-work loop run --json)"; then
    stop 'agent-work eligibility preview failed'
  fi
  if ! outcome="$(preview_outcome "$preview_report")"; then
    stop 'agent-work eligibility preview returned an invalid report'
  fi
  printf '%s\n' "$preview_report"
  if [[ "$outcome" != eligible ]]; then
    exit 0
  fi
  refresh_base
done
