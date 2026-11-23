"""Read-only, source-bound responsibility reviews. Never grants execution rights."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import math
from .core_reader import valid_id

RESPONSIBILITY = {'ai': 'AI 负责', 'user_decision': '需要你决定', 'user_action': '需要你操作', 'unknown': '待核清'}
PROGRESS = {'pending': '待处理', 'running': '处理中', 'blocked': '受阻', 'waiting': '等待条件', 'verification': '待验证', 'resolved': '已结束', 'paused': '已暂停'}


def open_local_file(path):
    # Anchor each directory descriptor so a replaced ancestor cannot redirect the read.
    if not path.is_absolute() or '..' in path.parts or not path.name:
        raise ValueError('invalid_review_path')
    directories = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open('/', directories)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, directories, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def private_json(path):
    fd = open_local_file(path)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('review_permissions')
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('review_limit')
    return json.loads(raw)


class Reviews:
    def __init__(self, directory, *, local_evidence_only=False):
        self.path = Path(directory).expanduser().absolute() / 'reviews.json'
        self.local_evidence_only = local_evidence_only
        self.bundle = {'baseline': [], 'reviews': {}}
        self.error = ''
        self._fingerprints = {}

    def reload(self):
        try:
            value = private_json(self.path)
            if (not isinstance(value, dict) or value.get('schema') != 1
                    or not isinstance(value.get('baseline'), list) or not isinstance(value.get('reviews'), dict)
                    or any(not isinstance(x, dict) or not valid_id(x.get('id')) for x in value.get('baseline', []))):
                raise ValueError('invalid_review')
            self.bundle, self.error = value, ''
        except (OSError, ValueError, TypeError):
            self.bundle, self.error = {'baseline': [], 'reviews': {}}, '逐项核查资料暂不可读取，责任和进度待核清。'

    def evidence_current(self, rows):
        if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
            return False
        try:
            for row in rows:
                path = Path(row['path'])
                if self.local_evidence_only and (not path.is_relative_to(self.path.parent) or '..' in path.parts):
                    return False
                fd = open_local_file(path)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 2 * 1024 * 1024:
                        return False
                    key = (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    fingerprint = self._fingerprints.get(key)
                    if not fingerprint:
                        raw = stream.read(2 * 1024 * 1024 + 1)
                        if len(raw) > 2 * 1024 * 1024:
                            return False
                        fingerprint = hashlib.sha256(raw).hexdigest()
                        self._fingerprints[key] = fingerprint
                    if fingerprint != row['sha256']:
                        return False
            return True
        except (OSError, KeyError, TypeError, ValueError):
            return False

    def project(self, item):
        unknown = {'responsibility': 'unknown', 'progress': 'pending', 'basis': '尚未核清当前依据；原记录保留，不要求你猜测。', 'next_step': '由 AI 核对原任务、当前结果及授权。', 'reviewed_at': '', 'evidence_version': '', 'question': '', 'user_action': '', 'outcome': '', 'review_valid': False}
        review = self.bundle['reviews'].get(item['id'])
        if not isinstance(review, dict):
            return unknown
        expiry = review.get('expires_at')
        time_valid = expiry is None or (type(expiry) in (int, float) and math.isfinite(expiry) and expiry > time.time())
        text_valid = all(isinstance(review.get(key, ''), str) for key in ('basis', 'next_step', 'question', 'user_action', 'outcome', 'reviewed_at'))
        valid = (time_valid and text_valid and str(review.get('native_revision')) == str(item.get('revision'))
                 and item.get('current_verification') != 'unknown'
                 and isinstance(review.get('responsibility'), str) and review['responsibility'] in RESPONSIBILITY
                 and isinstance(review.get('progress'), str) and review['progress'] in PROGRESS
                 and self.evidence_current(review.get('evidence')))
        if not valid:
            return {**unknown, 'basis': '事项版本或核查依据已变化或到期，需重新核查；旧结论不自动沿用。'}
        result = {key: review.get(key, '') for key in unknown}
        result['review_valid'] = True
        result['evidence_version'] = str(review['native_revision'])
        # A static review is never evidence of a currently executing process.
        if result['progress'] == 'running':
            result['progress'] = 'pending'
        if result['progress'] == 'resolved' and (not result['outcome'] or not review.get('completion_verified')):
            result['progress'] = 'verification'
        return result

    def baseline(self):
        return self.bundle['baseline']
