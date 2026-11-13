"""`endpointer`: open the window (default), or work with captures from the command line."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from endpointer import __version__
from endpointer.app import App
from endpointer.store import Store


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="endpointer",
        description="Capture the API calls a web app makes, keep the ones that matter, and spot drift.",
    )
    p.add_argument("--home", help="workspace folder (default: $ENDPOINTER_HOME or ~/.endpointer)")
    p.add_argument("--version", action="version", version=f"endpointer {__version__}")
    sub = p.add_subparsers(dest="command")

    gui = sub.add_parser("gui", help="open the window (the default)")
    for target in (p, gui):
        target.add_argument(
            "--port", type=int, default=0, help="port for the local UI (default: any free port)"
        )
        target.add_argument(
            "--browser", help="Chrome/Edge/Brave/Chromium to capture with (default: the first one found)"
        )
        target.add_argument(
            "--launch",
            nargs="?",
            const="about:blank",
            metavar="URL",
            help="open the capture browser right away",
        )
        target.add_argument(
            "--attach",
            type=int,
            metavar="PORT",
            help="attach to a browser already running with --remote-debugging-port=PORT",
        )
        target.add_argument(
            "--no-window", action="store_true", help="don't open a window; just print the URL"
        )
        target.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)

    imp = sub.add_parser("import", help="import a HAR file as a capture")
    imp.add_argument("file", type=Path)
    imp.add_argument("--name")

    sub.add_parser("captures", help="list saved captures")

    dr = sub.add_parser("drift", help="compare a capture with your library (or another capture)")
    dr.add_argument("capture", help="capture id (see `endpointer captures`)")
    dr.add_argument("--against", default="library", help="'library' (default) or another capture id")
    dr.add_argument("--json", action="store_true")
    return p


def _open_window(app: App, url: str) -> subprocess.Popen | None:
    """The UI in a small app-style window (its own profile, so it's never captured); else the default browser."""
    if app.browser_path:
        try:
            return subprocess.Popen(
                [
                    app.browser_path,
                    f"--app={url}",
                    f"--user-data-dir={app.store.home / 'ui-profile'}",
                    "--window-size=560,900",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass
    webbrowser.open(url)
    return None


def run_gui(args: argparse.Namespace, store: Store) -> int:
    from endpointer.server import Server

    app = App(store, browser=args.browser, headless=args.headless)
    server = Server(app, args.port)
    print(f"Endpointer is running at {server.url}  (workspace: {store.home})")
    if not app.browser_path:
        print(
            "No Chrome/Edge/Brave/Chromium found: set ENDPOINTER_BROWSER, attach to a running one, or import HAR files."
        )
    if args.attach:
        app.attach_browser(args.attach)
    elif args.launch:
        threading.Thread(target=app.launch_browser, args=(args.launch,), daemon=True).start()
    window = None if args.no_window else _open_window(app, server.url)
    print("Press Ctrl-C to quit.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
        app.shutdown()
        if window and window.poll() is None:
            window.terminate()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = Store(args.home)
    if args.command in (None, "gui"):
        return run_gui(args, store)
    app = App(store, browser=getattr(args, "browser", None))
    try:
        if args.command == "import":
            meta = app.import_har(
                json.loads(args.file.read_text(encoding="utf-8")), args.name or args.file.stem
            )
            print(f"Imported {meta['calls']} calls as {meta['id']}")
        elif args.command == "captures":
            for c in store.captures():
                print(f"{c['id']}  {c['calls']:>5} calls  {c['name']}")
        elif args.command == "drift":
            report = app.drift(args.capture, args.against)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(
                    f"{args.capture} against {report['against_label']}: "
                    + ", ".join(f"{n} {k}" for k, n in report["summary"].items())
                )
                for r in report["results"]:
                    if r["level"] == "same":
                        continue
                    print(f"  {r['level']:<9} {r['key']}")
                    for line in (
                        r["notes"]
                        + [d["text"] for d in r["response"] if d["kind"] != "unverified"]
                        + [f"request {d['text']}" for d in r["request"]]
                    ):
                        print(f"            {line}")
            return 1 if report["summary"].get("failing") or report["summary"].get("changed") else 0
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
