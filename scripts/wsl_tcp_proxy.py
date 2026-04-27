#!/usr/bin/env python3
import argparse
import os
import select
import signal
import socket
import threading
from typing import Tuple


def relay(client: socket.socket, target_addr: Tuple[str, int]) -> None:
    upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        upstream.settimeout(10)
        upstream.connect(target_addr)
        upstream.settimeout(None)
    except OSError:
        client.close()
        upstream.close()
        return

    sockets = [client, upstream]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 60)
            if not readable:
                continue
            for src in readable:
                data = src.recv(65536)
                if not data:
                    return
                dst = upstream if src is client else client
                dst.sendall(data)
    except OSError:
        return
    finally:
        try:
            client.close()
        except OSError:
            pass
        try:
            upstream.close()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Simple TCP forwarder for exposing WSL API to VM clients.")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    parser.add_argument("--pid-file", default="")
    args = parser.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.listen_host, args.listen_port))
    server.listen(128)

    if args.pid_file:
        os.makedirs(os.path.dirname(args.pid_file), exist_ok=True)
        with open(args.pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

    running = True

    def _stop(_sig, _frame):
        nonlocal running
        running = False
        try:
            server.close()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    target = (args.target_host, args.target_port)
    while running:
        try:
            client, _addr = server.accept()
        except OSError:
            break
        t = threading.Thread(target=relay, args=(client, target), daemon=True)
        t.start()

    if args.pid_file and os.path.exists(args.pid_file):
        try:
            os.remove(args.pid_file)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

