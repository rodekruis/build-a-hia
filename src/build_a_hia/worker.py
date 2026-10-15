"""Entry point for Container Apps Jobs.

python -m build_a_hia.worker convert           # process one queued message, then exit
python -m build_a_hia.worker convert --loop    # keep polling (local development)
python -m build_a_hia.worker generate [--loop] # structure proposals and content generation
python -m build_a_hia.worker cleanup           # delete expired and orphaned session data
python -m build_a_hia.worker prefetch-models   # download Docling models (image build)
"""

import argparse
import logging
import sys
import time
from collections.abc import Callable

from .config import Config
from .services.cleanup import CleanupService
from .services.container import build_services
from .services.conversion import DoclingConverter
from .services.generation import GenerationProcessor
from .services.jobs import ConversionProcessor
from .services.model import FoundryModelClient
from .services.settings import Settings
from .services.url_fetch import SafeFetcher

logger = logging.getLogger("build_a_hia.worker")


def _run(process_next: Callable[[], bool], *, loop: bool, poll_seconds: float) -> int:
    while True:
        handled = process_next()
        if not loop:
            if not handled:
                logger.info("No queued work")
            return 0
        if not handled:
            time.sleep(poll_seconds)


def _convert(settings: Settings, *, loop: bool, poll_seconds: float) -> int:
    services = build_services(settings)
    processor = ConversionProcessor(
        table=services.table,
        blobs=services.blobs,
        queue=services.storage.convert_queue,
        sessions=services.sessions,
        settings=settings,
        converter=DoclingConverter(),
        fetcher=SafeFetcher(
            max_bytes=settings.max_url_bytes,
            max_redirects=settings.max_redirects,
            timeout_seconds=settings.url_timeout_seconds,
        ),
    )
    return _run(processor.process_next, loop=loop, poll_seconds=poll_seconds)


def _generate(settings: Settings, *, loop: bool, poll_seconds: float) -> int:
    services = build_services(settings)
    processor = GenerationProcessor(
        table=services.table,
        blobs=services.blobs,
        queue=services.storage.generate_queue,
        sessions=services.sessions,
        structures=services.structures,
        contents=services.contents,
        settings=settings,
        model_factory=lambda: FoundryModelClient(settings),
    )
    return _run(processor.process_next, loop=loop, poll_seconds=poll_seconds)


def _cleanup(settings: Settings) -> int:
    services = build_services(settings)
    report = CleanupService(services.table, services.blobs, services.sessions).run()
    logger.info(
        "Cleanup removed %d expired sessions, %d orphaned partitions, %d orphaned blob folders",
        report.expired_sessions,
        report.orphaned_partitions,
        report.orphaned_blob_sessions,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one worker command.

    Args:
        argv: Command-line arguments; defaults to `sys.argv[1:]`.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(prog="python -m build_a_hia.worker")
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("convert", help="Fetch and convert queued sources")
    convert.add_argument("--loop", action="store_true", help="Keep polling for work")
    convert.add_argument("--poll-seconds", type=float, default=3.0)
    generate = commands.add_parser("generate", help="Propose structures and generate content")
    generate.add_argument("--loop", action="store_true", help="Keep polling for work")
    generate.add_argument("--poll-seconds", type=float, default=3.0)
    commands.add_parser("cleanup", help="Delete expired and orphaned session data")
    commands.add_parser("prefetch-models", help="Download Docling models")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("docling").setLevel(logging.WARNING)
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_object(Config)
    if args.command == "convert":
        return _convert(settings, loop=args.loop, poll_seconds=args.poll_seconds)
    if args.command == "generate":
        return _generate(settings, loop=args.loop, poll_seconds=args.poll_seconds)
    if args.command == "cleanup":
        return _cleanup(settings)
    DoclingConverter().prefetch_models()
    return 0


if __name__ == "__main__":
    sys.exit(main())
