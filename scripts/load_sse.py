from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import redis.asyncio as aioredis


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connections", "-n", type=int, default=20_000)
    parser.add_argument("--jobs", "-m", type=int, default=2_000)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--url", default=os.environ.get("REALTIME_URL", "http://127.0.0.1:8002"))
    parser.add_argument("--redis", default=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    return parser.parse_args()


def _parse_size(text: str) -> int | None:
    match = re.match(r"\s*([0-9]*\.?[0-9]+)\s*([KMGTP]?i?B)", text.strip(), re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    scale = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
    }
    factor = scale.get(unit)
    if factor is None:
        return None
    return int(value * factor)


def _proc_rss(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def _find_realtime_pid() -> int | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="ignore")
        except OSError:
            continue
        if "app.realtime" in command:
            return int(entry.name)
    return None


def _docker_rss() -> int | None:
    try:
        output = subprocess.check_output(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"],
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        name = parts[0]
        if name == "realtime" or name.endswith("-realtime") or name.endswith("_realtime"):
            return _parse_size(parts[1].split("/", 1)[0])
    return None


def realtime_rss_bytes() -> int | None:
    pid = os.environ.get("REALTIME_PID", "").strip()
    if pid.isdigit():
        rss = _proc_rss(int(pid))
        if rss is not None:
            return rss
    rss = _docker_rss()
    if rss is not None:
        return rss
    found = _find_realtime_pid()
    if found is None:
        return None
    return _proc_rss(found)


def _pg_connect(dsn: str):
    if not dsn:
        return None
    import psycopg

    normalized = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg.connect(normalized)


def _pg_count(connection) -> int | None:
    if connection is None:
        return None
    row = connection.execute("SELECT count(*) FROM pg_stat_activity").fetchone()
    if row is None:
        return None
    return int(row[0])


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(pct / 100 * len(ordered)) - 1))
    return ordered[index]


async def _consume(client: httpx.AsyncClient, url: str, latencies: list[float], ready: list[int], stop: asyncio.Event) -> None:
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            return
        async for line in response.aiter_lines():
            if stop.is_set():
                return
            if line.startswith("event:") and line.split(":", 1)[1].strip() == "subscribed":
                ready[0] += 1
                continue
            if not line.startswith("data:"):
                continue
            raw = line.split(":", 1)[1].strip()
            if not raw or raw == "{}":
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            seq = payload.get("seq")
            if isinstance(seq, (int, float)):
                latencies.append((time.time_ns() // 1_000_000) - int(seq))


async def _publish(redis_url: str, job_ids: list[str], seconds: float, stop: asyncio.Event) -> None:
    client = aioredis.from_url(redis_url)
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline and not stop.is_set():
            started = time.monotonic()
            pipe = client.pipeline()
            for job_id in job_ids:
                seq = time.time_ns() // 1_000_000
                payload = json.dumps(
                    {
                        "type": "job",
                        "id": job_id,
                        "status": "translating",
                        "done_segments": 1,
                        "total_segments": 1,
                        "cost_cents": 0,
                        "error_code": None,
                        "seq": seq,
                    },
                    separators=(",", ":"),
                )
                pipe.publish(f"job:{job_id}", payload)
            await pipe.execute()
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, 0.5 - elapsed))
    finally:
        closer = getattr(client, "aclose", None) or client.close
        result = closer()
        if asyncio.iscoroutine(result):
            await result


async def run(args: argparse.Namespace) -> None:
    job_count = max(1, args.jobs)
    connections = max(1, args.connections)
    job_ids = [str(uuid.uuid4()) for _ in range(job_count)]
    latencies: list[float] = []
    ready = [0]
    stop = asyncio.Event()
    limits = httpx.Limits(max_connections=connections, max_keepalive_connections=connections)
    pg = _pg_connect(args.database_url)
    before = _pg_count(pg)
    rss = None
    during = None
    async with httpx.AsyncClient(limits=limits, timeout=None) as client:
        tasks = [
            asyncio.create_task(
                _consume(client, f"{args.url.rstrip('/')}/events?job={job_ids[index % job_count]}", latencies, ready, stop)
            )
            for index in range(connections)
        ]
        deadline = time.monotonic() + max(30.0, connections / 200)
        while ready[0] < connections and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        during = _pg_count(pg)
        rss = realtime_rss_bytes()
        try:
            await _publish(args.redis, job_ids, args.seconds, stop)
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    if pg is not None:
        pg.close()
    per_connection = None if rss is None else int(rss / connections)
    print(f"connections={connections}")
    print(f"jobs={job_count}")
    print(f"subscribed={ready[0]}")
    print(f"pg_stat_activity_before={before}")
    print(f"pg_stat_activity_during={during}")
    print(f"rss_bytes={rss}")
    print(f"rss_per_connection_bytes={per_connection}")
    print(f"messages={len(latencies)}")
    print(f"latency_p50_ms={_percentile(latencies, 50)}")
    print(f"latency_p99_ms={_percentile(latencies, 99)}")


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
