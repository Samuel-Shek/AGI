#!/usr/bin/env python3
"""Start the local PSI web workbench with synthetic data or an explicit Core."""
import sys
sys.dont_write_bytecode = True

import argparse
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from psi_sync.core_reader import CoreReader, ReadError
from psi_sync.http_server import make_server
from psi_sync.reviews import Reviews
from psi_sync.security import HOST, PORT
from psi_sync.state import State
from psi_sync.storage import LocalStore, StorageError

WEB = Path(__file__).resolve().parent
DEMO = WEB / 'tests' / 'fixtures' / 'demo.json'


def main(argv=None):
    parser = argparse.ArgumentParser(description='PSI 本地网页工作台，默认展示虚构事项。')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--demo', action='store_true', help='使用随源码提供的示例（默认）')
    source.add_argument('--fixture', type=Path, help='从指定的合成 JSON 数据读取事项')
    source.add_argument('--live', action='store_true', help='连接此用户已单独配置并授权的本机 Core')
    parser.add_argument('--data-dir', type=Path, help='保存工作台草稿的私有目录')
    parser.add_argument('--standalone', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args(argv)

    try:
        with ExitStack() as resources:
            fixture = None if args.live else (args.fixture or DEMO)
            directory = args.data_dir
            if directory is None:
                if args.live:
                    directory = Path.home() / 'Library/Application Support/PSISyncCenter'
                else:
                    directory = Path(resources.enter_context(TemporaryDirectory(prefix='psi-demo-'))).resolve()
            reader = CoreReader(fixture=fixture)
            store = LocalStore(directory, reader.cache_identity, forbidden=[WEB.parent / 'core'])
            # Review files may refer to operator evidence. A fixture never opens them.
            reviews = Reviews(store.directory) if args.live else None
            state = State(reader, store, reviews=reviews)
            server = resources.enter_context(make_server(state, port=args.port))
            state.refresh()
            mode = '本机 Core' if args.live else '示例数据'
            print(f'PSI · {mode}  http://{HOST}:{args.port}/  （Ctrl+C 退出）', flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, ReadError, StorageError):
        print('启动失败。请检查端口、数据文件及私有目录权限。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
