#!/usr/bin/env python3
"""
Patch dingmaotu/mql-zmq for modern MQL5 compiler.

The upstream library (last updated 2023) uses `char[]` where the modern MT5
compiler now requires `uchar[]` (stricter signedness). Without this patch,
compilation fails with 26 errors of the form:

    error 246: parameter convertion type 'char[]' to 'const uchar[] &'
               is not allowed

This script rewrites the relevant #import signatures and local arrays from
`char` to `uchar`. At the libzmq C ABI level both are 8-bit bytes — the
library doesn't care about signedness — so the patch is behavior-preserving.

The .mqh files from dingmaotu's GitHub tarball are plain UTF-8 (despite
the MT5 runtime logs being UTF-16LE, source files on GitHub are UTF-8).

Run after the `/mql-zmq/src` tarball has been extracted in the Dockerfile.
"""
from pathlib import Path
import sys

BASE = Path(sys.argv[1] if len(sys.argv) > 1 else "/mql-zmq/src/Include/Zmq")

# (file, [(old_literal, new_literal), ...])
PATCHES = {
    "Socket.mqh": [
        # #import declarations — change addr[] parameter type
        ("int zmq_bind(intptr_t s,const char &addr[]);",
         "int zmq_bind(intptr_t s,const uchar &addr[]);"),
        ("int zmq_connect(intptr_t s,const char &addr[]);",
         "int zmq_connect(intptr_t s,const uchar &addr[]);"),
        ("int zmq_unbind(intptr_t s,const char &addr[]);",
         "int zmq_unbind(intptr_t s,const uchar &addr[]);"),
        ("int zmq_disconnect(intptr_t s,const char &addr[]);",
         "int zmq_disconnect(intptr_t s,const uchar &addr[]);"),
        ("int zmq_socket_monitor(intptr_t s,const char &addr[],int events);",
         "int zmq_socket_monitor(intptr_t s,const uchar &addr[],int events);"),
        # local arrays in bind/unbind/connect/disconnect — StringToUtf8 needs uchar
        ("bool Socket::bind(string addr)\n  {\n   char arr[];",
         "bool Socket::bind(string addr)\n  {\n   uchar arr[];"),
        ("bool Socket::unbind(string addr)\n  {\n   char arr[];",
         "bool Socket::unbind(string addr)\n  {\n   uchar arr[];"),
        ("bool Socket::connect(string addr)\n  {\n   char arr[];",
         "bool Socket::connect(string addr)\n  {\n   uchar arr[];"),
        ("bool Socket::disconnect(string addr)\n  {\n   char arr[];",
         "bool Socket::disconnect(string addr)\n  {\n   uchar arr[];"),
    ],
    "SocketOptions.mqh": [
        # getStringOption + setStringOption both use char buf[] → StringFromUtf8/StringToUtf8 want uchar
        ("bool SocketOptions::getStringOption(int option,string &value,size_t length)\n  {\n   char buf[];",
         "bool SocketOptions::getStringOption(int option,string &value,size_t length)\n  {\n   uchar buf[];"),
        ("bool SocketOptions::setStringOption(int option,const string value,bool ending)\n  {\n   char buf[];",
         "bool SocketOptions::setStringOption(int option,const string value,bool ending)\n  {\n   uchar buf[];"),
    ],
    "ZmqMsg.mqh": [
        ("intptr_t zmq_msg_gets(zmq_msg_t &msg,const char &property[]);",
         "intptr_t zmq_msg_gets(zmq_msg_t &msg,const uchar &property[]);"),
    ],
    "Zmq.mqh": [
        ("int zmq_has(const char &capability[]);",
         "int zmq_has(const uchar &capability[]);"),
    ],
    # Z85.mqh is pulled in via Zmq.mqh (curve auth) even though we don't use it.
    # Needs the same char→uchar treatment to compile.
    "Z85.mqh": [
        ("intptr_t zmq_z85_encode(char &str[],const uchar &data[],size_t size);",
         "intptr_t zmq_z85_encode(uchar &str[],const uchar &data[],size_t size);"),
        ("intptr_t zmq_z85_decode(uchar &dest[],const char &str[]);",
         "intptr_t zmq_z85_decode(uchar &dest[],const uchar &str[]);"),
        ("int zmq_curve_keypair(char &z85_public_key[],char &z85_secret_key[]);",
         "int zmq_curve_keypair(uchar &z85_public_key[],uchar &z85_secret_key[]);"),
        ("int zmq_curve_public(char &z85_public_key[],const char &z85_secret_key[]);",
         "int zmq_curve_public(uchar &z85_public_key[],const uchar &z85_secret_key[]);"),
        # Local str[] declarations — match each context uniquely via surrounding lines
        ("   char str[];\n   ArrayResize(str,(int)(1.25*size+1));",
         "   uchar str[];\n   ArrayResize(str,(int)(1.25*size+1));"),
        ("   char str[];\n   StringToUtf8(secret,str);",
         "   uchar str[];\n   StringToUtf8(secret,str);"),
        ("   char str[];\n   StringToUtf8(data,str,false);",
         "   uchar str[];\n   StringToUtf8(data,str,false);"),
    ],
}


def patch_file(path: Path, replacements: list[tuple[str, str]]) -> int:
    # Files use CRLF line endings — preserve them (read/write as bytes,
    # but do replacements on str with \r\n-aware patterns via normalization)
    raw = path.read_bytes()
    # The library ships a mix of encodings: some files are plain UTF-8,
    # others UTF-16LE with BOM (Zmq.mqh for example). Detect from BOM.
    encoding = "utf-8"
    bom = b""
    if raw.startswith(b"\xef\xbb\xbf"):
        bom = raw[:3]
        text = raw[3:].decode("utf-8")
    elif raw.startswith(b"\xff\xfe"):
        bom = raw[:2]
        encoding = "utf-16-le"
        text = raw[2:].decode("utf-16-le")
    else:
        text = raw.decode("utf-8")
    applied = 0
    for old, new in replacements:
        # Normalize both sides to LF for matching, but write back with the
        # file's native line endings (preserve CRLF if present)
        native_crlf = "\r\n" in text
        old_native = old.replace("\n", "\r\n") if native_crlf else old
        new_native = new.replace("\n", "\r\n") if native_crlf else new
        if old_native in text:
            text = text.replace(old_native, new_native)
            applied += 1
        else:
            print(f"  [WARN] pattern not found in {path.name}: {old[:60]!r}…", file=sys.stderr)
    new_bytes = bom + text.encode(encoding)
    path.write_bytes(new_bytes)
    return applied


def main() -> int:
    total = 0
    for name, reps in PATCHES.items():
        p = BASE / name
        if not p.exists():
            print(f"[ERROR] missing {p}", file=sys.stderr)
            return 1
        n = patch_file(p, reps)
        print(f"[OK] {name}: {n}/{len(reps)} patches applied")
        total += n
    expected = sum(len(v) for v in PATCHES.values())
    if total != expected:
        print(f"[FAIL] {total}/{expected} patches applied", file=sys.stderr)
        return 1
    print(f"[DONE] {total} patches applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
