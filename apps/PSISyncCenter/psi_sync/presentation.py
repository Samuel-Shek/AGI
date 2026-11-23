"""Punctuation rules copied from PSILocalWeb presentation.py; no inference."""
import re

PAIRS = {'(': ')', '（': '）', '[': ']', '【': '】', '“': '”', '「': '」', '『': '』', '‘': '’'}
ENDINGS = re.compile(r"(?:的|和|与|将|把|从|在|对|向|给|往|运往|送往|关于|用于|针对|为了|作为|以及|但是|但|并|或|到|由)$")


def clauses(text):
    """Only split outside paired punctuation. Never split decimal/model periods."""
    stack, start, result = [], 0, []
    for index, char in enumerate(text):
        if char == '"' or (char == "'" and not (index and index + 1 < len(text) and text[index-1].isalnum() and text[index+1].isalnum())):
            if stack and stack[-1] == char:
                stack.pop()
            else:
                stack.append(char)
        elif char in PAIRS:
            stack.append(PAIRS[char])
        elif char in PAIRS.values():
            if not stack or stack.pop() != char:
                return []
        elif not stack and char in '，,；;：:。！？!?\n':
            # Colons within clocks are not a clause boundary.
            if char == ':' and index and index + 1 < len(text) and text[index-1].isdigit() and text[index+1].isdigit():
                continue
            if text[start:index].strip():
                end = index + 1 if char in '？！?!' else index
                result.append(text[start:end].strip())
            start = index + 1
    if stack:
        return []
    if text[start:].strip():
        result.append(text[start:].strip())
    return result


def readable(text):
    value = text.strip().rstrip('。！？?!；;，,:：').rstrip()
    if not value or '…' in value or '...' in value or ENDINGS.search(value):
        return False
    # Validate punctuation without treating a closing quote as evidence that the
    # phrase inside is complete (e.g. “把材料运往”).
    if not clauses(value):
        return False
    tail = value.rstrip('"\'”’」』)]）】').rstrip()
    if ENDINGS.search(tail):
        return False
    return bool(re.search(r'[A-Za-z0-9\u4e00-\u9fff]', value))


def short_title(title):
    parts = clauses(title)
    if parts and len(parts[0]) <= 60 and readable(parts[0]):
        # A bare label such as 背景： is not the subject of the decision.
        if parts[0] not in {'背景', '说明', '事项', '任务', '合成范围', '待决定'}:
            return parts[0]
    # No generated substitute, ellipsis, dropped subject, or guessed object.
    return title


def recommendation_excerpt(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'^(?:(?:建议|推荐)(?:选项|选择)?\s*[:：]?\s*)+', '', value.strip())
    value = value.removesuffix('。').strip()
    for opening, closing in (('「','」'), ('“','”'), ('"','"')):
        if value.startswith(opening) and value.endswith(closing) and len(value) > 1:
            value = value[1:-1].strip()
            break
    parts = clauses(value)
    if not parts or not readable(parts[0]) or not re.search(r'[\u4e00-\u9fff]', value):
        return ''
    # The complete source, including later conditions, is displayed directly
    # beneath this short button. Never cut a long first clause by character count.
    selected = parts[0]
    if len(selected) > 60 or re.match(r'^(?:如果|若|除非|仅当)', selected):
        return ''
    if re.search(r'(?:^|\s)(?:~?/|[A-Za-z]:\\)|[<>\x00-\x08\x0b-\x1f]', selected):
        return ''
    return selected
