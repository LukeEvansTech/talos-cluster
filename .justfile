set positional-arguments := true
set quiet := true
set shell := ['bash', '-euo', 'pipefail', '-c']

mod bootstrap './bootstrap'

[private]
default:
    @just --list

[private]
log level message *args:
    #!/usr/bin/env bash
    set -euo pipefail
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    formatted_args=""
    for arg in "$@"; do
        if [[ "$arg" == *"="* ]]; then
            key="${arg%%=*}"
            value="${arg#*=}"
            formatted_args+=" $(gum style --bold --foreground=240 "${key}=")\"${value}\""
        else
            formatted_args+=" ${arg}"
        fi
    done
    color_code=""
    case "{{ level }}" in
        "debug") color_code="63" ;;
        "info")  color_code="87" ;;
        "warn")  color_code="192" ;;
        "error") color_code="198" ;;
        "fatal") color_code="198" ;;
    esac
    gum style --bold --foreground="${color_code}" "{{ level }}" | \
        xargs -I {} echo "${timestamp} {} {{ message }}${formatted_args}"
    if [[ "{{ level }}" == "fatal" ]]; then
        exit 1
    fi

[private]
template file:
    #!/usr/bin/env bash
    set -euo pipefail
    minijinja-cli "{{ file }}" | op inject
