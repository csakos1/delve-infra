#!/usr/bin/env python3
"""Ideiglenes cron hibaág-teszt: stderr-üzenet + exit 2, stdout üres. Teszt után törlendő."""
import sys
print("cron_failtest: szimulált parse_failed", file=sys.stderr)
sys.exit(2)
