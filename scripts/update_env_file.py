from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: update_env_file.py <path> KEY=VALUE [KEY=VALUE ...]", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    updates: dict[str, str] = {}
    for arg in sys.argv[2:]:
        if "=" not in arg:
            print(f"invalid assignment: {arg}", file=sys.stderr)
            return 2
        key, value = arg.split("=", 1)
        updates[key] = value

    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        replaced = False
        for key, value in updates.items():
            if line.startswith(f"{key}="):
                output.append(f"{key}={value}")
                seen.add(key)
                replaced = True
                break
        if not replaced:
            output.append(line)

    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")

    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
