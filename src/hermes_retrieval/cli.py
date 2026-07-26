from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from .config import Settings
from .service import RetrievalService
from .skill_admin import SkillAdmin, SkillAdminError


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
    find_workflows = sub.add_parser("find-workflows")
    find_workflows.add_argument("query")
    find_workflows.add_argument(
        "--type",
        action="append",
        choices=["agent", "command", "hook"],
        dest="workflow_types",
    )
    find_workflows.add_argument("--limit", type=int, default=8)
    load_workflows = sub.add_parser("load-workflows")
    load_workflows.add_argument("workflow_ids", nargs="+")
    recall = sub.add_parser("recall")
    recall.add_argument("query")
    recall.add_argument("--source", action="append", dest="sources")
    recall.add_argument("--limit", type=int, default=8)
    recall.add_argument("--before", type=int, default=2)
    recall.add_argument("--after", type=int, default=3)
    skills = sub.add_parser("skills")
    skill_commands = skills.add_subparsers(dest="skills_command", required=True)
    skill_list = skill_commands.add_parser("list")
    skill_list.add_argument("--json", action="store_true", dest="as_json")
    skill_inspect = skill_commands.add_parser("inspect")
    skill_inspect.add_argument("skill_id")
    skill_edit = skill_commands.add_parser("edit")
    skill_edit.add_argument("skill_id")
    skill_archive = skill_commands.add_parser("archive")
    skill_archive.add_argument("skill_id")
    skill_restore = skill_commands.add_parser("restore")
    skill_restore.add_argument("skill_id")
    sub.add_parser("serve")
    sub.add_parser(
        "watch",
        help="keep configured sources synchronized independently of an MCP client",
    )
    return parser


def _run_skills(args: argparse.Namespace) -> None:
    try:
        settings = Settings.load()
        admin = SkillAdmin(settings.sources(), settings.skill_archive_root)
        if args.skills_command == "list":
            rows = admin.list()
            if args.as_json:
                print(json.dumps(rows, indent=2, sort_keys=True))
                return
            if not rows:
                print("No configured skills were found.")
                return
            print(f"{'STATE':<10} {'MODIFIED (UTC)':<25} {'NAME':<28} EXACT ID")
            for row in rows:
                modified = str(row.get("modified_at") or "-")
                if modified != "-":
                    modified = modified.replace("+00:00", "Z")
                print(
                    f"{str(row['state']):<10} "
                    f"{modified[:24]:<25} "
                    f"{str(row.get('name') or '-')[:27]:<28} "
                    f"{row['skill_id']}"
                )
            return
        if args.skills_command == "inspect":
            row = admin.inspect(args.skill_id)
            content = str(row.pop("content"))
            print(json.dumps(row, indent=2, sort_keys=True))
            print("\n--- SKILL.md ---\n")
            print(content)
            return
        if args.skills_command == "edit":
            return_code = admin.edit(args.skill_id)
            if return_code:
                raise SystemExit(return_code)
            return
        if args.skills_command == "archive":
            print(
                json.dumps(
                    admin.archive(args.skill_id),
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        if args.skills_command == "restore":
            print(
                json.dumps(
                    admin.restore(args.skill_id),
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        raise AssertionError(args.skills_command)
    except (SkillAdminError, OSError, ValueError) as exc:
        print(f"hermes-retrieval: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _run_watcher() -> None:
    service = RetrievalService()
    if not service.settings.watch_enabled:
        print(
            "hermes-retrieval: source watching is disabled by "
            "RETRIEVAL_WATCH_ENABLED",
            file=sys.stderr,
        )
        raise SystemExit(2)

    stopping = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logging.getLogger(__name__).info(
            "Stopping Retrieval watcher after signal %s",
            signum,
        )
        stopping.set()

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    service.start_watcher()
    logging.getLogger(__name__).info(
        "Retrieval watcher is active for %d configured sources",
        len(service.sources),
    )
    try:
        while not stopping.wait(1.0):
            pass
    finally:
        service.stop_watcher()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _parser().parse_args()
    if args.command == "serve":
        from .server import main as serve
        serve()
        return
    if args.command == "watch":
        _run_watcher()
        return
    if args.command == "skills":
        _run_skills(args)
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
    elif args.command == "find-workflows":
        result = service.find_workflows(
            args.query,
            args.workflow_types,
            args.limit,
        )
    elif args.command == "load-workflows":
        result = service.load_workflows(args.workflow_ids)
    elif args.command == "recall":
        result = service.recall(
            args.query,
            args.sources,
            args.limit,
            args.before,
            args.after,
        )
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
