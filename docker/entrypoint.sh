#!/bin/sh
# Container entrypoint.
#   serve   (default) apply migrations, index the profile, start the API + dashboard
#   ingest  run the daily pipeline once
#   eval    run the eval suite
#   *       any other jobfit command, e.g. `docker compose run app profile search "python"`
set -e
case "$1" in
  serve)
    jobfit db migrate
    jobfit profile index   # cheap: only re-embeds chunks whose text or model changed
    exec jobfit serve --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  *)
    exec jobfit "$@"
    ;;
esac
