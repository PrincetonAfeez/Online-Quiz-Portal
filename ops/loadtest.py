"""Capacity probe for the core student workflow.

Not a substitute for a real load rig; it answers one question honestly: how does
the critical path behave under concurrent students on this hardware? Run it
against a staging instance, never production.

    python ops/loadtest.py --base http://localhost:8000 --students 25 --saves 10
"""

from __future__ import annotations

import argparse
import http.cookiejar as cookielib
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

RESULTS_LOCK = threading.Lock()
RESULTS: dict[str, list[float]] = {}


def record(label: str, seconds: float) -> None:
    with RESULTS_LOCK:
        RESULTS.setdefault(label, []).append(seconds)


class Session:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = cookielib.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def token(self) -> str:
        return next((c.value for c in self.jar if c.name == "csrftoken"), "")

    def get(self, path: str, label: str | None = None):
        start = time.perf_counter()
        try:
            response = self.opener.open(self.base + path)  # noqa: S310 - operator-supplied base URL
            body = response.read().decode("utf-8", "replace")
            status = response.getcode()
        except urllib.error.HTTPError as exc:
            body, status = exc.read().decode("utf-8", "replace"), exc.code
        if label:
            record(label, time.perf_counter() - start)
        return status, body

    def post(self, path: str, data: dict, label: str | None = None):
        payload = dict(data)
        payload["csrfmiddlewaretoken"] = self.token()
        request = urllib.request.Request(
            self.base + path,
            urllib.parse.urlencode(payload, doseq=True).encode(),
            headers={"Referer": self.base + path},
        )
        start = time.perf_counter()
        try:
            response = self.opener.open(request)  # noqa: S310 - operator-supplied base URL
            body, status = response.read().decode("utf-8", "replace"), response.getcode()
            url = response.geturl()
        except urllib.error.HTTPError as exc:
            body, status, url = exc.read().decode("utf-8", "replace"), exc.code, path
        if label:
            record(label, time.perf_counter() - start)
        return status, body, url


def run_student(base: str, username: str, password: str, assignment_id: int, saves: int):
    import re

    session = Session(base)
    session.get("/accounts/login/")
    session.post("/accounts/login/", {"username": username, "password": password}, "login")
    session.get("/", "dashboard")

    status, body, url = session.post(
        f"/assignments/{assignment_id}/start/", {}, "start_attempt"
    )
    match = re.search(r"/attempts/(\d+)/", url)
    if not match:
        return
    attempt_id = match.group(1)
    status, body = session.get(f"/attempts/{attempt_id}/", "attempt_page")

    items = re.findall(r"/attempts/\d+/save/(\d+)/", body)
    choices = re.findall(r'name="choices"\s+value="(\d+)"', body)
    revision = 0
    for index in range(saves):
        if not items:
            break
        item = items[index % len(items)]
        choice = choices[index % len(choices)] if choices else ""
        code, _, _ = session.post(
            f"/attempts/{attempt_id}/save/{item}/",
            {"choices": choice, "revision": str(revision)},
            "save_answer",
        )
        if code == 200:
            revision += 1
    session.post(f"/attempts/{attempt_id}/submit/", {}, "submit")
    session.get(f"/attempts/{attempt_id}/result/", "result_page")


def summarise() -> int:
    print(f"\n{'endpoint':<16}{'n':>6}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}")
    print("-" * 52)
    worst_p95 = 0.0
    for label, samples in sorted(RESULTS.items()):
        ordered = sorted(samples)
        p50 = statistics.median(ordered) * 1000
        p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)] * 1000
        worst = ordered[-1] * 1000
        worst_p95 = max(worst_p95, p95)
        print(f"{label:<16}{len(samples):>6}{p50:>10.0f}{p95:>10.0f}{worst:>10.0f}")
    print(f"\nWorst p95 across endpoints: {worst_p95:.0f} ms")
    return 0 if worst_p95 < 1000 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--students", type=int, default=10)
    parser.add_argument("--saves", type=int, default=5)
    parser.add_argument("--assignment", type=int, default=1)
    parser.add_argument("--username-prefix", default="load")
    parser.add_argument("--password", default="load-test-phrase-1")
    args = parser.parse_args()

    threads = [
        threading.Thread(
            target=run_student,
            args=(
                args.base,
                f"{args.username_prefix}{i}",
                args.password,
                args.assignment,
                args.saves,
            ),
        )
        for i in range(args.students)
    ]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started
    total = sum(len(v) for v in RESULTS.values())
    print(f"{args.students} concurrent students, {total} requests in {elapsed:.1f}s "
          f"({total / elapsed:.1f} req/s)")
    return summarise()


if __name__ == "__main__":
    sys.exit(main())
