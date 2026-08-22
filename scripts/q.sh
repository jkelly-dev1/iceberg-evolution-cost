#!/usr/bin/env bash
# Run one SQL statement against Trino.
#
# Every invocation writes a jline "unable to create a system terminal" warning
# to stderr that has nothing to do with the query. It is filtered by PATTERN,
# never by discarding stderr: that stream also carries the query failures, so
# dropping it makes a broken statement look like one that returned no rows.
set -uo pipefail
FMT="${FMT:-CSV_UNQUOTED}"
out=$(docker exec iec-trino trino --output-format="$FMT" --execute "$*" 2>&1)
rc=$?
printf '%s\n' "$out" | grep -v -e 'org.jline' -e 'dumb terminal' -e '^WARNING:'
exit $rc
