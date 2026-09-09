#!/usr/bin/env bash
set -Eeuo pipefail

readonly log_directory="ci-logs"
readonly result_directory="$log_directory/results"
readonly result_manifest="ci-gates.tsv"
readonly gate_report="ci-gates.xml"
readonly ruff_report="ruff-code-quality-report.json"
readonly yamllint_report="yamllint-code-quality-report.json"
readonly combined_report="code-quality-report.json"

mkdir -p "$result_directory"
: > "$result_manifest"
printf '[]\n' > "$ruff_report"
printf '[]\n' > "$yamllint_report"

run_ruff_check() {
    uv run --no-sync ruff check --output-format=gitlab . > "$ruff_report"
}

run_ruff_format() {
    uv run --no-sync ruff format --check --diff . > format-changes.diff
}

run_tests() {
    local environment="$1"
    local project_environment="$2"

    COVERAGE_FILE=".coverage.${environment}" \
        UV_PROJECT_ENVIRONMENT="$project_environment" \
        uv run --no-sync env -u UV_PROJECT_ENVIRONMENT pytest \
            --cov \
            --cov-fail-under=85 \
            --cov-report="xml:coverage-${environment}.xml" \
            --junitxml="test-results-${environment}.xml" \
            -o "junit_suite_name=${environment}" \
            -o "cache_dir=.cache/pytest/${environment}"
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
        --exclude .ruff_cache \
        --exclude ci-logs \
        --exclude 'ci-gates.*' \
        --exclude 'coverage-*.xml' \
        --exclude '.coverage.*' \
        --exclude 'test-results-*.xml' \
        --exclude '*-code-quality-report.json' \
        --exclude format-changes.diff
}

run_type_check() {
    uv run --no-sync ty check
}

run_shellcheck() {
    local file
    local first_line
    local status=0
    local -a shell_files=()

    mapfile -d '' shell_files < <(
        find . -type f \
            -not -path './.git/*' \
            -not -path './.cache/*' \
            -not -path './.venv/*' \
            -not -path './.tox/*' \
            -not -path './.pytest_cache/*' \
            -not -path './.ruff_cache/*' \
            -not -path './ci-logs/*' \
            -not -name 'ci-gates.*' \
            -not -name 'coverage-*.xml' \
            -not -name '.coverage.*' \
            -not -name 'test-results-*.xml' \
            -not -name '*-code-quality-report.json' \
            -not -name 'format-changes.diff' \
            -print0
    )
    for file in "${shell_files[@]}"; do
        case "$file" in
            *.sh)
                shellcheck "$file" || status=1
                ;;
            *)
                IFS= read -r first_line < "$file" || true
                if [[ "$first_line" =~ ^#!.*(ba)?sh ]]; then
                    shellcheck "$file" || status=1
                fi
                ;;
        esac
    done
    return "$status"
}

run_selected_gate() {
    local name="$1"

    case "$name" in
        ruff-check) run_ruff_check ;;
        ruff-format) run_ruff_format ;;
        pytest-py311) run_tests py311 "$CI_PROJECT_DIR/.venv" ;;
        pytest-py314) run_tests py314 "/tmp/ci-venvs/py314" ;;
        yamllint) run_yamllint ;;
        kingfisher) run_kingfisher ;;
        type-check) run_type_check ;;
        shellcheck) run_shellcheck ;;
        *)
            printf 'Unknown CI gate: %s\n' "$name" >&2
            return 2
            ;;
    esac
}

run_gate() {
    local name="$1"
    local started_ns
    local finished_ns
    local elapsed_ms
    local status

    started_ns=$(date +%s%N)
    if run_selected_gate "$name"; then
        status=0
    else
        status=$?
    fi
    finished_ns=$(date +%s%N)
    elapsed_ms=$(((finished_ns - started_ns) / 1000000))
    printf '%s\t%s\t%d.%03d\n' \
        "$name" "$status" "$((elapsed_ms / 1000))" "$((elapsed_ms % 1000))" \
        > "$result_directory/${name}.tsv"
    return "$status"
}

readonly -a check_names=(
    "ruff-check"
    "ruff-format"
    "pytest-py311"
    "pytest-py314"
    "yamllint"
    "kingfisher"
    "type-check"
    "shellcheck"
)
declare -a check_pids=()

printf 'Starting CI gates in parallel\n'
for name in "${check_names[@]}"; do
    printf 'START %s\n' "$name"
    run_gate "$name" > "$log_directory/${name}.log" 2>&1 &
    check_pids+=("$!")
done

overall_status=0
for index in "${!check_pids[@]}"; do
    if ! wait "${check_pids[$index]}"; then
        overall_status=1
    fi
done

for name in "${check_names[@]}"; do
    result_file="$result_directory/${name}.tsv"
    if [[ -s "$result_file" ]]; then
        cat "$result_file" >> "$result_manifest"
    else
        printf '%s\t125\t0.000\n' "$name" >> "$result_manifest"
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

if ! uv run --no-sync python tests/helpers/ci_gate_report.py \
    < "$result_manifest" > "$gate_report"; then
    overall_status=1
fi

printf 'CI check summary\n'
while IFS=$'\t' read -r name status duration; do
    if (( status == 0 )); then
        printf 'PASS %s (%ss)\n' "$name" "$duration"
    else
        printf 'FAIL %s (exit %s, %ss)\n' "$name" "$status" "$duration"
    fi
done < "$result_manifest"

for name in "${check_names[@]}"; do
    section_time=$(date +%s)
    printf '\e[0Ksection_start:%s:%s[collapsed=true]\r\e[0K%s log\n' \
        "$section_time" "$name" "$name"
    cat "$log_directory/${name}.log"
    section_time=$(date +%s)
    printf '\e[0Ksection_end:%s:%s\r\e[0K\n' "$section_time" "$name"
done

exit "$overall_status"
