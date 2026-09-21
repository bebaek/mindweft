from __future__ import annotations

import argparse
import sys
import webbrowser

from mindweft_client.output import print_json
from mindweft_workspace.instances import list_instances, resolve_instance
from mindweft_workspace.local_connection import browser_url, instance_credential


def run_instances_command(args: argparse.Namespace) -> int:
    try:
        if args.instances_command == "list":
            rows = list_instances()
            if args.json:
                print_json(rows)
            elif not rows:
                print(
                    "No registered local instances. Start one with mindweft code --instance NAME."
                )
            else:
                for row in rows:
                    print(f"{row['name']}\t{row['status']}\t{row['url']}\t{row['version']}")
            return 0
        record = resolve_instance(args.name)
        url = browser_url(record, instance_credential(record))
        print(record.api_url + "/console/")
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            print(
                "Could not open a browser; retry instances open from a desktop session.",
                file=sys.stderr,
            )
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
