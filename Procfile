web: sh -c 'timeout ${MIGRATE_TIMEOUT_SECONDS:-90} python -u scripts/migrate.py || echo "Migration timed out/failed; continuing startup"; exec gunicorn app:app --bind 0.0.0.0:$PORT'
