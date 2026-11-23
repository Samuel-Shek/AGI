#!/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
"""Manual entry point. Explicit handoffs use the existing Core controller."""
import sys
sys.dont_write_bytecode = True
import argparse
import os
import threading
import time
from pathlib import Path
from psi_sync.core_reader import CoreReader, ReadError
from psi_sync.storage import LocalStore, StorageError
from psi_sync.state import State
from psi_sync.reviews import Reviews
from psi_sync.http_server import make_server


def main(argv=None, *, on_bound=None, allow_shutdown=True):
    parser = argparse.ArgumentParser(description='PSI 事项工作台（本机只读服务）')
    parser.add_argument('--fixture', type=Path, help='只使用合成 JSON，不调用 Core')
    parser.add_argument('--data-dir', type=Path, default=Path('~/Library/Application Support/PSISyncCenter').expanduser(), help='私有本机记录目录，必须在 Core 之外')
    parser.add_argument("--standalone", action="store_true")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--instance", default="")
    args = parser.parse_args(argv)
    server = None
    try:
        reader = CoreReader(fixture=args.fixture)
        store = LocalStore(args.data_dir, reader.cache_identity, forbidden=[Path(__file__).resolve().parents[2] / 'core'])
        # Each entry uses only the review file in its own private workbench directory.
        # Existing embedded stores without a review file keep their original workflow.
        reviews = Reviews(store.directory, local_evidence_only=not args.standalone)
        reviewed_view = args.standalone or os.path.lexists(reviews.path)
        state = State(reader, store, reviews=reviews if reviewed_view else None)
        # Bind before reading Core: an occupied port must exit without touching it.
        server = make_server(state, port=args.port, instance=args.instance, allow_shutdown=allow_shutdown) if args.standalone else make_server(state)
        if on_bound is not None:
            on_bound()
        parent = int(os.environ.get('PSI_APP_PARENT_PID', str(os.getppid())))
        if not args.standalone and parent != os.getppid():
            raise ValueError('parent_exited')
        if not args.standalone and os.environ.get('PSI_APP_INSTANCE'):
            def watch_parent():
                while os.getppid() == parent:
                    time.sleep(1)
                os._exit(0)
            threading.Thread(target=watch_parent, daemon=True).start()
        state.refresh()
        print(f'PSI 问题与进展：http://127.0.0.1:{args.port}（仅本机；Ctrl+C 退出）', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, ReadError, StorageError):
        print('启动失败：请检查端口是否被占用、夹具格式及数据目录权限。未停止任何现有进程。', file=sys.stderr)
        return 1
    finally:
        if server:
            server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
