"""Gunicorn entry point: `gunicorn wsgi:app`.

Do NOT add --preload: poller threads must live inside a worker process, and
threads do not survive gunicorn's fork. One worker wins the flock in
pollers.start() and runs the polling loops; the other serves requests only.
"""

import logging

from app import create_app

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = create_app()
