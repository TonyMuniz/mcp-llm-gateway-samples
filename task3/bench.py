"""Benchmark: latency and memory of the gateway versus calling the mock provider directly.

Run:  uv run python bench.py        (Linux: reads the gateway's peak RSS from /proc)

Two scenarios:
  1. Paced stream (20 ms between tokens): shows what redaction adds to time-to-first-token
     (TTFT) and to the tail of the stream.
  2. Large unpaced stream (~1.4 MB of text, ~128k events): shows throughput and that
     the gateway's memory does not grow with the response size.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import aiohttp

MOCK, GW = 9101, 9102
TEXT = ("Reach alice.smith@example.com or bob@mail.example.co.uk. SSN 123-45-6789, "
        "card 4111 1111 1111 1111 and 5500-0000-0000-0004. Thanks for asking!")


def peak_rss_kb(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        return None


async def wait_port(port: int) -> None:
    for _ in range(100):
        try:
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise RuntimeError(f"port {port} never opened")


async def run(http: aiohttp.ClientSession, port: int, **extra) -> dict:
    payload = {"model": "m", "stream": True, "messages": [{"role": "user", "content": TEXT}], **extra}
    t0, ttft, events, size = time.perf_counter(), None, 0, 0
    async with http.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=payload) as r:
        async for raw in r.content:
            size += len(raw)
            if raw.startswith(b"data: {"):
                events += 1
                if ttft is None:
                    obj = json.loads(raw[6:])
                    if obj["choices"][0]["delta"].get("content"):
                        ttft = time.perf_counter() - t0
    return {"ttft_ms": (ttft or 0) * 1000, "total_s": time.perf_counter() - t0,
            "events": events, "mb": size / 1e6}


async def main() -> None:
    procs = [
        subprocess.Popen([sys.executable, "mock_provider.py", "--port", str(MOCK)]),
        subprocess.Popen([sys.executable, "gateway.py", "--port", str(GW),
                          "--upstream", f"http://127.0.0.1:{MOCK}"], stderr=subprocess.DEVNULL),
    ]
    gw_pid = procs[1].pid
    try:
        await asyncio.gather(wait_port(MOCK), wait_port(GW))
        async with aiohttp.ClientSession() as http:
            await run(http, GW)  # warm up

            print("Scenario 1: paced stream, 20 ms between tokens (best of 5)")
            for name, port in (("direct ", MOCK), ("gateway", GW)):
                runs = [await run(http, port, mock_delay_ms=20) for _ in range(5)]
                best = min(runs, key=lambda x: x["ttft_ms"])
                print(f"  {name}  TTFT {best['ttft_ms']:7.1f} ms   "
                      f"total {min(x['total_s'] for x in runs) * 1000:7.0f} ms")

            print("\nScenario 2: large unpaced stream")
            rss0 = peak_rss_kb(gw_pid)
            for repeat in (500, 2000, 8000):
                d = await run(http, MOCK, mock_repeat=repeat)
                g = await run(http, GW, mock_repeat=repeat)
                rss = peak_rss_kb(gw_pid)
                print(f"  x{repeat:<5} events={g['events']:>7}  direct {d['total_s']:5.2f}s  "
                      f"gateway {g['total_s']:5.2f}s  ({g['events'] / g['total_s']:,.0f} ev/s)  "
                      f"gateway peak RSS {rss} kB" if rss else f"  x{repeat} gateway {g['total_s']:.2f}s")
            if rss0:
                print(f"  (gateway peak RSS before scenario 2: {rss0} kB)")
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    asyncio.run(main())
