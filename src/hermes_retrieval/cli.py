from __future__ import annotations

import argparse
import json
import logging
import sys

from .service import RetrievalService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-retrieval")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sync = sub.add_parser("sync")
    sync.add_argument("sources", nargs="*")
    find = sub.add_parser("find-skills")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=8)
    load = sub.add_parser("load-skills")
    load.add_argument("skill_ids", nargs="+")
    recall = sub.add_parser("recall")
    recall.add_argument("query")
    recall.add_argument("--source", action="append", dest="sources")
    recall.add_argument("--limit", type=int, default=8)
    sub.add_parser("serve")
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parser().parse_args()
    if args.command == "serve":
        from .server import main as serve
        serve()
        return
    service = RetrievalService()
    if args.command == "status":
        result = service.status()
    elif args.command == "sync":
        result = service.sync(args.sources or None)
    elif args.command == "find-skills":
        result = service.find_skills(args.query, args.limit)
    elif args.command == "load-skills":
        result = service.load_skills(args.skill_ids)
    elif args.command == "recall":
        result = service.recall(args.query, args.sources, args.limit)
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

