#!/usr/bin/env python3
"""Delve News collector (no-agent cron job `news`, Delve N1): the feeds the backend lists
in /var/lib/delve-news/sources.json -> ~/.hermes/cards/news/batch-*.json. The work is in
the agentcards plugin (newslib.py); this file only puts it on the path."""
import os
import sys

sys.path.insert(0, os.path.join(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
                                "plugins", "agentcards"))
import newslib  # noqa: E402

sys.exit(newslib.main())
