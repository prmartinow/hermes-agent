"""Differential and edge-case tests for shell-preview parsing parity."""
import re
from typing import Iterator
import pytest
from agent.display import (
    summarize_shell_command,
    _split_shell_words,
    _split_shell_compound,
    _clean_shell_segment,
    _is_shell_boundary_echo,
    _shell_head_word,
    _scan_quoted,
)


def _scan_quoted_ref(text: str) -> Iterator[tuple[int, str, bool]]:
    quote = None
    for i, ch in enumerate(text):
        if quote:
            yield i, ch, True
            if ch == quote and (i == 0 or text[i - 1] != "\\"):
                quote = None
        elif ch in {"'", '"'}:
            quote = ch
            yield i, ch, True
        else:
            yield i, ch, False


def _split_shell_words_ref(segment: str) -> list[str]:
    parts: list[list[str]] = [[]]
    for _, ch, quoted in _scan_quoted_ref(segment):
        if not quoted and ch.isspace():
            parts.append([])
        else:
            parts[-1].append(ch)
    return ["".join(p) for p in parts if p]


def _strip_shell_pipe_tail_ref(segment: str) -> str:
    heads = {"head", "tail", "wc", "sort", "uniq"}
    words = _split_shell_words_ref(segment)
    for i, word in enumerate(words):
        basename = words[i + 1].rsplit("/", 1)[-1] if i + 1 < len(words) and words[i + 1] else ""
        if word == "|" and basename in heads:
            words = words[:i]
            break
    return " ".join(words).strip()


def _split_shell_compound_ref(command: str) -> list[str]:
    raw: list[list[str]] = [[]]
    skip = False
    for i, ch, quoted in _scan_quoted_ref(command):
        if skip:
            skip = False
        elif not quoted and (command.startswith("&&", i) or command.startswith("||", i)):
            raw.append([])
            skip = True
        elif not quoted and ch in {";", "\n"}:
            raw.append([])
        else:
            raw[-1].append(ch)
    segments = (_strip_shell_pipe_tail_ref("".join(buf).strip()) for buf in raw)
    return [s for s in segments if s]


def _clean_shell_segment_ref(segment: str) -> str:
    words = _split_shell_words_ref(segment)
    out: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if re.match(r"^\d*(?:>>?|<)$", word):
            i += 2
        elif re.match(r"^\d*(?:>&|<&)\d+$", word):
            i += 1
        else:
            out.append(word)
            i += 1
    return " ".join(out).strip()


def _shell_head_word_ref(segment: str) -> str:
    words = _split_shell_words_ref(segment)
    while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
        words.pop(0)
    head = words[0] if words else ""
    return head.rsplit("/", 1)[-1] if head else ""


def _is_shell_boundary_echo_ref(segment: str) -> bool:
    words = _split_shell_words_ref(segment)
    head = words[0] if words else ""
    basename = head.rsplit("/", 1)[-1] if head else ""
    if basename != "echo":
        return False
    return bool(re.search(r"-{2,}|_exit=|(?:^|\s|=)\$[?{]|PIPESTATUS", " ".join(words[1:])))


def summarize_shell_command_ref(command: str) -> str:
    original = " ".join(command.split())
    if not original:
        return ""
    segments = _split_shell_compound_ref(original)
    if len(segments) <= 1:
        return _clean_shell_segment_ref(segments[0] if segments else original) or original
    silent_heads = {"cd", "pushd", "popd", "export", "set", "unset", "source", ".", "true", "false", ":"}
    core: list[str] = []
    for segment in segments:
        cleaned = _clean_shell_segment_ref(segment)
        if cleaned and _shell_head_word_ref(cleaned) not in silent_heads and not _is_shell_boundary_echo_ref(cleaned):
            core.append(cleaned)
    if not core:
        return original
    count = len(core) - 1
    return core[0] if not count else f"{core[0]} + {count} {'command' if count == 1 else 'commands'}"


