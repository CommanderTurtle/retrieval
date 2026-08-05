from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import re
import signal
import sys
import threading

from .catalog import IweCatalog
from .config import Settings
from .context_audit import audit_context
from .projection import SkillProjection, integrate_harnesses
from .service import RetrievalService
from .skill_admin import SkillAdmin, SkillAdminError
from .source_admin import SourceRegistry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-retrieval")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sync = sub.add_parser("sync")
    sync.add_argument("sources", nargs="*")
    search = sub.add_parser(
        "search",
        help="fused semantic, fuzzy-title, and BM25 search of dormant skills",
    )
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--json", action="store_true", dest="as_json")
    retrieve = sub.add_parser(
        "retrieve",
        help="run the isolated scout and project at most one selected skill",
    )
    retrieve.add_argument("query")
    projected = sub.add_parser("projected")
    projected_commands = projected.add_subparsers(
        dest="projected_command", required=True
    )
    projected_commands.add_parser("list")
    projected_clear = projected_commands.add_parser("clear")
    projected_clear.add_argument("skill_ids", nargs="*")
    catalog = sub.add_parser("catalog")
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_commands.add_parser("sync")
    catalog_commands.add_parser("stats")
    catalog_audit = catalog_commands.add_parser(
        "audit",
        help="review category assignments without changing sources or MCP tools",
    )
    catalog_audit.add_argument("path", nargs="?")
    catalog_audit.add_argument("--name")
    catalog_audit.add_argument("--json", action="store_true", dest="as_json")
    catalog_register = catalog_commands.add_parser(
        "register",
        help="register and synchronize a reviewed external skill directory",
    )
    catalog_register.add_argument("name")
    catalog_register.add_argument("path")
    catalog_register.add_argument(
        "--state", choices=["cold", "archived"], default="cold"
    )
    catalog_register.add_argument("--dry-run", action="store_true")
    context = sub.add_parser("context", help="read-only context-file maintenance")
    context_commands = context.add_subparsers(dest="context_command", required=True)
    context_audit = context_commands.add_parser(
        "audit",
        help="report context size, precedence, imports, duplicates, and optional drift",
    )
    context_audit.add_argument("targets", nargs="*")
    context_audit.add_argument("--baseline")
    context_audit.add_argument("--json", action="store_true", dest="as_json")
    references = sub.add_parser(
        "references",
        help="retrieve optional Markdown reference sections without activating rules",
    )
    references.add_argument("query")
    references.add_argument("--limit", type=int, default=3)
    references.add_argument("--max-chars", type=int, default=8000)
    sub.add_parser(
        "integrate",
        help="register the shared projection directory with Hermes and OMP",
    )
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
    sub.add_parser("serve")
    sub.add_parser(
        "watch",
        help="keep configured sources synchronized independently of an MCP client",
    )
    return parser


def _run_skills(args: argparse.Namespace) -> None:
    try:
        settings = Settings.load()
        admin = SkillAdmin(settings.sources())
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
        sum(1 for source in service.sources if source.enabled),
    )
    try:
        while not stopping.wait(1.0):
            pass
    finally:
        service.stop_watcher()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _print_catalog_audit(report: dict[str, object]) -> None:
    print(
        "Catalog audit: "
        f"{report['approved']} approved, "
        f"{report['review_required']} require review, "
        f"{report['native_excluded']} native exclusions."
    )
    categories = report.get("categories") or {}
    if isinstance(categories, dict) and categories:
        print("Categories:")
        for category, count in categories.items():
            print(f"  {category}: {count}")
    review = report.get("review") or []
    if isinstance(review, list) and review:
        print("Review required (not graphed or vectorized):")
        for row in review:
            if not isinstance(row, dict):
                continue
            print(f"  {row['skill_id']}\n    {row['path']}")
        print(
            "Add exact IDs to category-overrides.toml using categories from "
            "taxonomy.toml, then rerun catalog audit and sync."
        )


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
    if args.command == "integrate":
        print(json.dumps(integrate_harnesses(Settings.load()), indent=2, sort_keys=True))
        return
    if args.command == "context":
        targets = [Path(value) for value in args.targets] or [Path.cwd()]
        result = audit_context(
            targets,
            baseline=Path(args.baseline) if args.baseline else None,
        )
        if args.as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        summary = result["summary"]
        print(
            "Context audit: "
            f"{summary['files']} files, ~{summary['estimated_tokens']} audited tokens, "
            f"{summary['review_for_reference']} reference candidates, "
            f"{summary['potential_shadowed']} precedence warnings."
        )
        for row in result["files"]:
            print(
                f"  {row['provider']:<18} ~{row['estimated_tokens']:>6} tokens  "
                f"{row['recommendation']}\n    {row['path']}"
            )
        print("Use --json for section, import, shadowing, duplicate, and baseline details.")
        return
    if args.command == "catalog":
        settings = Settings.load()
        catalog = IweCatalog(settings, settings.sources())
        if args.catalog_command == "sync":
            result = catalog.sync()
        elif args.catalog_command == "stats":
            result = catalog.stats()
        elif args.catalog_command == "audit":
            if args.path:
                default_name = re.sub(
                    r"[^a-z0-9]+", "-", Path(args.path).name.casefold()
                ).strip("-") or "skill-intake"
                result = catalog.audit_path(
                    Path(args.path), args.name or default_name
                )
            else:
                result = catalog.audit()
            if not args.as_json:
                _print_catalog_audit(result)
                return
        elif args.catalog_command == "register":
            result = SourceRegistry(settings).register(
                args.name,
                Path(args.path),
                state=args.state,
                dry_run=args.dry_run,
            )
        else:
            raise AssertionError(args.catalog_command)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.command == "projected":
        projection = SkillProjection(Settings.load())
        result = (
            projection.list()
            if args.projected_command == "list"
            else projection.clear(args.skill_ids or None)
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    service = RetrievalService()
    if args.command == "status":
        result = service.status()
    elif args.command == "sync":
        result = service.sync(args.sources or None)
    elif args.command == "search":
        result = service.search_skills(args.query, args.limit)
        if not args.as_json:
            for position, row in enumerate(result["matches"], start=1):
                categories = ", ".join(row.get("categories") or [])
                print(
                    f"{position:>2}. {row['name']}  [{row['state']}]\n"
                    f"    {row['skill_id']}\n"
                    f"    {row['description']}\n"
                    f"    categories: {categories or '-'}"
                )
            return
    elif args.command == "retrieve":
        result = service.retrieve_skill(args.query)
    elif args.command == "references":
        result = service.retrieve_reference(
            args.query,
            limit=args.limit,
            max_chars=args.max_chars,
        )
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
