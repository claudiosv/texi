"""Helpers for reading the real-tool fixtures in tests/fixtures/logs."""

from pathlib import Path

LOGS = Path(__file__).parent / "fixtures" / "logs"


def read(name: str) -> str:
    return (LOGS / name).read_text(encoding="utf-8", errors="replace")


def exit_code(name: str) -> int:
    return int((LOGS / name).read_text().strip())


def all_fixtures() -> list[Path]:
    return sorted(
        p
        for p in LOGS.iterdir()
        if p.suffix in {".log", ".blg", ".ilg", ".glg", ".txt"}
    )
