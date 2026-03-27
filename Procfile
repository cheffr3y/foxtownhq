web: sh -c 'timeout ${MIGRATE_TIMEOUT_SECONDS:-90} python -u scripts/migrate.py && exec gunicorn app:app --bind 0.0.0.0:$PORT'
