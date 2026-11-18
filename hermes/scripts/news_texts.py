#!/usr/bin/env python3
"""Delve News article texts (no-agent cron job `news-text`, Delve N1c): the stories the
backend lists in /var/lib/delve-news/wanted.json -> ~/.hermes/cards/news/text-*.json. The
work is in the agentcards plugin (newslib.texts_main, newstext.py in the renderer's venv);
this file only puts it on the path."""
import os
import sys

sys.path.insert(0, os.path.join(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
                                "plugins", "agentcards"))
import newslib  # noqa: E402

sys.exit(newslib.texts_main(sys.argv[1:]))
