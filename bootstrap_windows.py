#!/usr/bin/env python3
import base64
import hashlib
import io
import pathlib
import sys
import tarfile

EXPECTED = "be4b46495a0a44380770ae694769ed584073088b3fbf0399616c93f4800e01e9"


def main():
    root = pathlib.Path(__file__).resolve().parent
    if (root / "backend" / "app" / "main.py").is_file() and (root / "frontend" / "package.json").is_file():
        print("Source already present.")
        return

    print("Preparing Local Company source from the repository bootstrap bundle...")
    parts = sorted((root / "bootstrap").glob("bundle.part.*"))
    if not parts:
        raise SystemExit("Bootstrap bundle is missing. Run git pull origin main and try again.")

    payload = b"".join(part.read_bytes().strip() for part in parts)
    archive = base64.b64decode(payload, validate=True)
    actual = hashlib.sha256(archive).hexdigest()
    if actual != EXPECTED:
        raise SystemExit("Bootstrap checksum mismatch. Re-clone the repository and try again.")

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        for member in tf.getmembers():
            target = (root / member.name).resolve()
            if target != root and root not in target.parents:
                raise SystemExit("Unsafe path in bootstrap archive: " + member.name)
        tf.extractall(root)

    required = [
        root / "backend" / "app" / "main.py",
        root / "frontend" / "package.json",
        root / "docs" / "ARCHITECTURE.md",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("Bootstrap incomplete; missing: " + ", ".join(missing))
    print("Source ready.")


if __name__ == "__main__":
    main()
