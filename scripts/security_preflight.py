from __future__ import annotations

import re
import sys
from pathlib import Path

TEXT_EXT = {".py", ".md", ".toml", ".txt", ".plist", ".service", ".json", ".yaml", ".yml"}
PRIVATE_PATTERNS = [
    "/" + "Users/",
    "leo" + "-vault",
    "/_" + "inventory/",
    "." + "claude/skills",
    "." + "codex/skills",
    "active" + "-tasks",
]
SECRET_REGEXES = [
    re.compile(r"(?i)(private[_ -]?key|seed phrase|mnemonic)\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{24,}"),
    re.compile(r"(?i)\b(?:sk|pk)_[A-Za-z0-9_-]{24,}\b"),
    re.compile(r"\b0x[a-fA-F0-9]{64}\b"),
]
ALLOW_32B_FILES = {"config.example.toml", "tests/test_config.py", "tests/test_engine.py"}


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan(root: Path) -> list[str]:
    root = root.resolve()
    self_path = Path(__file__).resolve()
    issues: list[str] = []

    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path == self_path
            or ".git" in path.parts
            or path.suffix not in TEXT_EXT
        ):
            continue
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")

        for rule_index, pattern in enumerate(PRIVATE_PATTERNS, start=1):
            start = 0
            while True:
                found = text.find(pattern, start)
                if found < 0:
                    break
                issues.append(
                    f"PRIVATE_PATH_PATTERN {rel}:{_line_number(text, found)} rule={rule_index}"
                )
                start = found + max(1, len(pattern))

        for rule_index, rx in enumerate(SECRET_REGEXES, start=1):
            for match in rx.finditer(text):
                if rx.pattern.startswith("\\b0x") and rel in ALLOW_32B_FILES:
                    continue
                issues.append(
                    f"SECRET_LIKE_PATTERN {rel}:{_line_number(text, match.start())} "
                    f"rule={rule_index}"
                )

    for required in [".gitignore", ".env.example", "README.md", "README.zh.md", "SECURITY.md"]:
        if not (root / required).exists():
            issues.append(f"MISSING_REQUIRED_FILE {required}")

    return issues


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0] if args else ".")
    issues = scan(root)
    if issues:
        print("SECURITY_PREFLIGHT_FAIL")
        for issue in issues:
            print(issue)
        return 1
    print("SECURITY_PREFLIGHT_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
