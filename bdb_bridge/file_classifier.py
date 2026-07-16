from __future__ import annotations

from pathlib import PurePosixPath

from .repository_index_models import FileKind, ParseStatus

_EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".css": "css",
    ".html": "html",
    ".htm": "html",
    ".liquid": "liquid",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".psd1": "powershell",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".txt": "plain_text",
}


def detect_language(path: str) -> str:
    name = PurePosixPath(path).name.lower()
    if name in {".gitignore", ".gitattributes", ".editorconfig"}:
        return "plain_text"
    if name.startswith("readme") and "." not in name[6:]:
        return "plain_text"
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _EXTENSION_LANGUAGE:
        return _EXTENSION_LANGUAGE[suffix]
    suffixes = [s.lower() for s in PurePosixPath(path).suffixes]
    if len(suffixes) >= 2 and suffixes[-1] == ".liquid":
        return "liquid"
    return "unknown"


def is_strict_utf8_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def count_text_lines(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def classify_content(
    *,
    path: str,
    data: bytes,
    file_kind: FileKind,
    max_parse_bytes: int,
) -> tuple[str, bool, int | None, ParseStatus, str | None]:
    language = detect_language(path)
    if file_kind is FileKind.SUBMODULE:
        return language, False, None, ParseStatus.METADATA_ONLY, None
    if file_kind is FileKind.SYMLINK:
        is_text = is_strict_utf8_text(data) if data else False
        line_count = count_text_lines(data.decode("utf-8")) if is_text else None
        return language, is_text, line_count, ParseStatus.METADATA_ONLY, None
    if not is_strict_utf8_text(data):
        return language, False, None, ParseStatus.BINARY, None
    text = data.decode("utf-8")
    line_count = count_text_lines(text)
    if language != "python":
        return language, True, line_count, ParseStatus.UNSUPPORTED_LANGUAGE, None
    if len(data) > max_parse_bytes:
        return language, True, line_count, ParseStatus.TOO_LARGE, None
    return language, True, line_count, ParseStatus.OK, None
