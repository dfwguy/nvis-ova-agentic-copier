#!/usr/bin/env bash
set -Eeuo pipefail

readonly log_directory="ci-logs"
readonly ruff_report="ruff-code-quality-report.json"
readonly yamllint_report="yamllint-code-quality-report.json"
readonly combined_report="code-quality-report.json"

mkdir -p "$log_directory"
printf '[]\n' > "$ruff_report"
printf '[]\n' > "$yamllint_report"

run_ruff_check() {
    uv run --no-sync ruff check --output-format=gitlab . > "$ruff_report"
}

run_ruff_format() {
    uv run --no-sync ruff format --check --diff . > format-changes.diff
}

run_tests() {
    uv run --no-sync tox run-parallel
}

run_yamllint() {
    uv run --no-sync yamllint \
        -d '{"extends": "default", "rules": {"line-length": "disable", "document-start": "disable"}}' \
        -f parsable . \
        | uv run --no-sync python tests/helpers/yamllint_to_json.py \
            > "$yamllint_report"
}

run_kingfisher() {
    uv run --no-sync kingfisher scan . \
        --git-history none \
        --no-validate \
        --no-update-check \
        --exclude .git \
        --exclude .cache \
        --exclude .venv \
        --exclude .tox \
        --exclude .pytest_cache \
        --exclude .ruff_cache
}

run_type_check() {
    uv run --no-sync ty check
}

run_shellcheck() {
    local file
    local first_line
    local -a shell_files=()

    mapfile -d '' shell_files < <(
        find . -type f \
            -not -path './.git/*' \
            -not -path './.cache/*' \
            -not -path './.venv/*' \
            -not -path './.tox/*' \
            -not -path './.pytest_cache/*' \
            -not -path './.ruff_cache/*' \
            -print0
    )
    for file in "${shell_files[@]}"; do
        case "$file" in
            *.sh)
                shellcheck "$file"
                ;;
            *)
                IFS= read -r first_line < "$file" || true
                if [[ "$first_line" =~ ^#!.*(ba)?sh ]]; then
                    shellcheck "$file"
                fi
                ;;
        esac
    done
}

declare -a check_names=()
declare -a check_pids=()
declare -a check_statuses=()

check_names+=("ruff-check")
run_ruff_check > "$log_directory/ruff-check.log" 2>&1 &
check_pids+=("$!")
check_names+=("ruff-format")
run_ruff_format > "$log_directory/ruff-format.log" 2>&1 &
check_pids+=("$!")
check_names+=("pytest-py311-py314")
run_tests > "$log_directory/pytest-py311-py314.log" 2>&1 &
check_pids+=("$!")
check_names+=("yamllint")
run_yamllint > "$log_directory/yamllint.log" 2>&1 &
check_pids+=("$!")
check_names+=("kingfisher")
run_kingfisher > "$log_directory/kingfisher.log" 2>&1 &
check_pids+=("$!")
check_names+=("type-check")
run_type_check > "$log_directory/type-check.log" 2>&1 &
check_pids+=("$!")
check_names+=("shellcheck")
run_shellcheck > "$log_directory/shellcheck.log" 2>&1 &
check_pids+=("$!")

overall_status=0
for index in "${!check_pids[@]}"; do
    if wait "${check_pids[$index]}"; then
        check_statuses+=(0)
    else
        check_statuses+=("$?")
        overall_status=1
    fi
done

temporary_report="${combined_report}.tmp"
if jq -s 'add' "$ruff_report" "$yamllint_report" > "$temporary_report"; then
    mv "$temporary_report" "$combined_report"
else
    printf '[]\n' > "$combined_report"
    rm -f "$temporary_report"
    overall_status=1
fi

printf 'CI check summary\n'
for index in "${!check_names[@]}"; do
    if (( check_statuses[index] == 0 )); then
        printf 'PASS %s\n' "${check_names[$index]}"
    else
        printf 'FAIL %s (exit %s)\n' \
            "${check_names[$index]}" "${check_statuses[$index]}"
        sed 's/^/  /' "$log_directory/${check_names[$index]}.log"
    fi
done

exit "$overall_status"
