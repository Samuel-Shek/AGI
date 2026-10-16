"""Explicit local operator commands. No automatic installation or model calls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from .policy import GateDenied, ReadSession


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-ceo", description="AI CEO 本地控制与验收工具")
    parser.add_argument("--state", type=Path, required=True, help="独立本地状态目录；不会接管已有数据库")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="查看本地阶段、任务和权限")
    commands.add_parser("replay", help="运行固定合成回放，与业务验收分开")
    observe = commands.add_parser("observe", help="明确授权一次固定来源的无正文观察")
    observe.add_argument("--source", type=Path, required=True)
    serve_parser = commands.add_parser("serve", help="启动有时限的本地查看面板；不自动观察")
    serve_parser.add_argument("--port", type=int, default=0)
    serve_parser.add_argument("--duration-seconds", type=int, default=900)
    serve_parser.add_argument("--url-file", type=Path, help="以0600保存本地面板入口；不在日志显示访问令牌")
    serve_parser.add_argument("--read-source", type=Path, help="可选：明确授权当前面板会话的一次真实观察")
    daemon = commands.add_parser("daemon", help="使用明确批准的文件运行受限持续观察；不安装启动项")
    daemon.add_argument("--grant", type=Path, required=True)
    daemon.add_argument("--activate", action="store_true", help="仅首次明确启用独立新状态；不能解除停机或撤权")
    daemon.add_argument("--port", type=int, default=0)
    daemon.add_argument("--url-file", type=Path, required=True)
    control = commands.add_parser("control")
    control.add_argument("action", choices=["pause", "stop", "revoke", "takeover"])
    control.add_argument("--asset", choices=["sync-center"])
    commands.add_parser("audit", help="核对审计链；不声称能抵御同用户改写整个文件")
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", type=Path, required=True)
    stage = commands.add_parser("stage-service", help="只生成停用的启动项候选，不安装或加载")
    stage.add_argument("--output", type=Path, required=True)
    stage.add_argument("--grant", type=Path, help="候选授权文件；未指定时指向随附的关闭草案")
    args = parser.parse_args(argv)
    from .controller import Controller
    controller = None
    try:
        read_session = None
        if args.command == "observe":
            read_session = ReadSession.for_operator(args.source)
        if args.command == "serve":
            if not 1 <= args.duration_seconds <= 3600 or not 0 <= args.port <= 65535:
                parser.error("面板预览必须限时1至3600秒，端口必须有效")
            if args.read_source:
                read_session = ReadSession.for_operator(args.read_source, reads=1,
                                                       seconds=min(args.duration_seconds, 3600))
        if args.command == "control" and args.action == "stop":
            result = signal_stop(args.state)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        service_grant = None
        if args.command == "daemon":
            if not 0 <= args.port <= 65535:
                raise GateDenied("invalid_port", "端口必须有效。")
            from .service import load_grant
            # Reject an unapproved file before creating any task database.
            service_grant = load_grant(args.grant)
        controller = Controller(args.state, read_session)
        if args.command == "status":
            result = controller.snapshot()
        elif args.command == "observe":
            result = controller.observe()
        elif args.command == "replay":
            result = controller.replay()
        elif args.command == "control":
            result = controller.control(args.action, args.asset)
        elif args.command == "audit":
            result = controller.store.verify_audit()
        elif args.command == "backup":
            result = controller.store.backup(args.destination)
        elif args.command == "stage-service":
            result = stage_service(args.output, args.state, args.grant)
        elif args.command == "daemon":
            result = serve_observer(controller, service_grant, args)
        elif args.command == "serve":
            from .server import serve
            server = serve(controller, port=args.port)
            timer = None
            old_int = old_term = None
            try:
                port = server.server_address[1]
                url = f"http://127.0.0.1:{port}/#token={server.access_token}"
                if args.url_file:
                    write_preview_entry(args.url_file, url)
                    print(json.dumps({"status": "preview_running", "port": port,
                        "duration_seconds": args.duration_seconds, "access_url_saved": True,
                        "automatic_observation": False}, ensure_ascii=False), flush=True)
                else:
                    print(url, flush=True)
                timer = threading.Timer(args.duration_seconds, server.shutdown)
                timer.daemon = True
                timer.start()
                def stop_server(_signum, _frame):
                    threading.Thread(target=server.shutdown, daemon=True).start()
                old_int = signal.signal(signal.SIGINT, stop_server)
                old_term = signal.signal(signal.SIGTERM, stop_server)
                server.serve_forever(poll_interval=0.2)
            finally:
                if timer:
                    timer.cancel()
                server.server_close()
                if old_int is not None:
                    signal.signal(signal.SIGINT, old_int)
                if old_term is not None:
                    signal.signal(signal.SIGTERM, old_term)
            result = {"status": "preview_stopped", "scheduler_installed": False}
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (GateDenied, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "code": getattr(exc, "code", "operation_rejected"),
                          "message": str(exc) if isinstance(exc, GateDenied) else "本地操作无法安全完成；未扩大读取或写入范围。"}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if controller:
            controller.close()


def stage_service(destination: Path, state: Path, grant: Path | None = None):
    """Generate only a disabled candidate. Never issue a grant or install."""
    import plistlib
    project = Path(__file__).resolve().parent.parent
    destination = destination.absolute()
    if any(part in {"LaunchAgents", "LaunchDaemons"} for part in destination.resolve().parts):
        raise GateDenied("installation_not_authorized", "此命令只生成候选文件，不能写入系统启动项目录。")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    document = {
        "Label": "local.ai-ceo.observer",
        "Disabled": True,
        "RunAtLoad": False,
        "KeepAlive": False,
        "WorkingDirectory": str(project),
        "ProgramArguments": [sys.executable, "-B", "-m", "ai_ceo", "--state", str(state.absolute()),
                             "daemon", "--grant", str((grant or project / "examples/observer-grant.draft.json").absolute()),
                             "--url-file", str(state.absolute() / "observer-entry.txt")],
        "ProcessType": "Background",
    }
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        plistlib.dump(document, handle)
    return {"staged": True, "installed": False, "enabled": False,
            "purpose": "disabled_observer_review_only", "recurring_observation_implemented": True,
            "initial_activation_required": True}


def serve_observer(controller, grant, args):
    """One runtime owns the same controller as the authenticated local panel."""
    from .service import ServiceRuntime
    from .server import serve
    runtime = ServiceRuntime(controller, grant, activate=args.activate)
    server = None
    worker = None
    stop = threading.Event()
    errors = []
    try:
        server = serve(controller, port=args.port)
        url = f"http://127.0.0.1:{server.server_address[1]}/#token={server.access_token}"
        write_observer_entry(args.url_file, url)
        print(json.dumps({"status": "observer_running", "port": server.server_address[1],
                          "model_export": False, "production_write": False,
                          "launchd_install_verified": False}, ensure_ascii=False), flush=True)

        def loop():
            try:
                while not stop.is_set():
                    state = runtime.tick()
                    if state["state"] in {"stopped", "frozen", "expired", "exhausted", "clock_rollback"} or (state["state"] == "blocked" and state.get("blocked_reason")):
                        break
                    stop.wait(min(60, grant.interval_seconds))
            except Exception as exc:
                # Keep operational errors local and avoid leaking source data.
                errors.append(type(exc).__name__)
                controller.store.append_audit("observer_runtime_error", {"error_type": type(exc).__name__})
            finally:
                threading.Thread(target=server.shutdown, daemon=True).start()

        def terminate(_signum, _frame):
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        old_int = signal.signal(signal.SIGINT, terminate)
        old_term = signal.signal(signal.SIGTERM, terminate)
        try:
            worker = threading.Thread(target=loop, name="ceo-observer", daemon=True)
            worker.start()
            server.serve_forever(poll_interval=0.2)
        finally:
            stop.set()
            if worker:
                worker.join(timeout=60)
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
        if errors:
            raise GateDenied("observer_runtime_failed", "观察服务异常退出；证据已保留，未绕过边界重试。")
        return {"status": "observer_stopped", "model_calls": 0, "production_changes": 0}
    finally:
        stop.set()
        if server:
            server.server_close()
        runtime.close()


def write_preview_entry(path: Path, url: str):
    """A preview never overwrites an existing local file."""
    from .observers import _safe_directory
    path = path.absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = _safe_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "w") as handle:
            handle.write(url + "\n")
    finally:
        os.close(directory)


def write_observer_entry(path: Path, url: str):
    """Atomically update only our marked, private entry; reject unrelated files."""
    import re
    import secrets
    import stat
    from .observers import _safe_directory, _signature
    path = path.absolute()
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/#token=[A-Za-z0-9_-]{32,128}", url):
        raise GateDenied("invalid_entry", "本地面板入口格式不正确。")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = _safe_directory(path.parent)
    temporary = ".ceo-entry-" + secrets.token_hex(8)
    signature = None
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 512):
                    raise GateDenied("unsafe_entry_file", "拒绝覆盖非本工具的私有入口文件。")
                content = handle.read(513)
                signature = _signature(info)
                if not re.fullmatch(rb"# ai-ceo-local-panel-v1\nhttp://127\.0\.0\.1:[0-9]{1,5}/#token=[A-Za-z0-9_-]{32,128}\n", content):
                    raise GateDenied("unrelated_entry_file", "现有文件不是本工具创建的面板入口，已保留原文件。")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "w") as handle:
            handle.write("# ai-ceo-local-panel-v1\n" + url + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if signature is None:
            # Link with no overwrite so a concurrently created file survives.
            os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.unlink(temporary, dir_fd=directory)
        else:
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if _signature(current) != signature:
                raise GateDenied("entry_changed", "入口文件发生变化，已取消更新。")
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def signal_stop(state: Path):
    """Independent fail-closed stop path; does not need the controller lock."""
    import stat
    path = state.absolute()
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise GateDenied("invalid_state", "只允许停止当前用户的独立私有状态目录。")
    marker = path / ".ai-ceo-state.json"
    if marker.is_symlink() or not marker.is_file():
        raise GateDenied("not_ceo_state", "没有已登记的 AI CEO 状态目录。")
    stop = path / "STOP"
    try:
        fd = os.open(stop, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("operator_stop\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if stop.is_symlink() or not stop.is_file():
            raise GateDenied("invalid_stop_marker", "停机标记异常，未覆盖。")
    return {"stop_requested": True, "new_actions_blocked_on_admission": True,
            "already_started_action": "bounded read or replay may finish; no production or model worker exists",
            "requires_model": False}


if __name__ == "__main__":
    raise SystemExit(main())
