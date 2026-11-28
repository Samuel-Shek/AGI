"""Private, bounded, atomic local records. Adapted from PSILocalWeb Answers."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid

MAX_FILE = 8 * 1024 * 1024


class StorageError(ValueError):
    pass


class DraftConflict(ValueError):
    def __init__(self, draft, revision):
        super().__init__('draft_conflict')
        self.draft = draft
        self.revision = revision


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_directory(directory, forbidden=()):
    path = Path(directory).expanduser().absolute()
    resolved = path.resolve()
    if any(part.lower() == 'core' for part in resolved.parts):
        raise StorageError('data_directory_in_core')
    roots = [Path('~/Library/Application Support/PSI/core').expanduser(), *map(Path, forbidden)]
    if any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots):
        raise StorageError('data_directory_in_core')
    for parent in (path, *path.parents):
        if parent.is_symlink():
            if str(parent) in {'/tmp', '/var'} and parent.lstat().st_uid == 0 and str(parent.resolve()) == '/private' + str(parent):
                continue
            raise StorageError('unsafe_storage')
    return path


def store_draft(data, identifier, content, edit):
    """Order same-page revisions, and reject writes based on a superseded page."""
    revisions = data.setdefault('draft_versions', {})
    current = revisions.get(identifier, '')
    if edit is None:
        if current and data['drafts'].get(identifier, '') != content:
            raise DraftConflict(data['drafts'].get(identifier, ''), current)
        data['drafts'][identifier] = content
        return {'saved': True}
    if not isinstance(edit, dict):
        raise ValueError('invalid_draft_revision')
    writer, seq, base = edit.get('writer'), edit.get('sequence'), edit.get('base')
    if (not isinstance(writer, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', writer) or type(seq) is not int
            or not 0 < seq < 2**53 or not isinstance(base, str) or len(base) > 100):
        raise ValueError('invalid_draft_revision')
    revision = f'{writer}:{seq}'
    if revision == current and data['drafts'].get(identifier, '') == content:
        return {'saved': True, 'revision': revision}
    current_writer, _, current_seq = current.rpartition(':')
    if (current_writer == writer and seq <= int(current_seq)) or (current_writer != writer and base != current):
        raise DraftConflict(data['drafts'].get(identifier, ''), current)
    data['drafts'][identifier] = content
    revisions[identifier] = revision
    return {'saved': True, 'revision': revision}


def clear_draft(data, identifier):
    data['drafts'][identifier] = ''
    if identifier in data.get('draft_versions', {}):
        data['draft_versions'][identifier] = f'{uuid.uuid4()}:1'


class LocalStore:
    def __init__(self, directory, source, forbidden=()):
        self.directory = validate_directory(directory, forbidden)
        self.path = self.directory / ('local-' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.json')
        self.lock = threading.RLock()
        self.data = {'schema': 1, 'messages': {}, 'actions': [], 'drafts': {}}
        self._check_directory(create=True)
        if self.path.exists() or self.path.is_symlink():
            self._check_file()
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                self._check_stat(os.fstat(stream.fileno()))
                raw = stream.read(MAX_FILE + 1)
            try:
                value = json.loads(raw)
                if len(raw) > MAX_FILE or value.get('schema') != 1 or not isinstance(value.get('messages'), dict) or not isinstance(value.get('actions'), list) or not isinstance(value.get('drafts'), dict):
                    raise ValueError()
                for key, messages in value['messages'].items():
                    if not isinstance(key, str) or not isinstance(messages, list):
                        raise ValueError()
                    for row in messages:
                        if row.get('author') != 'user' or not all(isinstance(row.get(k), str) for k in ('content', 'created_at', 'request_id')):
                            raise ValueError()
                if any(not isinstance(k, str) or not isinstance(v, str) for k, v in value['drafts'].items()):
                    raise ValueError()
                versions = value.get('draft_versions', {})
                if (not isinstance(versions, dict) or any(not isinstance(k,str) or k not in value['drafts']
                        or not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}:[1-9][0-9]{0,15}',v)
                        for k,v in versions.items())):
                    raise ValueError()
                for row in value['actions']:
                    if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ('id', 'action', 'created_at', 'request_id')):
                        raise ValueError()
                self.data = value
            except (ValueError, TypeError, AttributeError, RecursionError) as exc:
                raise StorageError('invalid_storage_preserved') from exc

    def _check_directory(self, create=False):
        validate_directory(self.directory)
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise StorageError('unsafe_directory_permissions')

    @staticmethod
    def _check_stat(info):
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise StorageError('unsafe_storage')

    def _check_file(self):
        if self.path.is_symlink():
            raise StorageError('unsafe_storage')
        if self.path.exists():
            self._check_stat(self.path.lstat())

    def read(self):
        with self.lock:
            return deepcopy(self.data)

    def update(self, mutate):
        with self.lock:
            updated = deepcopy(self.data)
            result = mutate(updated)
            encoded = json.dumps(updated, ensure_ascii=False, allow_nan=False).encode()
            if len(encoded) > MAX_FILE:
                raise StorageError('storage_full')
            self._check_directory()
            self._check_file()
            temporary = None
            committed = False
            try:
                fd, temporary = tempfile.mkstemp(prefix='.local-', dir=self.directory)
                with os.fdopen(fd, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                temporary = None
                # Rename is the commit point. Keep memory consistent even if the
                # following durability acknowledgement fails.
                self.data = updated
                committed = True
                dfd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError as exc:
                raise StorageError('storage_durability_unconfirmed' if committed else 'storage_unavailable') from exc
            finally:
                if temporary:
                    os.unlink(temporary)
            self.data = updated
            return deepcopy(result)
