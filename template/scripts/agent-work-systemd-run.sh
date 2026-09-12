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

require_path() {
  local value="${!1:-}"
  if [[ "$value" != /* || ! -x "$value" ]]; then
    stop "$1 must be an absolute executable path"
  fi
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
case "$project_id" in
  ''|0|00*|*[!0-9]*)
    stop 'GITLAB_PROJECT_ID must be a positive integer'
    ;;
esac
if [[ "${AGENT_WORK_CHECKOUT:-}" != /* || ! -d "$AGENT_WORK_CHECKOUT" ]]; then
  stop 'AGENT_WORK_CHECKOUT must be an absolute directory'
fi
if [[ -n "${AGENT_WORK_DIRENV:-}" ]]; then
  if [[ ! -f "$AGENT_WORK_CHECKOUT/.envrc" || -L "$AGENT_WORK_CHECKOUT/.envrc" ]]; then
    stop 'AGENT_WORK_DIRENV requires a regular AGENT_WORK_CHECKOUT/.envrc'
  fi
fi
for name in AGENT_WORK_GIT AGENT_WORK_GLAB AGENT_WORK_JQ AGENT_WORK_FLOCK AGENT_WORK_UV; do
  require_path "$name"
done
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
if [[ -n "${AGENT_WORK_DIRENV:-}" ]]; then
  if [[ ! -x "$AGENT_WORK_DIRENV" ]]; then
    stop 'AGENT_WORK_DIRENV is not executable'
  fi
  require_path AGENT_WORK_DIRENV
fi

git=("$AGENT_WORK_GIT" -c core.hooksPath=/dev/null)
checkout="$(cd -P "$AGENT_WORK_CHECKOUT" && pwd)"
if ! top="$("${git[@]}" -C "$checkout" rev-parse --show-toplevel)"; then
  stop 'AGENT_WORK_CHECKOUT is not a Git checkout'
fi
if [[ "$top" != "$checkout" ]]; then
  stop 'AGENT_WORK_CHECKOUT must resolve to the Git top-level'
fi
if ! "${git[@]}" -C "$checkout" check-ref-format --branch "$base" >/dev/null; then
  stop 'AGENT_WORK_BASE_BRANCH is not valid'
fi
if ! local_config="$("${git[@]}" -C "$checkout" config --local --name-only --list)"; then
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
if ! git_dir="$("${git[@]}" -C "$checkout" rev-parse --path-format=absolute --git-dir)"; then
  stop 'cannot resolve checkout Git directory'
fi
if [[ "$git_dir" != /* ]]; then
  stop 'checkout Git directory is not absolute'
fi
if ! attributes_file="$("${git[@]}" -C "$checkout" rev-parse --path-format=absolute --git-path info/attributes)"; then
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
  if ! status_output="$("${git[@]}" -C "$checkout" status --porcelain --untracked-files=all)"; then
    stop 'cannot inspect checkout cleanliness'
  fi
  if [[ -n "$status_output" ]]; then
    stop 'checkout is not clean'
  fi
  if [[ "$("${git[@]}" -C "$checkout" symbolic-ref --quiet --short HEAD)" != "$base" ]]; then
    stop 'checkout is not on AGENT_WORK_BASE_BRANCH'
  fi
}
clean
if ! origin_urls="$("${git[@]}" -C "$checkout" config --get-all remote.origin.url)"; then
  stop 'checkout has no origin remote'
fi
if [[ "${origin_urls%%$'\n'*}" != "$origin_urls" ]]; then
  stop 'checkout origin has ambiguous URLs'
fi
origin="$origin_urls"
project_command=("$AGENT_WORK_GLAB" api --hostname "$host" "projects/$project_id")
if [[ -n "${AGENT_WORK_DIRENV:-}" ]]; then
  project_command=("$AGENT_WORK_DIRENV" exec "$checkout" /usr/bin/env "GITLAB_HOST=$host" "GITLAB_PROJECT_ID=$project_id" "${project_command[@]}")
fi
if ! json="$("${project_command[@]}")"; then
  stop 'cannot read configured GitLab project'
fi
if ! actual_id="$("$AGENT_WORK_JQ" -er '.id | if type == "number" then tostring else empty end' <<<"$json")"; then
  stop 'GitLab project response has no numeric id'
fi
if [[ "$actual_id" != "$project_id" ]]; then
  stop 'GitLab project id does not match configured project'
fi
if ! default="$("$AGENT_WORK_JQ" -er '.default_branch | strings | select(length > 0)' <<<"$json")"; then
  stop 'GitLab project response has no default branch'
fi
if ! "${git[@]}" -C "$checkout" check-ref-format --branch "$default" >/dev/null; then
  stop 'GitLab default branch is not valid'
fi
if ! http="$("$AGENT_WORK_JQ" -er '.http_url_to_repo | strings | select(length > 0)' <<<"$json")"; then
  stop 'GitLab project response has no HTTP origin'
fi
if ! ssh="$("$AGENT_WORK_JQ" -er '.ssh_url_to_repo | strings | select(length > 0)' <<<"$json")"; then
  stop 'GitLab project response has no SSH origin'
fi
if [[ "$(normal_url "$origin")" != "$(normal_url "$http")" && "$(normal_url "$origin")" != "$(normal_url "$ssh")" ]]; then
  stop 'origin does not match configured GitLab project'
fi
if [[ "$("${git[@]}" -C "$checkout" config --get "branch.$base.remote")" != origin || "$("${git[@]}" -C "$checkout" config --get "branch.$base.merge")" != "refs/heads/$default" ]]; then
  stop 'checkout upstream is not origin default branch'
fi

remote_ref="refs/remotes/origin/$default"
if [[ "${1:-}" != --apply ]]; then
  if ! lines="$("${git[@]}" -C "$checkout" ls-remote --exit-code origin "refs/heads/$default")"; then
    stop 'cannot read default branch from origin'
  fi
  if ! [[ "$lines" =~ ^[0-9a-f]{40}[[:space:]]refs/heads/ ]] || [[ "${lines%%$'\n'*}" != "$lines" ]]; then
    stop 'origin default branch did not return one valid SHA'
  fi
  if [[ "${lines%%[[:space:]]*}" != "$("${git[@]}" -C "$checkout" rev-parse HEAD)" ]]; then
    stop 'preview checkout is not current with origin default branch'
  fi
else
  if ! "${git[@]}" -C "$checkout" fetch --no-tags --no-recurse-submodules origin "refs/heads/$default:$remote_ref"; then
    stop 'cannot fetch verified default branch'
  fi
  if "${git[@]}" -C "$checkout" grep -q -E '(^|[[:space:]])filter(=|[[:space:]]|$)' "$remote_ref" -- .gitattributes '*/.gitattributes'; then
    stop 'target branch contains filter attributes'
  else
    grep_status="$?"
    if [[ "$grep_status" != 1 ]]; then
      stop 'cannot inspect target Git attributes'
    fi
  fi
  if ! "${git[@]}" -C "$checkout" merge --ff-only "$remote_ref"; then
    stop 'cannot fast-forward checkout to verified default branch'
  fi
fi
clean
if [[ "${1:-}" == --apply ]]; then
  if [[ "$("${git[@]}" -C "$checkout" rev-parse HEAD)" != "$("${git[@]}" -C "$checkout" rev-parse "$remote_ref")" ]]; then
    stop 'checkout HEAD is not the refreshed origin default branch'
  fi
fi
runner=("$AGENT_WORK_UV")
if [[ -n "${AGENT_WORK_DIRENV:-}" ]]; then
  runner=("$AGENT_WORK_DIRENV" exec "$checkout" /usr/bin/env "GITLAB_HOST=$host" "GITLAB_PROJECT_ID=$project_id" "$AGENT_WORK_UV")
fi
cd "$checkout"
if [[ "${1:-}" == --apply ]]; then
  exec "${runner[@]}" run agent-work loop run --apply --json
fi
exec "${runner[@]}" run agent-work loop run --json
