"""Fixed synthetic public-input contracts, never model-written verdicts.

No personal inputs or storage. These probes attest bounded engineering behavior,
not natural usage or user benefit. The model cannot alter this module.
"""
from . import sources

CONTRACTS = {
    'text_blocks': ('core/activity_mvp/feedback/sources.py', '结构化文字提取',
                    '部分文字块内容为空或类型异常时，读取不应中断，合法文字应保留。'),
    'request_region': ('core/activity_mvp/feedback/sources.py', '请求正文提取',
                       '引用和附件不能当成本人的当前请求，实际请求应保留。'),
    'timestamp_format': ('core/activity_mvp/feedback/sources.py', '时间解析',
                         '等价时区格式应得到相同时间，无效时间应返回空值。'),
}


def check(contract):
    if contract == 'text_blocks':
        cases = [('', ''), ('正文', '正文'), (None, ''),
                 ([{'type':'text','text':'甲'}, {'type':'input_text','text':'乙'}], '甲\n乙'),
                 ([{'type':'text','text':None}, {'type':'text','text':'有效文字'}], '有效文字'),
                 ([{'type':'text','text':3}, {'type':'image','text':'不应读取'},
                   {'type':'output_text','text':'有效文字'}, None], '有效文字')]
        for value, expected in cases: assert sources._text(value) == expected, 'text_blocks_contract'
    elif contract == 'request_region':
        for value, expected in [(None,''), ('> 引用不对\n当前请求','当前请求'),
                                ('```\n历史错误\n```\n当前请求','当前请求'),
                                ('附件资料\n## My request:\n当前请求<image name="fixture">旧文</image>','当前请求')]:
            assert sources.direct_text(value) == expected, 'request_region_contract'
    elif contract == 'timestamp_format':
        assert sources.timestamp('2026-01-01T08:00:00+08:00') == sources.timestamp('2026-01-01T00:00:00Z')
        assert sources.timestamp('not-a-time') is None
        assert sources.timestamp(123) == 123
    else: raise ValueError('unknown_proactive_contract')


def inspect():
    failures = []
    for name in CONTRACTS:
        try: check(name)
        except (AssertionError, TypeError, ValueError): failures.append(name)
    return failures


if __name__ == '__main__':
    import sys
    # A failing input contract is always an assertion failure, never an import
    # or environment error misrepresented as a reproducible behavior defect.
    try: check(sys.argv[1])
    except (TypeError, ValueError) as exc: raise AssertionError('fixed_contract_failed') from exc
