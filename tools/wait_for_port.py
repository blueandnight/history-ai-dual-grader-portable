"""Deployment pre-start check, using the same non-reuse bind as the server.

Run before starting a replacement process, with the intended host and port.
Does not stop processes, edit configuration, or retry application startup.
"""
import argparse
import errno
import socket
import time


def wait_for_port(host, port, timeout=100):
    deadline = time.monotonic() + timeout
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
                return
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE and getattr(exc, 'winerror', None) != 10048:
                    raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Startup port is still occupied; existing process was not changed')
        time.sleep(min(0.25, remaining))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--timeout', type=float, default=100)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.timeout < 0:
        parser.error('port must be 1..65535 and timeout must be nonnegative')
    wait_for_port(args.host, args.port, args.timeout)