TEST_COMMANDS = [
    "",
    "   ",
    "\t \n \r ",
    "ls",
    "ls -la /tmp",
    "echo hello world",
    "echo 'single quoted string' foo",
    'echo "double quoted string" bar',
    "echo 'nested \"double\"' \"nested 'single'\"",
    r'echo "escaped \" quote"',
    r'echo \"unquoted escaped quote\"',
    r'echo "double \\\" backslash quote"',
    r'echo \\\"many backslashes\"',
    'echo "unclosed quote',
    "echo 'unclosed single quote",
    "cmd arg1='val 1' arg2=\"val 2\" arg3=val3",
    # Unicode
    "echo 🚀 🌟 喵喵喵 こんにちは 세계",
    "echo 'こんにちは 世界' && ls -l 📂",
    "grep -rn 'café au lait' /data/ñandú",
    # Compound operators
    "cmd1 && cmd2 || cmd3 ; cmd4\ncmd5",
    "echo a && echo b && echo c",
    'echo "a && b" || echo "c || d; e"',
    "cmd1 ; ; ; cmd2",
    ";",
    "&&",
    "||",
    "\n\n\n",
    # Redirections
    "python test.py > output.txt 2>&1 < input.txt >> append.log",
    "echo done 1>&2 3> err.log",
    "cat < /dev/null > /dev/null",
    # Pipe tails
    "cat file.txt | head -n 10",
    "cat file.txt | tail -f | grep foo",
    "dmesg | grep error | wc -l",
    "ps aux | sort -nk +3 | uniq -c",
    "curl -s http://example.com | head",
    # Boundary echo
    "echo ----------------------------------------",
    "echo _exit=0",
    "echo $?",
    "echo ${PIPESTATUS[0]}",
    'echo "status: $?"',
    # Variable assignments
    "FOO=bar python main.py",
    "A=1 B=2 C=3 /usr/bin/env bash -c 'echo ok'",
    "_VAR_1=val-123 my_cmd",
    # Multi-command summaries with silent heads
    "cd /workspace && git status && echo done",
    "export FOO=1 && true && ./run.sh && unset FOO",
    ": && source env.sh && python run.py 2>&1 > /dev/null",
    # Large command
    "echo " + " ".join(f"arg_{i}='val {i}'" for i in range(1000)),
    " && ".join(f"echo step_{i} > file_{i}.txt" for i in range(100)),
]


@pytest.mark.parametrize("cmd", TEST_COMMANDS)
def test_split_shell_words_parity(cmd: str):
    assert _split_shell_words(cmd) == _split_shell_words_ref(cmd)


@pytest.mark.parametrize("cmd", TEST_COMMANDS)
def test_split_shell_compound_parity(cmd: str):
    assert _split_shell_compound(cmd) == _split_shell_compound_ref(cmd)


@pytest.mark.parametrize("cmd", TEST_COMMANDS)
def test_clean_shell_segment_parity(cmd: str):
    assert _clean_shell_segment(cmd) == _clean_shell_segment_ref(cmd)


@pytest.mark.parametrize("cmd", TEST_COMMANDS)
def test_shell_head_word_parity(cmd: str):
    assert _shell_head_word(cmd) == _shell_head_word_ref(cmd)


@pytest.mark.parametrize("cmd", TEST_COMMANDS)
def test_is_shell_boundary_echo_parity(cmd: str):
    assert _is_shell_boundary_echo(cmd) == _is_shell_boundary_echo_ref(cmd)


@pytest.mark.parametrize("cmd", TEST_COMMANDS)
def test_summarize_shell_command_parity(cmd: str):
    assert summarize_shell_command(cmd) == summarize_shell_command_ref(cmd)


def test_generated_shell_preview_parity():
    # Preserve the historical parser's grammar, including unbalanced quotes and
    # immediate-backslash escaping; this is intentionally not a shlex oracle.
    import random

    rng = random.Random(20260928)
    alphabet = "ab09_=;&|<>/\\\"' \t\n\r\u00a0\u2003界"
    for _ in range(2000):
        command = "".join(rng.choices(alphabet, k=rng.randrange(129)))
        assert _split_shell_words(command) == _split_shell_words_ref(command)
        assert _split_shell_compound(command) == _split_shell_compound_ref(command)
        assert summarize_shell_command(command) == summarize_shell_command_ref(command)


def test_scan_quoted_generator_preservation():
    samples = [
        'echo "hello world"',
        r'echo "escaped \" quote"',
        'echo \'single\' "double"',
    ]
    for s in samples:
        assert list(_scan_quoted(s)) == list(_scan_quoted_ref(s))
