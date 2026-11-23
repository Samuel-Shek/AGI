"""Bounded JSON projection for an authenticated current-turn desktop read.

Conversation bodies are lexically validated and discarded in input blocks.
Only the existing snapshot's identity, ordered history and progress survive.
This is an opt-in transport budget, not an authorization or progress cache.
"""
from __future__ import annotations

import codecs
import json
import re
import time


class MetadataError(ValueError):
    pass


class MetadataBudget:
    FRAME = 16 * 1024 * 1024
    TOTAL = 32 * 1024 * 1024
    BLOCK = 64 * 1024

    def __init__(self):
        self.deadline = time.monotonic() + 20
        self.received = 0

    def check(self, deadline):
        remaining = min(deadline, self.deadline) - time.monotonic()
        if remaining <= 0:
            raise MetadataError('codex_metadata_timeout')
        return remaining

    def receive(self, sock, size, deadline):
        if size > self.TOTAL - self.received:
            raise MetadataError('codex_metadata_wire_limit')
        sock.settimeout(self.check(deadline))
        value = sock.recv(min(size, self.BLOCK))
        if not value:
            raise MetadataError('codex_ipc_closed')
        self.received += len(value)
        self.check(deadline)
        return value


_ANY = object()
_TURN = {'id': True, 'turnId': True, 'status': True}
_STATE = {
    'id': True, 'cwd': True, 'latestTurnStartMessageId': True,
    'threadRuntimeStatus': {'type': True},
    'turns': (_TURN,),
    'turnHistory': {'history': {
        'entitiesByKey': {_ANY: _TURN},
        'islands': ({'newerBoundary': {'status': True},
                     'entries': ({'value': True},)},),
    }},
}
SNAPSHOT = {'method': True, 'sourceClientId': True, 'version': True,
            'params': {'conversationId': True,
                       'change': {'type': True, 'conversationState': _STATE}}}
_SPECIAL = re.compile(r'["\\\x00-\x1f]')
_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')


class Projection:
    MAX_RETAINED = 1024 * 1024
    MAX_NODES = 200000
    MAX_DEPTH = 64
    MAX_ENTRIES = 4096

    def __init__(self, sock, size, budget, deadline):
        if size <= 0 or size > budget.FRAME:
            raise MetadataError('codex_metadata_frame_limit')
        self.sock, self.left, self.budget, self.deadline = sock, size, budget, deadline
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.buf, self.pos = '', 0
        self.nodes = self.retained = 0
        self.invalid_shape = False

    def charge(self, count):
        self.retained += count
        if self.retained > self.MAX_RETAINED:
            raise MetadataError('codex_metadata_retained_limit')

    def peek(self):
        self.budget.check(self.deadline)
        while self.pos == len(self.buf) and self.left:
            raw = self.budget.receive(self.sock, min(self.left, self.budget.BLOCK), self.deadline)
            self.left -= len(raw)
            self.buf = self.decoder.decode(raw, final=self.left == 0)
            self.pos = 0
        return self.buf[self.pos:self.pos + 1]

    def take(self):
        value = self.peek()
        if not value:
            raise MetadataError('codex_metadata_truncated')
        self.pos += 1
        return value

    def whitespace(self):
        while self.peek() and self.peek() in ' \r\n\t':
            self.pos += 1

    def string(self, capture, limit=65536):
        if self.take() != '"':
            raise MetadataError('codex_metadata_string')
        chunks = []
        length = 0
        def save(text):
            nonlocal length
            if capture:
                length += len(text)
                if length > limit:
                    raise MetadataError('codex_metadata_string_limit')
                chunks.append(text)
        while True:
            if not self.peek():
                raise MetadataError('codex_metadata_truncated')
            match = _SPECIAL.search(self.buf, self.pos)
            end = match.start() if match else len(self.buf)
            save(self.buf[self.pos:end] if capture else '')
            self.pos = end
            if match is None:
                continue
            char = self.take()
            if char == '"':
                return json.loads('"' + ''.join(chunks) + '"') if capture else None
            if char != '\\':
                raise MetadataError('codex_metadata_control_character')
            escaped = self.take()
            save('\\' + escaped)
            if escaped == 'u':
                digits = ''.join(self.take() for _ in range(4))
                if any(c not in '0123456789abcdefABCDEF' for c in digits):
                    raise MetadataError('codex_metadata_escape')
                save(digits)
            elif escaped not in '"\\/bfnrt':
                raise MetadataError('codex_metadata_escape')

    def value(self, selector, depth=0):
        self.nodes += 1
        if depth > self.MAX_DEPTH or self.nodes > self.MAX_NODES:
            raise MetadataError('codex_metadata_structure_limit')
        self.whitespace()
        char = self.peek()
        keep = selector is not None
        if keep:
            self.charge(64)  # container/scalar overhead, in addition to strings
        if ((selector is True and char in ('{', '['))
                or (isinstance(selector, dict) and char != '{')
                or (isinstance(selector, tuple) and char != '[')):
            # A different event can have the same field names with a different
            # schema. Validate and discard its value, retaining the key so a
            # duplicate still fails. The caller rejects malformed target frames
            # only after the complete frame proves its routing identity.
            self.invalid_shape = True
            self.value(None, depth)
            return None
        if char == '{':
            self.take(); self.whitespace()
            result = {} if keep else None
            if self.peek() == '}':
                self.take(); return result
            count = 0
            while True:
                self.whitespace()
                key = self.string(True, 4096)
                child = (True if selector is True else
                         selector.get(key, selector.get(_ANY)) if isinstance(selector, dict) else None)
                if child is not None:
                    if key in result:
                        raise MetadataError('codex_metadata_duplicate_key')
                    count += 1
                    if count > self.MAX_ENTRIES:
                        raise MetadataError('codex_metadata_entries_limit')
                    self.charge(len(key.encode('utf-8', errors='surrogatepass')) + 64)
                self.whitespace()
                if self.take() != ':':
                    raise MetadataError('codex_metadata_separator')
                value = self.value(child, depth + 1)
                if child is not None:
                    result[key] = value
                self.whitespace(); end = self.take()
                if end == '}':
                    return result
                if end != ',':
                    raise MetadataError('codex_metadata_separator')
        if char == '[':
            child = True if selector is True else selector[0] if isinstance(selector, tuple) else None
            self.take(); self.whitespace()
            result = [] if keep else None
            if self.peek() == ']':
                self.take(); return result
            while True:
                if keep and len(result) >= self.MAX_ENTRIES:
                    raise MetadataError('codex_metadata_entries_limit')
                value = self.value(child, depth + 1)
                if keep:
                    result.append(value)
                self.whitespace(); end = self.take()
                if end == ']':
                    return result
                if end != ',':
                    raise MetadataError('codex_metadata_separator')
        if char == '"':
            value = self.string(keep)
            if keep:
                self.charge(len(value.encode('utf-8', errors='surrogatepass')))
            return value
        token = ''
        while self.peek() and self.peek() not in ' \r\n\t,]}':
            token += self.take()
            if len(token) > 256:
                raise MetadataError('codex_metadata_scalar_limit')
        if token not in ('null', 'true', 'false') and not _NUMBER.fullmatch(token):
            raise MetadataError('codex_metadata_scalar')
        value = json.loads(token)
        return value if keep else None

    def read(self):
        try:
            result = self.value(SNAPSHOT)
            self.whitespace()
            if self.peek():
                raise MetadataError('codex_metadata_trailing_data')
            self.budget.check(self.deadline)
            return result
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MetadataError('codex_metadata_json') from exc
