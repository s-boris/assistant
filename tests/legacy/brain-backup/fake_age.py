"""Stand-in for age / age-keygen where the real ones are missing (the hub's CI runner): the escrow tests check
brain-backup's wiring (recipients file, AGE_BIN, the member's place and name, the failure path), not age's
cryptography. Not encryption: the payload is only encoded, keyed to the recipient so another key is refused.
  fake_age.py keygen -o FILE        a secret key file
  fake_age.py keygen -y FILE        its public key
  fake_age.py age -R FILE -o OUT    stdin -> OUT for the recipients in FILE
  fake_age.py age -d -i KEY FILE    FILE -> stdout; exit 1 when KEY is not a recipient"""

import base64
import hashlib
import secrets
import sys

HEAD = b"age-encryption.org/v1\n"


def public(secret: str) -> str:
    return "age1" + hashlib.sha256(secret.strip().encode()).hexdigest()[:58]


def secret_of(path: str) -> str:
    with open(path) as f:
        return next(ln.strip() for ln in f if ln.startswith("AGE-SECRET-KEY-"))


def main(argv: list[str]) -> int:
    mode, args = argv[0], argv[1:]
    if mode == "keygen":
        if args[:1] == ["-y"]:
            print(public(secret_of(args[1])))
            return 0
        key = "AGE-SECRET-KEY-1" + secrets.token_hex(20).upper()
        with open(args[args.index("-o") + 1], "w") as f:
            f.write(f"# public key: {public(key)}\n{key}\n")
        return 0
    if "-d" in args:
        pub = public(secret_of(args[args.index("-i") + 1]))
        with open(args[-1], "rb") as f:
            data = f.read()
        head, _, body = data.partition(b"\n---\n")
        if not head.startswith(HEAD) or f"-> stub {pub}".encode() not in head.split(b"\n"):
            print("age: error: no identity matched any of the recipients", file=sys.stderr)
            return 1
        sys.stdout.buffer.write(base64.b64decode(body))
        return 0
    with open(args[args.index("-R") + 1]) as f:
        recips = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not recips:
        return 1
    body = base64.b64encode(sys.stdin.buffer.read())
    with open(args[args.index("-o") + 1], "wb") as f:
        f.write(HEAD + b"".join(f"-> stub {r}\n".encode() for r in recips) + b"---\n" + body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
