"""Desktop host for the existing analysis engine; never opens a browser."""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
from pathlib import Path


def watch_parent(parent_pid: int) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, parent_pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Cannot monitor desktop process")

    def wait() -> None:
        result = kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        kernel.CloseHandle(handle)
        if result == 0:
            os._exit(0)

    threading.Thread(target=wait, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--portfolio-file", required=True)
    args = parser.parse_args()
    portfolio = Path(args.portfolio_file).resolve()
    if not portfolio.is_file():
        parser.error("Account file does not exist; desktop host will not create or overwrite it")
    if sys.stdout is None or sys.stderr is None:
        log_path = portfolio.parent / "service.log"
        log_stream = log_path.open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_stream
        if sys.stderr is None:
            sys.stderr = log_stream
    os.environ["XIAO_A_PORTFOLIO_FILE"] = str(portfolio)
    os.environ["XIAO_A_DATA_DIR"] = str(portfolio.parent)
    os.environ["XIAO_A_AUDIT_FILE"] = str(portfolio.parent / "prediction_audit.jsonl")
    if args.parent_pid and os.name == "nt":
        watch_parent(args.parent_pid)
    from xiao_a.dashboard import run_dashboard
    return run_dashboard(port=args.port, open_browser=False)


if __name__ == "__main__":
    raise SystemExit(main())
